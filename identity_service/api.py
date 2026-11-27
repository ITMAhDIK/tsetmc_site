"""Independent Identity Service: credentials, sessions, MFA and authorization."""
from __future__ import annotations

import hmac
import hashlib
import json
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from urllib.parse import urlsplit
from uuid import UUID

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout
from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from saas_core.auth.mfa import matching_counter, new_totp_secret, provisioning_uri
from saas_core.auth.service import (
    AuthenticationError, ConfigurationError, hash_password,
    hash_session_token, new_otp, new_session_token, normalize_phone,
    verify_password, _DUMMY_PASSWORD_HASH, SESSION_TTL, OTP_TTL,
)

APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
COOKIE_NAME = "tloomberg_session"
COOKIE_SECURE = os.getenv("IDENTITY_COOKIE_SECURE", "true").lower() == "true"
TRUSTED_HOSTS = [v.strip() for v in os.getenv("IDENTITY_TRUSTED_HOSTS", "").split(",") if v.strip()]
ALLOWED_ORIGINS = [v.strip().rstrip("/") for v in os.getenv("IDENTITY_ALLOWED_ORIGINS", "").split(",") if v.strip()]
_POOL: ConnectionPool | None = None
bearer = HTTPBearer(auto_error=False)
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}

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

class OtpBody(BaseModel):
    phone: str = Field(min_length=9, max_length=16)
    purpose: str = Field(default="login", pattern="^(signup|login)$")
    terms_accepted: bool = False
    terms_version: str | None = Field(default=None, max_length=80)
    privacy_version: str | None = Field(default=None, max_length=80)

class OtpVerifyBody(OtpBody):
    otp: str = Field(min_length=6, max_length=6, pattern="^[0-9]{6}$")

class MfaCodeBody(BaseModel):
    code: str = Field(pattern="^[0-9]{6}$")

class InternalTokenBody(BaseModel):
    token: str = Field(min_length=20, max_length=256)

class UserStatusBody(BaseModel):
    status: str = Field(pattern="^(active|suspended)$")
    reason: str = Field(min_length=3, max_length=500)

class RoleBody(BaseModel):
    grant: bool

class ProfileBody(BaseModel):
    display_name: str | None = Field(default=None, max_length=160)

def _pool_bounds() -> tuple[int, int, float]:
    try:
        minimum = int(os.getenv("IDENTITY_DB_POOL_MIN", "1"))
        maximum = int(os.getenv("IDENTITY_DB_POOL_MAX", "8"))
        timeout = float(os.getenv("IDENTITY_DB_POOL_TIMEOUT_SECONDS", "5"))
    except ValueError as exc:
        raise ConfigurationError("Identity pool settings must be numeric") from exc
    if not 1 <= minimum <= maximum <= 100 or not 0.1 <= timeout <= 60:
        raise ConfigurationError("Identity pool bounds or timeout are invalid")
    return minimum, maximum, timeout

def validate_configuration() -> None:
    dsn = os.getenv("IDENTITY_DATABASE_URL", "").strip()
    expected = os.getenv("IDENTITY_EXPECTED_DATABASE_NAME", "").strip()
    if APP_ENV in {"prod", "production"}:
        if not dsn or not expected:
            raise ConfigurationError("Identity database URL and expected database name are required")
        params = conninfo_to_dict(dsn)
        if params.get("sslmode", "prefer").lower() != "verify-full":
            raise ConfigurationError("Remote Identity PostgreSQL requires sslmode=verify-full")
        if len(os.getenv("IDENTITY_SERVICE_TOKEN", "")) < 32:
            raise ConfigurationError("IDENTITY_SERVICE_TOKEN must contain at least 32 characters")
        if len(os.getenv("IDENTITY_OTP_HMAC_SECRET", "")) < 32:
            raise ConfigurationError("IDENTITY_OTP_HMAC_SECRET must contain at least 32 characters")
        for name in ("IDENTITY_OUTBOX_FERNET_KEY", "IDENTITY_ADMIN_MFA_FERNET_KEY"):
            try:
                Fernet(os.getenv(name, "").encode("ascii"))
            except Exception as exc:
                raise ConfigurationError(f"{name} must be a valid Fernet key") from exc
        if os.getenv("IDENTITY_OUTBOX_FERNET_KEY") == os.getenv("IDENTITY_ADMIN_MFA_FERNET_KEY"):
            raise ConfigurationError("Identity outbox and MFA keys must be independent")
        if not COOKIE_SECURE or not TRUSTED_HOSTS or "*" in TRUSTED_HOSTS:
            raise ConfigurationError("Identity production cookies and trusted hosts are not secure")
        if not ALLOWED_ORIGINS:
            raise ConfigurationError("IDENTITY_ALLOWED_ORIGINS must contain HTTPS origins")
        for origin in ALLOWED_ORIGINS:
            parsed = urlsplit(origin)
            if parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment:
                raise ConfigurationError("Identity allowed origins must be HTTPS origins without paths")
    _pool_bounds()

