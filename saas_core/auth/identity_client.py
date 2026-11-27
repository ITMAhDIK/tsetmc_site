"""Private HTTP client and user projection adapter for the Identity Service."""
from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID
from datetime import datetime


class IdentityUnavailable(RuntimeError):
    """Identity could not be reached or its response could not be trusted."""


class IdentityUnauthorized(RuntimeError):
    """Identity rejected the user session token."""

class IdentityRequestError(RuntimeError):
    """Identity rejected a validly authenticated internal request."""
    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__("Identity request was rejected")


def identity_enabled() -> bool:
    return bool(os.getenv("IDENTITY_SERVICE_URL", "").strip())


def _request(path: str, *, payload: dict | None = None, user_token: str | None = None,
             method: str = "POST", timeout: float = 3.0) -> dict:
    base = os.getenv("IDENTITY_SERVICE_URL", "").strip().rstrip("/")
    service_token = os.getenv("IDENTITY_SERVICE_TOKEN", "").strip()
    if not base or len(service_token) < 32:
        raise IdentityUnavailable("Identity service is not configured")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Identity-Service-Token": service_token,
    }
    if user_token:
        headers["X-Identity-User-Token"] = user_token
    body = json.dumps(payload or {}, separators=(",", ":")).encode("utf-8")
    request = Request(base + path, data=body if method not in {"GET", "HEAD"} else None,
                      headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(256 * 1024)
            value = json.loads(raw.decode("utf-8")) if raw else {}
    except HTTPError as exc:
        if exc.code == 401:
            raise IdentityUnauthorized("Identity rejected the request") from None
        if exc.code in {403, 404, 409, 422}:
            raise IdentityRequestError(exc.code) from None
        raise IdentityUnavailable("Identity returned an error") from None
    except (URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError):
        raise IdentityUnavailable("Identity is temporarily unavailable") from None
    if not isinstance(value, dict):
        raise IdentityUnavailable("Identity returned an invalid response")
    return value


def identity_health() -> dict:
    return _request("/health", method="GET")

def introspect(token: str) -> dict:
    if not token or len(token) > 256:
        raise IdentityUnauthorized("Authentication required")
    response = _request("/internal/introspect", payload={"token": token})
    principal = response.get("principal")
    if not isinstance(principal, dict) or not isinstance(principal.get("user"), dict):
        raise IdentityUnavailable("Identity returned an invalid principal")
    principal["_identity_token"] = token
    return principal


def user_action(path: str, *, user_token: str, payload: dict | None = None,
                method: str = "POST") -> dict:
    return _request(path, payload=payload, user_token=user_token, method=method)


def project_identity_user(conn, principal: dict) -> dict:
    """Synchronize only non-secret identity facts into SaaS for local foreign keys."""
    user = principal.get("user")
    if not isinstance(user, dict):
        raise IdentityUnavailable("Identity user projection is missing")
    user_id = UUID(str(user["id"]))
    def _timestamp(value):
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value
    normalized_user = dict(user)
    for key in ("phone_verified_at", "email_verified_at", "created_at", "updated_at", "last_login_at"):
        normalized_user[key] = _timestamp(normalized_user.get(key))
    principal["user"] = normalized_user
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO users(
                       id,public_id,username,display_name,account_type,status,
                       phone_e164,phone_verified_at,email,email_verified_at,password_hash,
                       created_at,updated_at,last_login_at
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s)
                   ON CONFLICT(id) DO UPDATE SET
                       public_id=EXCLUDED.public_id, username=EXCLUDED.username,
                       display_name=EXCLUDED.display_name, account_type=EXCLUDED.account_type,
                       status=EXCLUDED.status, phone_e164=EXCLUDED.phone_e164,
                       phone_verified_at=EXCLUDED.phone_verified_at, email=EXCLUDED.email,
                       email_verified_at=EXCLUDED.email_verified_at, password_hash=NULL,
                       updated_at=EXCLUDED.updated_at, last_login_at=EXCLUDED.last_login_at""",
                (user_id, user["public_id"], user.get("username"), user.get("display_name"),
                 user.get("account_type", "standard"), user.get("status", "active"),
                 user.get("phone_e164"), _timestamp(user.get("phone_verified_at")), user.get("email"),
                 _timestamp(user.get("email_verified_at")), _timestamp(user.get("created_at")),
                 _timestamp(user.get("updated_at")), _timestamp(user.get("last_login_at"))),
            )
            cur.execute("INSERT INTO user_preferences(user_id) VALUES (%s) ON CONFLICT DO NOTHING", (user_id,))
            cur.execute("INSERT INTO wallet_accounts(user_id) VALUES (%s) ON CONFLICT DO NOTHING", (user_id,))
            roles = [str(role) for role in principal.get("roles", [])]
            cur.execute(
                """DELETE FROM user_roles ur USING roles r
                   WHERE ur.role_id=r.id AND ur.user_id=%s AND NOT (r.code = ANY(%s::text[]))""",
                (user_id, roles),
            )
            for role in roles:
                cur.execute(
                    """INSERT INTO user_roles(user_id,role_id)
                       SELECT %s,id FROM roles WHERE code=%s ON CONFLICT DO NOTHING""",
                    (user_id, role),
                )
    # Preserve the legacy flat principal contract while keeping the nested
    # Identity document available to account-projection and audit consumers.
    for key, value in normalized_user.items():
        if key == "id":
            principal[key] = user_id
        else:
            principal.setdefault(key, value)
    principal["id"] = user_id
    principal["mfa_verified_at"] = _timestamp(principal.get("mfa_verified_at"))
    principal["mfa_enabled"] = bool(principal.get("mfa_enabled"))
    return principal


def sync_target_projection(conn, result: dict) -> None:
    target = result.get("principal")
    if isinstance(target, dict):
        project_identity_user(conn, target)
