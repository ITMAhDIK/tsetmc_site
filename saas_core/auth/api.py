"""FastAPI authentication API; deploy separately from GitHub Pages."""
from __future__ import annotations

import hmac
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
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram, REGISTRY, generate_latest, multiprocess

from .mfa import matching_counter, new_totp_secret, provisioning_uri

from .repository import (
    authenticate_session, consume_phone_otp, login_password,
    register_password_user, request_phone_otp, revoke_session, create_session,
)
from .service import AuthenticationError, ConfigurationError, normalize_phone
from .identity_client import (
    IdentityUnavailable, IdentityUnauthorized, identity_enabled,
    introspect as identity_introspect, project_identity_user,
)

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
_HTTP_REQUESTS = Counter(
    "saas_http_requests_total",
    "HTTP requests grouped by method, route template and status code.",
    ("method", "route", "status"),
)
_HTTP_REQUEST_DURATION = Histogram(
    "saas_http_request_duration_seconds",
    "HTTP request duration grouped by method and route template.",
    ("method", "route"),
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
_REQUIRED_SCHEMA_TABLES = (
    "users", "sessions", "roles", "permissions", "role_permissions",
    "plans", "subscriptions", "payment_orders", "payment_attempts",
    "wallet_accounts", "wallet_ledger_entries", "wallet_bank_accounts",
    "withdrawal_requests", "referrals", "tickets", "ticket_messages", "notification_outbox",
    "analytics_consents", "analytics_events", "admin_audit_logs",
    "payment_reconciliation_notes", "user_admin_mfa", "legal_acceptances",
    "crm_customer_profiles", "crm_interactions", "crm_followups",
)
_REQUIRED_SCHEMA_COLUMNS = (
    "analytics_events.event_order",
    "sessions.mfa_verified_at",
    "phone_verifications.signup_terms_version",
    "phone_verifications.signup_privacy_version",
    "notification_outbox.locked_at",
    "notification_outbox.last_error",
)


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
    bootstrap_phone = os.getenv("SAAS_SUPERADMIN_PHONE_E164", "").strip()
    if not bootstrap_phone:
        raise ConfigurationError("SAAS_SUPERADMIN_PHONE_E164 is required in production")
    try:
        normalize_phone(bootstrap_phone)
    except ValueError as exc:
        raise ConfigurationError("SAAS_SUPERADMIN_PHONE_E164 must be a valid E.164 number") from exc
    if not fernet_key:
        raise ConfigurationError("SAAS_OUTBOX_FERNET_KEY is required in production")
    identity_url = os.getenv("IDENTITY_SERVICE_URL", "").strip()
    identity_token = os.getenv("IDENTITY_SERVICE_TOKEN", "").strip()
    if not identity_url or len(identity_token) < 32:
        raise ConfigurationError("IDENTITY_SERVICE_URL and a distinct IDENTITY_SERVICE_TOKEN are required in production")
    identity_origin = urlsplit(identity_url)
    if (identity_origin.scheme not in {"http", "https"} or not identity_origin.netloc
            or identity_origin.path not in {"", "/"}
            or identity_origin.username or identity_origin.password
            or identity_origin.query or identity_origin.fragment):
        raise ConfigurationError("IDENTITY_SERVICE_URL must be a valid private HTTP(S) service origin")
    metrics_token = os.getenv("SAAS_METRICS_TOKEN", "").strip()
    if len(metrics_token) < 32:
        raise ConfigurationError("SAAS_METRICS_TOKEN must contain at least 32 characters")
    metrics_dir = os.getenv("PROMETHEUS_MULTIPROC_DIR", "").strip()
    if not metrics_dir:
        raise ConfigurationError("PROMETHEUS_MULTIPROC_DIR must be configured for multi-worker metrics")

    admin_mfa_key = os.getenv("SAAS_ADMIN_MFA_FERNET_KEY", "")
    if not admin_mfa_key or admin_mfa_key in {fernet_key, otp_secret}:
        raise ConfigurationError("SAAS_ADMIN_MFA_FERNET_KEY must be a separate Fernet key")
    try:
        Fernet(admin_mfa_key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise ConfigurationError("SAAS_ADMIN_MFA_FERNET_KEY must be a valid Fernet key") from exc
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
        duration_seconds = max(0.0, time.perf_counter() - started)
        try:
            _HTTP_REQUESTS.labels(
                request.method, route_template, str(record["status_code"])
            ).inc()
            _HTTP_REQUEST_DURATION.labels(
                request.method, route_template
            ).observe(duration_seconds)
        except Exception:
            # Metrics must never turn a completed business response into a 500.
            pass
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
    terms_accepted: bool = False
    terms_version: str | None = Field(default=None, max_length=80)
    privacy_version: str | None = Field(default=None, max_length=80)


class PasswordLoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class OtpRequestBody(BaseModel):
    phone: str = Field(min_length=9, max_length=16)
    purpose: str = Field(default="login", pattern="^(signup|login)$")
    terms_accepted: bool = False
    terms_version: str | None = Field(default=None, max_length=80)
    privacy_version: str | None = Field(default=None, max_length=80)


class OtpVerifyBody(OtpRequestBody):
    otp: str = Field(min_length=6, max_length=6, pattern="^[0-9]{6}$")


def _validate_legal_acceptance(accepted: bool, terms_version: str | None,
                              privacy_version: str | None) -> tuple[str, str]:
    """Fail closed until the owner publishes versioned legal text."""
    required_terms = os.getenv("SAAS_TERMS_VERSION", "").strip()
    required_privacy = os.getenv("SAAS_PRIVACY_VERSION", "").strip()
    placeholders = {"pending", "draft", "pending-owner-content"}
    if (not required_terms or not required_privacy
            or required_terms.lower() in placeholders
            or required_privacy.lower() in placeholders):
        raise HTTPException(status_code=503,
            detail="registration is disabled until reviewed terms and privacy versions are published")
    if accepted is not True or terms_version != required_terms or privacy_version != required_privacy:
        raise HTTPException(status_code=422, detail="current terms and privacy notice must both be accepted")
    return required_terms, required_privacy


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
    if identity_enabled():
        try:
            principal = identity_introspect(token or "")
            principal = project_identity_user(conn, principal)
            return principal
        except IdentityUnauthorized:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required") from None
        except IdentityUnavailable:
            # Identity owns the session source of truth. Do not fall back to
            # potentially stale local SaaS sessions when it is unavailable.
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                                detail="Identity service is temporarily unavailable") from None
    user = authenticate_session(conn, token) if token else None
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required")
    return user


@app.get("/internal/metrics")
def internal_metrics(request: Request):
    """Private Prometheus scrape endpoint; Caddy intentionally does not proxy it."""
    expected = os.getenv("SAAS_METRICS_TOKEN", "").strip()
    if not expected:
        raise HTTPException(status_code=404, detail="not found")
    supplied_header = request.headers.get("Authorization", "")
    supplied = supplied_header[7:].strip() if supplied_header.startswith("Bearer ") else ""
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="metrics authorization required")
    multiprocess_dir = os.getenv("PROMETHEUS_MULTIPROC_DIR", "").strip()
    if multiprocess_dir:
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
    else:
        registry = REGISTRY
    return Response(
        content=generate_latest(registry),
        media_type=CONTENT_TYPE_LATEST,
        headers={"Cache-Control": "no-store"},
    )


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
    if identity_enabled():
        from .identity_client import identity_health
        try:
            identity_health()
        except IdentityUnavailable:
            raise HTTPException(status_code=503, detail="Identity service is not ready") from None
    return {"status": "ready", "service": "saas-api"}