@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _POOL
    validate_configuration()
    dsn = os.getenv("IDENTITY_DATABASE_URL", "").strip()
    if not dsn:
        yield
        return
    minimum, maximum, timeout = _pool_bounds()
    pool = ConnectionPool(
        conninfo=dsn, min_size=minimum, max_size=maximum, timeout=timeout,
        kwargs={"row_factory": dict_row, "connect_timeout": 5},
        open=False, name="tloomberg-identity",
    )
    try:
        pool.open(wait=True, timeout=5)
        database = pool.getconn(timeout=timeout)
        try:
            actual = database.execute("SELECT current_database() AS name").fetchone()["name"]
        finally:
            pool.putconn(database)
        expected = os.getenv("IDENTITY_EXPECTED_DATABASE_NAME", "").strip()
        if expected and actual != expected:
            raise ConfigurationError("Connected database does not match IDENTITY_EXPECTED_DATABASE_NAME")
        _POOL = pool
        yield
    finally:
        _POOL = None
        pool.close()

app = FastAPI(title="Tloomberg Identity Service", version="1.0.0", lifespan=lifespan)
if TRUSTED_HOSTS:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=TRUSTED_HOSTS)
if ALLOWED_ORIGINS:
    app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_credentials=True,
                       allow_methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS"],
                       allow_headers=["Authorization","Content-Type","X-Identity-Service-Token","X-Identity-User-Token"],
                       max_age=600)

def get_conn():
    dsn = os.getenv("IDENTITY_DATABASE_URL", "").strip()
    if not dsn:
        raise HTTPException(status_code=503, detail="Identity database is not configured")
    try:
        if _POOL is not None:
            _minimum, _maximum, timeout = _pool_bounds()
            with _POOL.connection(timeout=timeout) as conn:
                yield conn
            return
        if APP_ENV in {"prod", "production"}:
            raise HTTPException(status_code=503, detail="Identity connection pool is not ready")
        with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5) as conn:
            yield conn
    except (psycopg.OperationalError, PoolTimeout):
        raise HTTPException(status_code=503, detail="Identity database is temporarily unavailable") from None

def _service_auth(x_identity_service_token: str | None = Header(default=None, alias="X-Identity-Service-Token")):
    expected = os.getenv("IDENTITY_SERVICE_TOKEN", "").strip()
    if not expected or not x_identity_service_token or not hmac.compare_digest(expected, x_identity_service_token):
        raise HTTPException(status_code=401, detail="Identity service authentication required")
    return True

def _request_token(request: Request, credentials: HTTPAuthorizationCredentials | None) -> str | None:
    return (credentials.credentials if credentials else None) or request.cookies.get(COOKIE_NAME)

def _origin(request: Request) -> str | None:
    raw = request.headers.get("origin")
    if not raw:
        referer = request.headers.get("referer")
        if not referer:
            return None
        p = urlsplit(referer)
        if not p.scheme or not p.netloc:
            return None
        raw = f"{p.scheme}://{p.netloc}"
    p = urlsplit(raw.strip())
    if p.scheme not in {"http","https"} or not p.netloc or p.path not in {"","/"} or p.query or p.fragment or p.username or p.password:
        return None
    return f"{p.scheme.lower()}://{p.netloc.lower()}"

def _csrf(request: Request) -> None:
    origin = _origin(request)
    expected = f"{request.url.scheme.lower()}://{request.url.netloc.lower()}"
    allowed = {v.lower() for v in ALLOWED_ORIGINS}
    if not origin or (origin != expected and origin not in allowed):
        raise HTTPException(status_code=403, detail="request origin is not allowed")

def _set_cookie(response: Response, token: str, expires) -> None:
    response.set_cookie(COOKIE_NAME, token, httponly=True, secure=COOKIE_SECURE,
                        samesite="lax", expires=expires, path="/")

def _create_session(conn, user_id: UUID) -> tuple[str, object]:
    token = new_session_token()
    expires = __import__("datetime").datetime.now(__import__("datetime").timezone.utc) + SESSION_TTL
    with conn.cursor() as cur:
        cur.execute("INSERT INTO sessions(user_id,token_hash,expires_at) VALUES (%s,%s,%s)",
                    (user_id, hash_session_token(token), expires))
    return token, expires

def _user_roles_permissions(conn, user_id: UUID) -> tuple[list[str], list[str]]:
    with conn.cursor() as cur:
        cur.execute("""SELECT COALESCE(array_agg(DISTINCT r.code ORDER BY r.code),'{}') AS roles
                       FROM user_roles ur JOIN roles r ON r.id=ur.role_id WHERE ur.user_id=%s""", (user_id,))
        roles = cur.fetchone()["roles"]
        cur.execute("""SELECT COALESCE(array_agg(DISTINCT p.code ORDER BY p.code),'{}') AS permissions
                       FROM user_roles ur JOIN role_permissions rp ON rp.role_id=ur.role_id
                       JOIN permissions p ON p.id=rp.permission_id WHERE ur.user_id=%s""", (user_id,))
        permissions = cur.fetchone()["permissions"]
    return roles, permissions

