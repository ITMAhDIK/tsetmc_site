"""FastAPI authentication API; deploy separately from GitHub Pages."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
from psycopg_pool import ConnectionPool, PoolTimeout
from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field
from psycopg.rows import dict_row
from psycopg.conninfo import conninfo_to_dict

from .repository import (
    authenticate_session, consume_phone_otp, login_password,
    register_password_user, request_phone_otp, revoke_session, create_session,
)
from .service import AuthenticationError, ConfigurationError

APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
COOKIE_NAME = "tloomberg_session"
COOKIE_SECURE = os.getenv("SAAS_COOKIE_SECURE", "true").lower() == "true"
TRUSTED_HOSTS = [
    item.strip() for item in os.getenv("SAAS_TRUSTED_HOSTS", "").split(",")
    if item.strip()
]
ALLOWED_ORIGINS = [
    item.strip().rstrip("/") for item in os.getenv("SAAS_ALLOWED_ORIGINS", "").split(",")
    if item.strip()
]
_SAFE_REQUEST_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
_DB_POOL: ConnectionPool | None = None
_ACCESS_LOG = logging.getLogger("tloomberg.saas.access")
_ACCESS_LOG.setLevel(logging.INFO)
_REQUIRED_SCHEMA_TABLES = (
    "users", "sessions", "roles", "permissions", "role_permissions",
    "plans", "subscriptions", "payment_orders", "payment_attempts",
    "wallet_accounts", "wallet_ledger_entries", "wallet_bank_accounts",
    "withdrawal_requests", "referrals", "tickets", "ticket_messages",
    "analytics_consents", "analytics_events", "admin_audit_logs",
    "payment_reconciliation_notes",
)
_REQUIRED_SCHEMA_COLUMNS = ("analytics_events.event_order",)


def validate_production_configuration() -> None:
    """Fail startup when the API is configured for production without key safeguards."""
    if APP_ENV not in {"prod", "production"}:
        return

    dsn = os.getenv("SAAS_DATABASE_URL", "").strip()
    expected_db = os.getenv("SAAS_EXPECTED_DATABASE_NAME", "").strip()
    otp_secret = os.getenv("SAAS_OTP_HMAC_SECRET", "")
    fernet_key = os.getenv("SAAS_OUTBOX_FERNET_KEY", "")

    if not dsn:
        raise ConfigurationError("SAAS_DATABASE_URL is required in production")
    try:
        database_connection = conninfo_to_dict(dsn)
    except Exception as exc:
        raise ConfigurationError("SAAS_DATABASE_URL is not a valid PostgreSQL connection string") from exc
    if database_connection.get("sslmode", "prefer").lower() != "verify-full":
        raise ConfigurationError("SAAS_DATABASE_URL must explicitly require sslmode=verify-full in production")
    if not expected_db:
        raise ConfigurationError("SAAS_EXPECTED_DATABASE_NAME is required in production")
    if len(otp_secret) < 32:
        raise ConfigurationError("SAAS_OTP_HMAC_SECRET must contain at least 32 characters")
    if not fernet_key:
        raise ConfigurationError("SAAS_OUTBOX_FERNET_KEY is required in production")
    if otp_secret == fernet_key:
        raise ConfigurationError("OTP and outbox encryption secrets must be different")
    if not COOKIE_SECURE:
        raise ConfigurationError("SAAS_COOKIE_SECURE must be true in production")
    if not TRUSTED_HOSTS or "*" in TRUSTED_HOSTS:
        raise ConfigurationError("SAAS_TRUSTED_HOSTS must contain explicit production hostnames")
    if not ALLOWED_ORIGINS:
        raise ConfigurationError("SAAS_ALLOWED_ORIGINS must contain explicit HTTPS origins")

    for origin in ALLOWED_ORIGINS:
        parsed = urlsplit(origin)
        if (parsed.scheme != "https" or not parsed.netloc
                or parsed.path or parsed.query or parsed.fragment
                or parsed.username or parsed.password):
            raise ConfigurationError("SAAS_ALLOWED_ORIGINS entries must be HTTPS origins without paths")
    try:
        Fernet(fernet_key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise ConfigurationError("SAAS_OUTBOX_FERNET_KEY must be a valid Fernet key") from exc

    _database_pool_config()


def _database_pool_config() -> tuple[int, int, float]:
    """Validate per-worker connection-pool bounds before opening sockets."""
    try:
        minimum = int(os.getenv("SAAS_DB_POOL_MIN", "1"))
        maximum = int(os.getenv("SAAS_DB_POOL_MAX", "10"))
        timeout = float(os.getenv("SAAS_DB_POOL_TIMEOUT_SECONDS", "5"))
    except ValueError as exc:
        raise ConfigurationError("database pool settings must be numeric") from exc
    if minimum < 1 or maximum < minimum or maximum > 100:
        raise ConfigurationError("database pool bounds must satisfy 1 <= min <= max <= 100")
    if not 0.1 <= timeout <= 60:
        raise ConfigurationError("SAAS_DB_POOL_TIMEOUT_SECONDS must be between 0.1 and 60")
    return minimum, maximum, timeout


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _DB_POOL
    validate_production_configuration()
    dsn = os.getenv("SAAS_DATABASE_URL", "").strip()
    if dsn:
        minimum, maximum, timeout = _database_pool_config()
        pool = ConnectionPool(
            conninfo=dsn,
            min_size=minimum,
            max_size=maximum,
            timeout=timeout,
            kwargs={"row_factory": dict_row, "connect_timeout": 5},
            open=False,
            name="tloomberg-saas-api",
        )
        try:
            pool.open(wait=True, timeout=5)
            with pool.connection(timeout=timeout) as conn:
                actual_database = conn.execute(
                    "SELECT current_database() AS database_name"
                ).fetchone()["database_name"]
            expected_database = os.getenv("SAAS_EXPECTED_DATABASE_NAME", "").strip()
            if expected_database and actual_database != expected_database:
                raise ConfigurationError(
                    "connected PostgreSQL database does not match SAAS_EXPECTED_DATABASE_NAME"
                )
            _DB_POOL = pool
            yield
        finally:
            _DB_POOL = None
            pool.close()
    else:
        # Enables liveness and route-unit tests without configuring a database.
        yield


app = FastAPI(title="Tloomberg SaaS API", version="0.8.0", lifespan=lifespan)
bearer = HTTPBearer(auto_error=False)

# Middleware is configured from explicit allow-lists before the application
# receives requests. No wildcard credentialed CORS configuration is permitted.
if TRUSTED_HOSTS:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=TRUSTED_HOSTS)
if ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
        max_age=600,
    )


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """Add security headers and log only non-sensitive route-level timing metadata."""
    supplied_id = request.headers.get("X-Request-ID", "")
    request_id = supplied_id if re.fullmatch(r"[A-Za-z0-9._-]{1,80}", supplied_id) else str(uuid4())
    request.state.request_id = request_id
    started = time.perf_counter()
    response = None
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        response.headers["Cache-Control"] = "no-store"
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response
    finally:
        route = request.scope.get("route")
        route_template = getattr(route, "path", "/unmatched")
        record = {
            "event": "http_request",
            "request_id": request_id,
            "method": request.method,
            "route": route_template,
            "status_code": response.status_code if response is not None else 500,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        }
        log_line = json.dumps(record, separators=(",", ":"), ensure_ascii=True)
        if response is not None and response.status_code < 500:
            _ACCESS_LOG.info(log_line)
        else:
            _ACCESS_LOG.error(log_line)


def get_conn():
    dsn = os.getenv("SAAS_DATABASE_URL", "").strip()
    if not dsn:
        raise HTTPException(status_code=503, detail="SaaS database is not configured")
    try:
        if _DB_POOL is not None:
            _minimum, _maximum, timeout = _database_pool_config()
            with _DB_POOL.connection(timeout=timeout) as conn:
                yield conn
            return
        if APP_ENV in {"prod", "production"}:
            # Production must never bypass lifespan startup, pool sizing,
            # or the expected-database identity check.
            raise HTTPException(status_code=503, detail="SaaS database pool is not ready")
        # Direct connections are retained only for local development and
        # dependency-level tools that intentionally run outside app lifespan.
        with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5) as conn:
            yield conn
    except (psycopg.OperationalError, PoolTimeout) as exc:
        raise HTTPException(status_code=503, detail="SaaS account service is unavailable") from exc


class RegisterBody(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=12, max_length=256)
    display_name: str | None = Field(default=None, max_length=160)


class PasswordLoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class OtpRequestBody(BaseModel):
    phone: str = Field(min_length=9, max_length=16)
    purpose: str = Field(default="login", pattern="^(signup|login)$")


class OtpVerifyBody(OtpRequestBody):
    otp: str = Field(min_length=6, max_length=6, pattern="^[0-9]{6}$")


def _set_session(response, token, expires):
    response.set_cookie(
        COOKIE_NAME, token, httponly=True, secure=COOKIE_SECURE,
        samesite="lax", expires=expires, path="/",
    )


def _token(request, credentials):
    return (credentials.credentials if credentials else None) or request.cookies.get(COOKIE_NAME)


def _request_origin(request: Request) -> str | None:
    """Extract an origin from Origin/Referer without accepting paths or userinfo."""
    raw_origin = request.headers.get("origin")
    if raw_origin:
        value = raw_origin.strip()
    else:
        referer = request.headers.get("referer")
        if not referer:
            return None
        parsed_referer = urlsplit(referer)
        if not parsed_referer.scheme or not parsed_referer.netloc:
            return None
        value = f"{parsed_referer.scheme}://{parsed_referer.netloc}"

    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.netloc
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or parsed.username or parsed.password):
        return None
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def _enforce_cookie_csrf_origin(request: Request) -> None:
    """Require an exact same-origin or explicitly allowed origin for cookie writes."""
    origin = _request_origin(request)
    request_origin = f"{request.url.scheme.lower()}://{request.url.netloc.lower()}"
    allowed = {item.lower() for item in ALLOWED_ORIGINS}
    if not origin or (origin != request_origin and origin not in allowed):
        raise HTTPException(status_code=403, detail="request origin is not allowed")


def current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    conn=Depends(get_conn),
):
    cookie_token = request.cookies.get(COOKIE_NAME)
    if cookie_token and credentials is None and request.method.upper() not in _SAFE_REQUEST_METHODS:
        _enforce_cookie_csrf_origin(request)
    token = _token(request, credentials)
    user = authenticate_session(conn, token) if token else None
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required")
    return user


@app.get("/health")
def health():
    """Liveness only; does not imply that dependencies or schema are ready."""
    return {"status": "ok", "service": "saas-api"}


@app.get("/ready")
def ready(conn=Depends(get_conn)):
    """Readiness probe verifies the selected SaaS database and required migrations."""
    with conn.cursor() as cur:
        cur.execute("SELECT current_database() AS database_name")
        database_name = cur.fetchone()["database_name"]
        expected_database = os.getenv("SAAS_EXPECTED_DATABASE_NAME", "").strip()
        if expected_database and database_name != expected_database:
            raise HTTPException(status_code=503, detail="SaaS database identity check failed")
        cur.execute(
            """SELECT COUNT(*)::integer AS missing
               FROM unnest(%s::text[]) AS required(name)
               WHERE to_regclass('public.' || required.name) IS NULL""",
            (list(_REQUIRED_SCHEMA_TABLES),),
        )
        missing = cur.fetchone()["missing"]
        cur.execute(
            """SELECT COUNT(*)::integer AS missing
               FROM unnest(%s::text[]) AS required(value)
               LEFT JOIN information_schema.columns c
                 ON c.table_schema='public'
                AND c.table_name=split_part(required.value,'.',1)
                AND c.column_name=split_part(required.value,'.',2)
               WHERE c.column_name IS NULL""",
            (list(_REQUIRED_SCHEMA_COLUMNS),),
        )
        missing_columns = cur.fetchone()["missing"]
    if missing or missing_columns:
        raise HTTPException(status_code=503, detail="SaaS database schema is not ready")
    return {"status": "ready", "service": "saas-api"}


@app.post("/auth/register")
def register(body: RegisterBody, response: Response, conn=Depends(get_conn)):
    try:
        user = register_password_user(conn, body.username, body.password, body.display_name)
        with conn.transaction():
            token, expires = create_session(conn, user["id"])
        _set_session(response, token, expires)
        return {"user": {
            "id": str(user["id"]), "public_id": user["public_id"],
            "username": user["username"], "display_name": user["display_name"],
        }}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except psycopg.errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="username is already in use")


@app.post("/auth/login")
def login(body: PasswordLoginBody, response: Response, conn=Depends(get_conn)):
    try:
        token, expires = login_password(conn, body.username, body.password)
        _set_session(response, token, expires)
        return {"authenticated": True, "expires_at": expires}
    except AuthenticationError:
        raise HTTPException(status_code=401, detail="invalid username or password")


@app.post("/auth/phone/request")
def phone_request(body: OtpRequestBody, conn=Depends(get_conn)):
    try:
        request_phone_otp(conn, body.phone, body.purpose)
        return {"accepted": True, "delivery": "queued", "expires_in_seconds": 300}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/auth/phone/verify")
def phone_verify(body: OtpVerifyBody, response: Response, conn=Depends(get_conn)):
    try:
        token, expires = consume_phone_otp(conn, body.phone, body.otp, body.purpose)
        _set_session(response, token, expires)
        return {"authenticated": True, "expires_at": expires}
    except (AuthenticationError, ValueError):
        raise HTTPException(status_code=401, detail="invalid or expired verification code")


@app.get("/auth/me")
def me(user=Depends(current_user)):
    return {"user": {
        "id": str(user["id"]), "public_id": user["public_id"], "username": user["username"],
        "display_name": user["display_name"], "account_type": user["account_type"],
    }}


@app.post("/auth/logout")
def logout(
    request: Request,
    response: Response,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    conn=Depends(get_conn),
    user=Depends(current_user),
):
    revoke_session(conn, _token(request, credentials))
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, secure=COOKIE_SECURE, samesite="lax")
    return {"authenticated": False}


from saas_core.account.api import router as account_router
from saas_core.payments.api import router as payments_router
from saas_core.wallet.api import router as wallet_router
from saas_core.support.api import router as support_router
from saas_core.analytics.api import router as analytics_router
from saas_core.admin.api import router as admin_router

app.include_router(account_router)
app.include_router(payments_router)
app.include_router(wallet_router)
app.include_router(support_router)
app.include_router(analytics_router)
app.include_router(admin_router)