@app.post("/auth/register")
def register(body: RegisterBody, response: Response, conn=Depends(get_conn)):
    terms_version, privacy_version = _validate_legal_acceptance(
        body.terms_accepted, body.terms_version, body.privacy_version
    )
    try:
        # The user row, initial role, wallet/preferences, legal acceptance,
        # and session are one transaction. A failed consent write cannot leave
        # behind an account that was registered without accepted legal text.
        with conn.transaction():
            user = register_password_user(conn, body.username, body.password, body.display_name)
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO legal_acceptances
                       (user_id,terms_version,privacy_version,source)
                       VALUES (%s,%s,%s,'password_signup')""",
                    (user["id"], terms_version, privacy_version),
                )
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
    terms_version = privacy_version = None
    if body.purpose == "signup":
        terms_version, privacy_version = _validate_legal_acceptance(
            body.terms_accepted, body.terms_version, body.privacy_version
        )
    try:
        request_phone_otp(
            conn, body.phone, body.purpose,
            terms_version=terms_version, privacy_version=privacy_version,
        )
        return {"accepted": True, "delivery": "queued", "expires_in_seconds": 300,
                "terms_version": terms_version, "privacy_version": privacy_version}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/auth/phone/verify")
def phone_verify(body: OtpVerifyBody, response: Response, conn=Depends(get_conn)):
    terms_version = privacy_version = None
    if body.purpose == "signup":
        terms_version, privacy_version = _validate_legal_acceptance(
            body.terms_accepted, body.terms_version, body.privacy_version
        )
    try:
        token, expires = consume_phone_otp(
            conn, body.phone, body.otp, body.purpose,
            terms_version=terms_version, privacy_version=privacy_version,
        )
        _set_session(response, token, expires)
        user = authenticate_session(conn, token)
        roles = _user_roles(conn, user["id"])
        return {"authenticated": True, "expires_at": expires,
                "role": _primary_role(roles),
                "user": {"id": str(user["id"]), "public_id": user["public_id"],
                         "username": user["username"], "display_name": user["display_name"],
                         "phone_e164": body.phone, "roles": roles}}
    except (AuthenticationError, ValueError):
        raise HTTPException(status_code=401, detail="invalid or expired verification code")


def _user_roles(conn, user_id):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT COALESCE(array_agg(r.code ORDER BY r.code),'{}') AS roles
               FROM user_roles ur JOIN roles r ON r.id=ur.role_id
               WHERE ur.user_id=%s""",
            (user_id,),
        )
        return cur.fetchone()["roles"]