def _principal(conn, token: str) -> dict | None:
    if not token or len(token) > 256:
        return None
    with conn.cursor() as cur:
        cur.execute("""SELECT u.id,u.public_id,u.username,u.display_name,u.account_type,u.status,
                              u.phone_e164,u.phone_verified_at,u.email,u.email_verified_at,
                              u.created_at,u.updated_at,u.last_login_at,
                              s.id AS session_id,
                              CASE WHEN s.mfa_verified_at>now()-interval '12 hours'
                                     AND s.mfa_verified_at<=now()+interval '1 minute'
                                   THEN s.mfa_verified_at ELSE NULL END AS mfa_verified_at
                       FROM sessions s JOIN users u ON u.id=s.user_id
                       WHERE s.token_hash=%s AND s.revoked_at IS NULL
                         AND s.expires_at>now() AND u.status='active'""",
                    (hash_session_token(token),))
        user = cur.fetchone()
        if not user:
            return None
        cur.execute("UPDATE sessions SET last_seen_at=now() WHERE id=%s", (user["session_id"],))
        cur.execute("SELECT enabled_at FROM user_admin_mfa WHERE user_id=%s", (user["id"],))
        mfa = cur.fetchone()
    roles, permissions = _user_roles_permissions(conn, user["id"])
    return {
        "user": {key: value for key, value in user.items() if key != "session_id"},
        "session_id": str(user["session_id"]),
        "roles": roles,
        "permissions": permissions,
        "mfa_enabled": bool(mfa and mfa["enabled_at"] is not None),
        "mfa_verified_at": user["mfa_verified_at"],
    }

def _current_user(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
                  conn=Depends(get_conn)):
    cookie = request.cookies.get(COOKIE_NAME)
    if cookie and credentials is None and request.method.upper() not in SAFE_METHODS:
        _csrf(request)
    token = _request_token(request, credentials)
    value = _principal(conn, token) if token else None
    if not value:
        raise HTTPException(status_code=401, detail="authentication required")
    value["_session_token"] = token
    return value

def _legal_versions(accepted: bool, terms: str | None, privacy: str | None) -> tuple[str, str]:
    expected_terms = os.getenv("IDENTITY_TERMS_VERSION", "").strip()
    expected_privacy = os.getenv("IDENTITY_PRIVACY_VERSION", "").strip()
    placeholders = {"pending","draft","pending-owner-content"}
    if not expected_terms or not expected_privacy or expected_terms.lower() in placeholders or expected_privacy.lower() in placeholders:
        raise HTTPException(status_code=503, detail="registration is disabled until reviewed terms and privacy versions are published")
    if accepted is not True or terms != expected_terms or privacy != expected_privacy:
        raise HTTPException(status_code=422, detail="current terms and privacy notice must both be accepted")
    return expected_terms, expected_privacy

