"""Identity-backed authentication for the independently deployed Commerce API.

Commerce never reads Identity tables. It validates the opaque session through
Identity's private introspection contract and stores only a minimal user projection.
"""
from __future__ import annotations

import hmac
import json
import os
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import UUID

from fastapi import Depends, HTTPException, Request as FastAPIRequest
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from commerce_service.db import get_conn

COOKIE_NAME = "tloomberg_session"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_bearer = HTTPBearer(auto_error=False)


class IdentityUnavailable(RuntimeError):
    pass


class IdentityUnauthorized(RuntimeError):
    pass


def _request_identity(token: str) -> dict:
    base = os.getenv("IDENTITY_SERVICE_URL", "").strip().rstrip("/")
    service_token = os.getenv("IDENTITY_SERVICE_TOKEN", "").strip()
    if not base or len(service_token) < 32:
        raise IdentityUnavailable("Identity service is not configured")
    request = Request(
        base + "/internal/introspect",
        data=json.dumps({"token": token}, separators=(",", ":")).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Identity-Service-Token": service_token,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=3.0) as response:
            payload = json.loads(response.read(256 * 1024).decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 401:
            raise IdentityUnauthorized from None
        raise IdentityUnavailable("Identity request failed") from None
    except (URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError):
        raise IdentityUnavailable("Identity service is unavailable") from None
    principal = payload.get("principal") if isinstance(payload, dict) else None
    if not isinstance(principal, dict) or not isinstance(principal.get("user"), dict):
        raise IdentityUnavailable("Identity returned an invalid principal")
    return principal


def _origin(request: FastAPIRequest) -> str | None:
    raw = request.headers.get("origin")
    if not raw:
        referer = request.headers.get("referer")
        if not referer:
            return None
        parsed = urlsplit(referer)
        if not parsed.scheme or not parsed.netloc:
            return None
        raw = f"{parsed.scheme}://{parsed.netloc}"
    parsed = urlsplit(raw.strip())
    if (parsed.scheme not in {"http", "https"} or not parsed.netloc
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or parsed.username or parsed.password):
        return None
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def _check_cookie_csrf(request: FastAPIRequest) -> None:
    origin = _origin(request)
    own = f"{request.url.scheme.lower()}://{request.url.netloc.lower()}"
    allowed = {
        value.strip().rstrip("/").lower()
        for value in os.getenv("COMMERCE_ALLOWED_ORIGINS", "").split(",")
        if value.strip()
    }
    if not origin or (origin != own and origin not in allowed):
        raise HTTPException(status_code=403, detail="request origin is not allowed")


def current_user(
    request: FastAPIRequest,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    conn=Depends(get_conn),
):
    cookie_token = request.cookies.get(COOKIE_NAME)
    if cookie_token and credentials is None and request.method.upper() not in _SAFE_METHODS:
        _check_cookie_csrf(request)
    token = (credentials.credentials if credentials else None) or cookie_token
    if not token or len(token) > 256:
        raise HTTPException(status_code=401, detail="authentication required")
    try:
        principal = _request_identity(token)
    except IdentityUnauthorized:
        raise HTTPException(status_code=401, detail="authentication required") from None
    except IdentityUnavailable:
        raise HTTPException(status_code=503, detail="Identity service is temporarily unavailable") from None
    user = principal["user"]
    try:
        user_id = UUID(str(user["id"]))
    except (KeyError, ValueError, TypeError):
        raise HTTPException(status_code=503, detail="Identity returned an invalid user identity") from None
    if user.get("status") != "active":
        raise HTTPException(status_code=403, detail="active account required")
    phone_verified_at = user.get("phone_verified_at")
    if isinstance(phone_verified_at, str):
        try:
            phone_verified_at = datetime.fromisoformat(phone_verified_at.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=503, detail="Identity returned invalid verification metadata") from None
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO users(id,status,phone_e164,phone_verified_at)
                   VALUES (%s,%s,%s,%s)
                   ON CONFLICT(id) DO UPDATE SET
                     status=EXCLUDED.status, phone_e164=EXCLUDED.phone_e164,
                     phone_verified_at=EXCLUDED.phone_verified_at""",
                (user_id, user["status"], user.get("phone_e164"), phone_verified_at),
            )
            cur.execute(
                "INSERT INTO wallet_accounts(user_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (user_id,),
            )
    principal["id"] = user_id
    principal["user"]["id"] = user_id
    return principal