def _primary_role(roles):
    for role in ("superadmin", "admin", "support", "data_manager", "user"):
        if role in roles:
            return role
    return "user"


@app.get("/auth/me")
def me(user=Depends(current_user), conn=Depends(get_conn)):
    roles = _user_roles(conn, user["id"])
    return {"role": _primary_role(roles), "user": {
        "id": str(user["id"]), "public_id": user["public_id"], "username": user["username"],
        "display_name": user["display_name"], "account_type": user["account_type"],
        "phone_e164": user.get("phone_e164"), "roles": roles,
        "is_superadmin": "superadmin" in roles,
        "mfa_enabled": user.get("mfa_enabled", False),
        "mfa_verified": user.get("mfa_verified_at") is not None,
    }}


def _admin_mfa_cipher() -> Fernet:
    key = os.getenv("SAAS_ADMIN_MFA_FERNET_KEY", "").strip()
    if not key:
        raise HTTPException(status_code=503, detail="privileged MFA encryption is not configured")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        raise HTTPException(status_code=503, detail="privileged MFA encryption is not configured") from None


def _require_privileged_account(conn, user_id, principal=None):
    from .authorization import has_permission
    # Identity claims are authoritative when enabled; local role lookup is
    # retained only for compatibility tests and local development.
    if not (
        has_permission(conn, user_id, "superadmin.console.access", principal=principal)
        or has_permission(conn, user_id, "crm.manage", principal=principal)
    ):
        raise HTTPException(status_code=403, detail="privileged account required")


def require_crm_mfa(user=Depends(current_user), conn=Depends(get_conn)):
    """Require an enrolled TOTP device and a recent verification for CRM staff."""
    if user.get("mfa_verified_at") is None:
        raise HTTPException(status_code=403, detail="recent MFA verification is required")
    if identity_enabled():
        if not user.get("mfa_enabled"):
            raise HTTPException(status_code=403, detail="privileged MFA enrollment is required")
        return user
    with conn.cursor() as cur:
        cur.execute(
            "SELECT enabled_at FROM user_admin_mfa WHERE user_id=%s",
            (user["id"],),
        )
        record = cur.fetchone()
    if not record or record["enabled_at"] is None:
        raise HTTPException(status_code=403, detail="privileged MFA enrollment is required")
    return user


def require_admin_mfa(user=Depends(current_user), conn=Depends(get_conn)):
    """Dependency applied to every privileged admin route."""
    if user.get("mfa_verified_at") is None:
        raise HTTPException(status_code=403, detail="recent MFA verification is required")
    if identity_enabled():
        if not user.get("mfa_enabled"):
            raise HTTPException(status_code=403, detail="privileged MFA enrollment is required")
        return user
    with conn.cursor() as cur:
        cur.execute(
            "SELECT enabled_at FROM user_admin_mfa WHERE user_id=%s",
            (user["id"],),
        )
        record = cur.fetchone()
    if not record or record["enabled_at"] is None:
        raise HTTPException(status_code=403, detail="privileged MFA enrollment is required")
    return user


@app.get("/auth/mfa/status")
def mfa_status(user=Depends(current_user), conn=Depends(get_conn)):
    _require_privileged_account(conn, user["id"], principal=user)
    with conn.cursor() as cur:
        cur.execute("SELECT enabled_at FROM user_admin_mfa WHERE user_id=%s", (user["id"],))
        row = cur.fetchone()
    return {
        "enabled": bool(row and row["enabled_at"] is not None),
        "verified_for_session": user.get("mfa_verified_at") is not None,
    }