def _audit(conn, actor_id: UUID | None, action: str, target_id: str, after: dict | None = None):
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO admin_audit_logs(actor_user_id,action,target_type,target_id,after_state)
                       VALUES (%s,%s,'user',%s,%s::jsonb)""",
                    (actor_id, action, target_id, json.dumps(after or {})))

def _service_principal(conn, actor_token: str | None, permission: str) -> dict:
    value = _principal(conn, actor_token or "")
    if not value:
        raise HTTPException(status_code=401, detail="acting user session is invalid")
    if permission not in value["permissions"]:
        raise HTTPException(status_code=403, detail="acting user lacks the required permission")
    # Administrative identity mutations are protected by session-bound TOTP.
    if permission in {"users.manage", "roles.manage"} and (
        not value["mfa_enabled"] or value["mfa_verified_at"] is None
    ):
        raise HTTPException(status_code=403, detail="recent session-bound MFA verification is required")
    return value

def _mfa_cipher() -> Fernet:
    key = os.getenv("IDENTITY_ADMIN_MFA_FERNET_KEY", "").strip()
    try:
        return Fernet(key.encode("ascii"))
    except Exception:
        raise HTTPException(status_code=503, detail="Identity MFA encryption is not configured") from None

def _privileged(principal: dict):
    permissions = principal.get("permissions", [])
    if "superadmin.console.access" not in permissions and "crm.manage" not in permissions:
        raise HTTPException(status_code=403, detail="privileged account required")

@app.get("/health")
def health():
    return {"status":"ok","service":"identity"}

@app.get("/ready")
def ready(conn=Depends(get_conn)):
    with conn.cursor() as cur:
        cur.execute("SELECT current_database() AS name")
        actual = cur.fetchone()["name"]
        expected = os.getenv("IDENTITY_EXPECTED_DATABASE_NAME", "").strip()
        if expected and actual != expected:
            raise HTTPException(status_code=503, detail="Identity database identity check failed")
        required = ("users","auth_identities","email_verifications","phone_verifications","sessions","roles","permissions",
                    "role_permissions","user_roles","user_admin_mfa","legal_acceptances","notification_outbox")
        cur.execute("""SELECT COUNT(*)::integer AS missing FROM unnest(%s::text[]) r(name)
                       WHERE to_regclass('public.' || r.name) IS NULL""", (list(required),))
        if cur.fetchone()["missing"]:
            raise HTTPException(status_code=503, detail="Identity schema is not ready")
    return {"status":"ready","service":"identity"}

@app.post("/auth/register", status_code=201)
def register(body: RegisterBody, response: Response, conn=Depends(get_conn)):
    terms, privacy = _legal_versions(body.terms_accepted, body.terms_version, body.privacy_version)
    username = body.username.strip().lower()
    if not 3 <= len(username) <= 64 or not username.replace("_","").replace(".","").isalnum():
        raise HTTPException(status_code=422, detail="username must be 3-64 characters and contain letters, numbers, . or _")
    try:
        password_hash = hash_password(body.password)
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO users(public_id,username,display_name,password_hash)
                               VALUES (%s,%s,%s,%s) RETURNING id,public_id,username,display_name,created_at,updated_at""",
                            ("usr_"+secrets.token_hex(12), username, body.display_name, password_hash))
                user = cur.fetchone()
                cur.execute("INSERT INTO user_roles(user_id,role_id) SELECT %s,id FROM roles WHERE code='user'", (user["id"],))
                cur.execute("INSERT INTO auth_identities(user_id,provider,provider_subject) VALUES (%s,'password',%s)",
                            (user["id"], username))
                cur.execute("""INSERT INTO legal_acceptances(user_id,terms_version,privacy_version,source)
                               VALUES (%s,%s,%s,'password_signup')""", (user["id"],terms,privacy))
                token, expires = _create_session(conn, user["id"])
        _set_cookie(response, token, expires)
        return {"authenticated":True,"expires_at":expires,"user":{k:str(v) if k=="id" else v for k,v in user.items()}}
    except psycopg.errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="username is already in use")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

@app.post("/auth/login")
def login(body: PasswordLoginBody, response: Response, conn=Depends(get_conn)):
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT id,password_hash,status FROM users WHERE lower(username)=lower(%s)", (body.username.strip(),))
            user = cur.fetchone()
            candidate = user["password_hash"] if user and user["password_hash"] else _DUMMY_PASSWORD_HASH
            valid = verify_password(candidate, body.password)
            if not user or user["status"] != "active" or not user["password_hash"] or not valid:
                raise HTTPException(status_code=401, detail="invalid username or password")
            token, expires = _create_session(conn, user["id"])
            cur.execute("UPDATE users SET last_login_at=now(),updated_at=now() WHERE id=%s", (user["id"],))
    _set_cookie(response, token, expires)
    return {"authenticated":True,"expires_at":expires}

def _identity_hash_otp(phone: str, purpose: str, otp: str) -> str:
    key = os.getenv("IDENTITY_OTP_HMAC_SECRET", "").encode("utf-8")
    if len(key) < 32:
        raise HTTPException(status_code=503, detail="Identity OTP protection is not configured")
    return hmac.new(key, f"{phone}|{purpose}|{otp}".encode("utf-8"), hashlib.sha256).hexdigest()

def _identity_verify_otp(expected: str, phone: str, purpose: str, otp: str) -> bool:
    return hmac.compare_digest(expected, _identity_hash_otp(phone, purpose, otp))

def _otp_enqueue(conn, phone: str, purpose: str, terms: str | None, privacy: str | None):
    if purpose not in {"signup","login"}:
        raise ValueError("unsupported OTP purpose")
    otp = new_otp()
    digest = _identity_hash_otp(phone,purpose,otp)
    expires = __import__("datetime").datetime.now(__import__("datetime").timezone.utc) + OTP_TTL
    try:
        cipher = Fernet(os.getenv("IDENTITY_OUTBOX_FERNET_KEY","").encode("ascii"))
    except Exception:
        raise HTTPException(status_code=503, detail="Identity SMS encryption is not configured") from None
    encrypted_phone = cipher.encrypt(phone.encode()).decode("ascii")
    encrypted_payload = cipher.encrypt(json.dumps({"otp":otp,"purpose":purpose}).encode()).decode("ascii")
    tag = __import__("hashlib").sha256(phone.encode()).hexdigest()[:20]
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s::text,0::bigint))",(phone,))
            cur.execute("""SELECT count(*) AS n FROM phone_verifications
                           WHERE phone_e164=%s AND created_at>now()-interval '15 minutes'""",(phone,))
            if cur.fetchone()["n"] >= 3:
                raise ValueError("too many verification requests; try again later")
            cur.execute("UPDATE phone_verifications SET consumed_at=now() WHERE phone_e164=%s AND purpose=%s AND consumed_at IS NULL",
                        (phone,purpose))
            cur.execute("""INSERT INTO phone_verifications(phone_e164,purpose,otp_hash,expires_at,
                           signup_terms_version,signup_privacy_version)
                           VALUES (%s,%s,%s,%s,%s,%s)""",
                        (phone,purpose,digest,expires,terms,privacy))
            cur.execute("""INSERT INTO notification_outbox(channel,template_code,destination_token,payload,idempotency_key)
                           VALUES ('sms','phone_otp',%s,jsonb_build_object('encrypted_payload',%s::text),%s)""",
                        (encrypted_phone,encrypted_payload,f"otp:{tag}:{purpose}:{digest[:16]}"))

