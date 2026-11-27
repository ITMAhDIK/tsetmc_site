"""Private Identity adapter for authenticated Product Analytics requests.

Analytics never queries Identity tables directly. Every request is introspected
against Identity using a dedicated service token and the caller's session token.
"""
from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

COOKIE_NAME = "tloomberg_session"
bearer = HTTPBearer(auto_error=False)


class IdentityUnavailable(RuntimeError):
    pass


def _request_identity(token: str) -> dict:
    if not isinstance(token, str) or not token or len(token) > 256:
        raise IdentityUnavailable("invalid session token")
    base_url = os.getenv("IDENTITY_SERVICE_URL", "").strip().rstrip("/")
    service_token = os.getenv("IDENTITY_SERVICE_TOKEN", "").strip()
    if not base_url or len(service_token) < 32:
        raise IdentityUnavailable("private Identity connection is not configured")
    request = Request(
        base_url + "/internal/introspect",
        data=json.dumps({"token": token}, separators=(",", ":")).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Identity-Service-Token": service_token,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=3.0) as response:
            raw = response.read(64 * 1024 + 1)
    except HTTPError as exc:
        if exc.code == 401 or exc.code == 400:
            raise IdentityUnavailable("session is invalid or expired") from None
        if exc.code >= 500 or exc.code in {403, 404, 408, 429}:
            raise IdentityUnavailable("Identity service is unavailable") from None
        raise IdentityUnavailable("Identity service rejected the request") from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise IdentityUnavailable("Identity service is unavailable") from None
    if len(raw) > 64 * 1024:
        raise IdentityUnavailable("Identity response exceeded its size limit")
    try:
        payload = json.loads(raw)
        principal = payload["principal"]
        user = principal["user"]
        user_id = UUID(str(user["id"]))
        session_id = UUID(str(principal["session_id"]))
        permissions = principal["permissions"]
        roles = principal["roles"]
        if user.get("status") != "active":
            raise ValueError("inactive user")
        if not isinstance(permissions, list) or not all(
            isinstance(value, str) for value in permissions
        ):
            raise ValueError("invalid permissions")
        if not isinstance(roles, list) or not all(
            isinstance(value, str) for value in roles
        ):
            raise ValueError("invalid roles")
        mfa_enabled = principal.get("mfa_enabled")
        if not isinstance(mfa_enabled, bool):
            raise ValueError("invalid MFA state")
        return {
            "id": user_id,
            "session_id": session_id,
            "user": user,
            "roles": roles,
            "permissions": permissions,
            "mfa_enabled": mfa_enabled,
            "mfa_verified_at": principal.get("mfa_verified_at"),
        }
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise IdentityUnavailable("Identity returned an invalid principal") from None


def current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> dict:
    token = credentials.credentials if credentials else request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="authentication required")
    try:
        return _request_identity(token)
    except IdentityUnavailable as exc:
        status = 503 if "unavailable" in str(exc) or "not configured" in str(exc) else 401
        raise HTTPException(status_code=status, detail=str(exc)) from None