@app.post("/auth/mfa/setup")
def mfa_setup(user=Depends(current_user), conn=Depends(get_conn)):
    """Create a one-time enrollment secret; never returns an existing enabled secret."""
    _require_privileged_account(conn, user["id"])
    secret = new_totp_secret()
    cipher = _admin_mfa_cipher()
    encrypted = cipher.encrypt(secret.encode("ascii")).decode("ascii")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT enabled_at FROM user_admin_mfa WHERE user_id=%s FOR UPDATE", (user["id"],))
            existing = cur.fetchone()
            if existing and existing["enabled_at"] is not None:
                raise HTTPException(status_code=409, detail="MFA is already enabled; use the verified recovery process to reset it")
            cur.execute(
                """INSERT INTO user_admin_mfa(user_id,encrypted_secret,enabled_at,last_verified_counter,updated_at)
                   VALUES (%s,%s,NULL,NULL,now())
                   ON CONFLICT(user_id) DO UPDATE
                     SET encrypted_secret=EXCLUDED.encrypted_secret,enabled_at=NULL,
                         last_verified_counter=NULL,updated_at=now()""",
                (user["id"], encrypted),
            )
    account = user.get("phone_e164") or user.get("username") or str(user["id"])
    return {
        "secret": secret,
        "otpauth_uri": provisioning_uri(secret, account=account),
        "issuer": "Tloomberg",
        "period_seconds": 30,
        "digits": 6,
    }


class MfaCodeBody(BaseModel):
    code: str = Field(pattern="^[0-9]{6}$")


def _load_pending_mfa_secret(conn, user_id, *, require_enabled: bool):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT encrypted_secret,enabled_at,last_verified_counter FROM user_admin_mfa WHERE user_id=%s FOR UPDATE",
            (user_id,),
        )
        row = cur.fetchone()
    if not row or (require_enabled and row["enabled_at"] is None) or (not require_enabled and row["enabled_at"] is not None):
        raise HTTPException(status_code=409, detail="MFA setup state is invalid")
    try:
        secret = _admin_mfa_cipher().decrypt(row["encrypted_secret"].encode("ascii")).decode("ascii")
    except Exception:
        raise HTTPException(status_code=503, detail="privileged MFA secret cannot be decrypted") from None
    return secret, row


def _consume_mfa_counter(conn, user, code: str, secret: str, previous_counter):
    import time as _time
    counter = matching_counter(secret, code, at_time=_time.time(), window=1)
    if counter is None:
        raise HTTPException(status_code=401, detail="invalid or expired MFA code")
    if previous_counter is not None and counter <= int(previous_counter):
        raise HTTPException(status_code=409, detail="MFA code has already been used")
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE user_admin_mfa
               SET last_verified_counter=%s,updated_at=now()
               WHERE user_id=%s AND (last_verified_counter IS NULL OR last_verified_counter < %s)
               RETURNING user_id""",
            (counter, user["id"], counter),
        )
        if cur.fetchone() is None:
            raise HTTPException(status_code=409, detail="MFA code has already been used")
        cur.execute(
            """UPDATE sessions SET mfa_verified_at=now()
               WHERE id=%s AND user_id=%s AND revoked_at IS NULL AND expires_at>now()
               RETURNING id""",
            (user["session_id"], user["id"]),
        )
        if cur.fetchone() is None:
            raise HTTPException(status_code=401, detail="session is no longer active")


@app.post("/auth/mfa/confirm")
def mfa_confirm(body: MfaCodeBody, user=Depends(current_user), conn=Depends(get_conn)):
    _require_privileged_account(conn, user["id"])
    with conn.transaction():
        secret, row = _load_pending_mfa_secret(conn, user["id"], require_enabled=False)
        _consume_mfa_counter(conn, user, body.code, secret, row["last_verified_counter"])
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE user_admin_mfa SET enabled_at=now(),updated_at=now() WHERE user_id=%s",
                (user["id"],),
            )
    return {"enabled": True, "verified_for_session": True}


@app.post("/auth/mfa/verify")
def mfa_verify(body: MfaCodeBody, user=Depends(current_user), conn=Depends(get_conn)):
    _require_privileged_account(conn, user["id"])
    with conn.transaction():
        secret, row = _load_pending_mfa_secret(conn, user["id"], require_enabled=True)
        _consume_mfa_counter(conn, user, body.code, secret, row["last_verified_counter"])
    return {"verified": True, "verified_at": "session-bound"}


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
from saas_core.crm.api import router as crm_router

app.include_router(account_router)
app.include_router(payments_router)
app.include_router(wallet_router)
app.include_router(support_router)
app.include_router(analytics_router)
app.include_router(admin_router)
app.include_router(crm_router)