@app.post("/auth/phone/request")
def phone_request(body: OtpBody, conn=Depends(get_conn)):
    try:
        phone = normalize_phone(body.phone)
        terms = privacy = None
        if body.purpose == "signup":
            terms, privacy = _legal_versions(body.terms_accepted,body.terms_version,body.privacy_version)
        elif body.terms_accepted or body.terms_version or body.privacy_version:
            raise ValueError("legal versions are accepted only for signup")
        _otp_enqueue(conn,phone,body.purpose,terms,privacy)
    except ValueError as exc:
        raise HTTPException(status_code=422,detail=str(exc))
    return {"accepted":True,"delivery":"queued","expires_in_seconds":300,
            "terms_version":terms,"privacy_version":privacy}

@app.post("/auth/phone/verify")
def phone_verify(body: OtpVerifyBody, response: Response, conn=Depends(get_conn)):
    phone = normalize_phone(body.phone)
    terms = privacy = None
    if body.purpose == "signup":
        terms, privacy = _legal_versions(body.terms_accepted,body.terms_version,body.privacy_version)
    invalid = False
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("""SELECT id,otp_hash,attempts,signup_terms_version,signup_privacy_version
                           FROM phone_verifications WHERE phone_e164=%s AND purpose=%s
                             AND consumed_at IS NULL AND expires_at>now()
                           ORDER BY created_at DESC LIMIT 1 FOR UPDATE""",(phone,body.purpose))
            challenge = cur.fetchone()
            if not challenge or challenge["attempts"] >= 5:
                invalid = True
            elif body.purpose == "signup" and (
                challenge["signup_terms_version"] != terms or challenge["signup_privacy_version"] != privacy
            ):
                invalid = True
            elif not _identity_verify_otp(challenge["otp_hash"],phone,body.purpose,body.otp):
                cur.execute("UPDATE phone_verifications SET attempts=attempts+1 WHERE id=%s",(challenge["id"],))
                invalid = True
            else:
                cur.execute("UPDATE phone_verifications SET consumed_at=now() WHERE id=%s",(challenge["id"],))
                cur.execute("SELECT id,status FROM users WHERE phone_e164=%s FOR UPDATE",(phone,))
                user = cur.fetchone()
                if body.purpose == "signup" and user:
                    invalid = True
                elif user and user["status"] != "active":
                    invalid = True
                elif not user and body.purpose != "signup":
                    invalid = True
                else:
                    if not user:
                        cur.execute("""INSERT INTO users(public_id,phone_e164,phone_verified_at,last_login_at)
                                       VALUES (%s,%s,now(),now()) RETURNING id""",
                                    ("usr_"+secrets.token_hex(12),phone))
                        user_id = cur.fetchone()["id"]
                        cur.execute("INSERT INTO auth_identities(user_id,provider,provider_subject) VALUES (%s,'phone_otp',%s)",
                                    (user_id,phone))
                        cur.execute("INSERT INTO user_roles(user_id,role_id) SELECT %s,id FROM roles WHERE code='user'",(user_id,))
                        cur.execute("""INSERT INTO legal_acceptances(user_id,terms_version,privacy_version,source)
                                       VALUES (%s,%s,%s,'phone_otp_signup')""",(user_id,terms,privacy))
                    else:
                        user_id = user["id"]
                        cur.execute("UPDATE users SET phone_verified_at=COALESCE(phone_verified_at,now()),last_login_at=now() WHERE id=%s",(user_id,))
                    bootstrap = os.getenv("IDENTITY_SUPERADMIN_PHONE_E164","").strip()
                    if bootstrap and hmac.compare_digest(phone,bootstrap):
                        cur.execute("INSERT INTO user_roles(user_id,role_id) SELECT %s,id FROM roles WHERE code='superadmin' ON CONFLICT DO NOTHING",(user_id,))
                        cur.execute("""INSERT INTO admin_audit_logs(actor_user_id,action,target_type,target_id,after_state)
                                       VALUES (NULL,'identity.bootstrap_superadmin.granted','user',%s,'{"role":"superadmin"}'::jsonb)""",(str(user_id),))
                    token, expires = _create_session(conn,user_id)
    if invalid:
        raise HTTPException(status_code=401,detail="invalid or expired verification code")
    _set_cookie(response,token,expires)
    principal = _principal(conn, token)
    user = dict(principal["user"]) if principal else {"id":str(user_id), "phone_e164":phone}
    user["roles"] = principal["roles"] if principal else ["user"]
    role = _primary_role(user["roles"])
    user = {k:(str(v) if isinstance(v,UUID) else v) for k,v in user.items()}
    return {"authenticated":True,"expires_at":expires,"role":role,"user":user}

def _primary_role(roles: list[str]) -> str:
    for role in ("superadmin", "admin", "support", "data_manager", "user"):
        if role in roles:
            return role
    return "user"

@app.get("/auth/me")
def me(principal=Depends(_current_user)):
    user = dict(principal["user"])
    user["roles"] = principal["roles"]
    user["is_superadmin"] = "superadmin" in principal["roles"]
    user["mfa_enabled"] = principal["mfa_enabled"]
    user["mfa_verified"] = principal["mfa_verified_at"] is not None
    user = {k:(str(v) if isinstance(v,UUID) else v) for k,v in user.items()}
    return {"role":_primary_role(principal["roles"]), "user":user,
            "roles":principal["roles"], "permissions":principal["permissions"],
            "mfa_enabled":principal["mfa_enabled"],
            "mfa_verified":principal["mfa_verified_at"] is not None}

@app.post("/auth/logout")
def logout(request: Request, response: Response,
           credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
           conn=Depends(get_conn)):
    if request.cookies.get(COOKIE_NAME) and credentials is None:
        _csrf(request)
    token = _request_token(request,credentials)
    if token:
        with conn.cursor() as cur:
            cur.execute("UPDATE sessions SET revoked_at=now() WHERE token_hash=%s AND revoked_at IS NULL",(hash_session_token(token),))
    response.delete_cookie(COOKIE_NAME,path="/",httponly=True,secure=COOKIE_SECURE,samesite="lax")
    return {"authenticated":False}

@app.get("/auth/mfa/status")
def mfa_status(principal=Depends(_current_user),conn=Depends(get_conn)):
    _privileged(principal)
    return {"enabled":principal["mfa_enabled"],"verified_for_session":principal["mfa_verified_at"] is not None}

@app.post("/auth/mfa/setup")
def mfa_setup(principal=Depends(_current_user),conn=Depends(get_conn)):
    _privileged(principal)
    secret = new_totp_secret()
    encrypted = _mfa_cipher().encrypt(secret.encode()).decode("ascii")
    user_id = principal["user"]["id"]
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT enabled_at FROM user_admin_mfa WHERE user_id=%s FOR UPDATE",(user_id,))
            row = cur.fetchone()
            if row and row["enabled_at"] is not None:
                raise HTTPException(status_code=409,detail="MFA is already enabled; use the verified recovery process to reset it")
            cur.execute("""INSERT INTO user_admin_mfa(user_id,encrypted_secret,enabled_at,last_verified_counter,updated_at)
                           VALUES (%s,%s,NULL,NULL,now()) ON CONFLICT(user_id) DO UPDATE
                           SET encrypted_secret=EXCLUDED.encrypted_secret,enabled_at=NULL,last_verified_counter=NULL,updated_at=now()""",
                        (user_id,encrypted))
    account = principal["user"].get("phone_e164") or principal["user"].get("username") or str(user_id)
    return {"secret":secret,"otpauth_uri":provisioning_uri(secret,account=account),
            "issuer":"Tloomberg","period_seconds":30,"digits":6}

def _consume_mfa(conn,principal,code:str,secret:str,previous):
    counter = matching_counter(secret,code,window=1)
    if counter is None:
        raise HTTPException(status_code=401,detail="invalid or expired MFA code")
    if previous is not None and counter <= int(previous):
        raise HTTPException(status_code=409,detail="MFA code has already been used")
    with conn.cursor() as cur:
        cur.execute("""UPDATE user_admin_mfa SET last_verified_counter=%s,updated_at=now()
                       WHERE user_id=%s AND (last_verified_counter IS NULL OR last_verified_counter<%s)
                       RETURNING user_id""",(counter,principal["user"]["id"],counter))
        if cur.fetchone() is None:
            raise HTTPException(status_code=409,detail="MFA code has already been used")
        cur.execute("""UPDATE sessions SET mfa_verified_at=now()
                       WHERE id=%s AND user_id=%s AND revoked_at IS NULL AND expires_at>now()
                       RETURNING id""",(principal["session_id"],principal["user"]["id"]))
        if cur.fetchone() is None:
            raise HTTPException(status_code=401,detail="session is no longer active")

def _mfa_change(conn,principal,code,enable:bool):
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT encrypted_secret,enabled_at,last_verified_counter FROM user_admin_mfa WHERE user_id=%s FOR UPDATE",
                        (principal["user"]["id"],))
            row=cur.fetchone()
        if not row or (enable and row["enabled_at"] is not None) or (not enable and row["enabled_at"] is None):
            raise HTTPException(status_code=409,detail="MFA setup state is invalid")
        try:
            secret=_mfa_cipher().decrypt(row["encrypted_secret"].encode()).decode()
        except Exception:
            raise HTTPException(status_code=503,detail="MFA secret cannot be decrypted") from None
        _consume_mfa(conn,principal,code,secret,row["last_verified_counter"])
        if enable:
            with conn.cursor() as cur:
                cur.execute("UPDATE user_admin_mfa SET enabled_at=now(),updated_at=now() WHERE user_id=%s",(principal["user"]["id"],))

@app.post("/auth/mfa/confirm")
def mfa_confirm(body:MfaCodeBody,principal=Depends(_current_user),conn=Depends(get_conn)):
    _privileged(principal)
    _mfa_change(conn,principal,body.code,True)
    return {"enabled":True,"verified_for_session":True}

@app.post("/auth/mfa/verify")
def mfa_verify(body:MfaCodeBody,principal=Depends(_current_user),conn=Depends(get_conn)):
    _privileged(principal)
    _mfa_change(conn,principal,body.code,False)
    return {"verified":True,"verified_at":"session-bound"}

@app.post("/internal/introspect",dependencies=[Depends(_service_auth)])
def introspect(body:InternalTokenBody,conn=Depends(get_conn)):
    with conn.transaction():
        value = _principal(conn,body.token)
    if not value:
        raise HTTPException(status_code=401,detail="invalid or expired user session")
    return {"principal":value}

@app.get("/internal/sessions",dependencies=[Depends(_service_auth)])
def list_sessions(request:Request,conn=Depends(get_conn),
                  x_identity_user_token:str|None=Header(default=None,alias="X-Identity-User-Token")):
    owner=_principal(conn,x_identity_user_token or "")
    if not owner:
        raise HTTPException(status_code=401,detail="user session is invalid")
    with conn.cursor() as cur:
        cur.execute("""SELECT id,created_at,last_seen_at,expires_at,user_agent,(id=%s) AS is_current
                       FROM sessions WHERE user_id=%s AND revoked_at IS NULL AND expires_at>now()
                       ORDER BY last_seen_at DESC""",(owner["session_id"],owner["user"]["id"]))
        rows=cur.fetchall()
    return {"sessions":rows}

@app.delete("/internal/sessions/{session_id}",dependencies=[Depends(_service_auth)])
def revoke_one_session(session_id:UUID,conn=Depends(get_conn),
                       x_identity_user_token:str|None=Header(default=None,alias="X-Identity-User-Token")):
    owner=_principal(conn,x_identity_user_token or "")
    if not owner:
        raise HTTPException(status_code=401,detail="user session is invalid")
    if str(owner["session_id"])==str(session_id):
        raise HTTPException(status_code=409,detail="use logout to revoke the current session")
    with conn.cursor() as cur:
        cur.execute("""UPDATE sessions SET revoked_at=now()
                       WHERE id=%s AND user_id=%s AND revoked_at IS NULL RETURNING id""",
                    (session_id,owner["user"]["id"]))
        changed=cur.fetchone()
    if not changed:
        raise HTTPException(status_code=404,detail="active session not found")
    return {"revoked":True}

@app.post("/internal/sessions/revoke-others",dependencies=[Depends(_service_auth)])
def revoke_other_sessions(conn=Depends(get_conn),
                          x_identity_user_token:str|None=Header(default=None,alias="X-Identity-User-Token")):
    owner=_principal(conn,x_identity_user_token or "")
    if not owner:
        raise HTTPException(status_code=401,detail="user session is invalid")
    with conn.cursor() as cur:
        cur.execute("""UPDATE sessions SET revoked_at=now()
                       WHERE user_id=%s AND id<>%s AND revoked_at IS NULL RETURNING id""",
                    (owner["user"]["id"],owner["session_id"]))
        count=len(cur.fetchall())
    return {"revoked_count":count}

@app.patch("/internal/users/me/profile",dependencies=[Depends(_service_auth)])
def update_own_profile(body:ProfileBody,conn=Depends(get_conn),
                       x_identity_user_token:str|None=Header(default=None,alias="X-Identity-User-Token")):
    owner=_principal(conn,x_identity_user_token or "")
    if not owner:
        raise HTTPException(status_code=401,detail="user session is invalid")
    with conn.cursor() as cur:
        cur.execute("""UPDATE users SET display_name=%s,updated_at=now()
                       WHERE id=%s RETURNING id,public_id,username,display_name,account_type,status,
                       phone_e164,phone_verified_at,email,email_verified_at,created_at,updated_at,last_login_at""",
                    (body.display_name,owner["user"]["id"]))
        user=cur.fetchone()
    return {"principal":{"user":user}}

@app.post("/internal/users/{user_id}/status",dependencies=[Depends(_service_auth)])
def change_user_status(user_id:UUID,body:UserStatusBody,conn=Depends(get_conn),
                       x_identity_user_token:str|None=Header(default=None,alias="X-Identity-User-Token")):
    actor=_service_principal(conn,x_identity_user_token,"users.manage")
    actor_id=actor["user"]["id"]
    if actor_id==user_id:
        raise HTTPException(status_code=409,detail="self status changes are not allowed")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM users WHERE id=%s FOR UPDATE",(user_id,))
            before=cur.fetchone()
            if not before:
                raise HTTPException(status_code=404,detail="user not found")
            if before["status"] not in {"active","suspended"}:
                raise HTTPException(status_code=409,detail="deleted or pending accounts cannot be reactivated")
            cur.execute("""SELECT EXISTS(SELECT 1 FROM user_roles ur JOIN roles r ON r.id=ur.role_id
                           WHERE ur.user_id=%s AND r.code='superadmin') AS is_superadmin""",(user_id,))
            is_root=cur.fetchone()["is_superadmin"]
            if is_root and body.status=="suspended":
                cur.execute("""SELECT u.id FROM users u JOIN user_roles ur ON ur.user_id=u.id
                               JOIN roles r ON r.id=ur.role_id WHERE r.code='superadmin'
                               ORDER BY u.id FOR UPDATE OF u""")
                cur.fetchall()
                cur.execute("""SELECT count(*)::integer AS n FROM users u
                               JOIN user_roles ur ON ur.user_id=u.id JOIN roles r ON r.id=ur.role_id
                               WHERE u.status='active' AND r.code='superadmin'""")
                if cur.fetchone()["n"]<=1:
                    raise HTTPException(status_code=409,detail="the last active superadmin cannot be suspended")
            cur.execute("UPDATE users SET status=%s,updated_at=now() WHERE id=%s",(body.status,user_id))
            if body.status=="suspended":
                cur.execute("UPDATE sessions SET revoked_at=now() WHERE user_id=%s AND revoked_at IS NULL",(user_id,))
            _audit(conn,actor_id,"identity.user.status",str(user_id),{"status":body.status,"reason":body.reason})
    return {"principal":_principal_for_user(conn,user_id)}

def _principal_for_user(conn,user_id:UUID):
    with conn.cursor() as cur:
        cur.execute("""SELECT id,public_id,username,display_name,account_type,status,phone_e164,
                       phone_verified_at,email,email_verified_at,created_at,updated_at,last_login_at
                       FROM users WHERE id=%s""",(user_id,))
        user=cur.fetchone()
    if not user:
        return None
    roles,permissions=_user_roles_permissions(conn,user_id)
    with conn.cursor() as cur:
        cur.execute("SELECT enabled_at FROM user_admin_mfa WHERE user_id=%s",(user_id,))
        mfa=cur.fetchone()
    return {"user":user,"roles":roles,"permissions":permissions,
            "mfa_enabled":bool(mfa and mfa["enabled_at"] is not None)}

@app.put("/internal/users/{user_id}/roles/{role_code}",dependencies=[Depends(_service_auth)])
def change_user_role(user_id:UUID,role_code:str,body:RoleBody,conn=Depends(get_conn),
                     x_identity_user_token:str|None=Header(default=None,alias="X-Identity-User-Token")):
    actor=_service_principal(conn,x_identity_user_token,"roles.manage")
    actor_id=actor["user"]["id"]
    if actor_id==user_id:
        raise HTTPException(status_code=409,detail="self role changes are not allowed")
    if role_code not in {"admin","support"}:
        raise HTTPException(status_code=409,detail="only admin and support roles can be managed")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM users WHERE id=%s FOR UPDATE",(user_id,))
            target=cur.fetchone()
            if not target:
                raise HTTPException(status_code=404,detail="user not found")
            if body.grant and target["status"]!="active":
                raise HTTPException(status_code=409,detail="roles can only be granted to active users")
            cur.execute("SELECT id FROM roles WHERE code=%s",(role_code,))
            role=cur.fetchone()
            if body.grant:
                cur.execute("INSERT INTO user_roles(user_id,role_id,granted_by) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                            (user_id,role["id"],actor_id))
            else:
                cur.execute("DELETE FROM user_roles WHERE user_id=%s AND role_id=%s",(user_id,role["id"]))
            _audit(conn,actor_id,"identity.user.role",str(user_id),{"role":role_code,"granted":body.grant})
    return {"principal":_principal_for_user(conn,user_id)}

@app.get("/internal/permissions",dependencies=[Depends(_service_auth)])
def permission_catalog(conn=Depends(get_conn)):
    with conn.cursor() as cur:
        cur.execute("SELECT code,description FROM permissions ORDER BY code")
        return {"permissions":cur.fetchall()}
