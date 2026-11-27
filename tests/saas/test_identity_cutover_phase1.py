from __future__ import annotations

from uuid import uuid4
import pytest
from fastapi import HTTPException
from starlette.requests import Request
from fastapi.security import HTTPAuthorizationCredentials

import saas_core.auth.api as auth_api
from saas_core.auth.authorization import has_permission, has_entitlement
from saas_core.auth.identity_client import IdentityUnavailable


def _request(method="GET"):
    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": "/account/profile",
        "raw_path": b"/account/profile",
        "query_string": b"",
        "headers": [],
        "server": ("saas.example.com", 443),
        "client": ("127.0.0.1", 10000),
    })


def test_current_user_uses_identity_principal_when_cutover_enabled(monkeypatch):
    monkeypatch.setenv("IDENTITY_SERVICE_URL", "http://identity-api:8000")
    principal = {
        "id": uuid4(),
        "user": {"id": str(uuid4()), "status": "active"},
        "roles": ["user"],
        "permissions": ["analytics.read"],
        "mfa_enabled": False,
    }
    monkeypatch.setattr(auth_api, "identity_introspect", lambda token: {**principal, "_identity_token": token})
    monkeypatch.setattr(auth_api, "project_identity_user", lambda conn, value: value)
    monkeypatch.setattr(auth_api, "authenticate_session", lambda *args: (_ for _ in ()).throw(AssertionError("local fallback used")))
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="opaque-session-token")
    result = auth_api.current_user(_request(), credentials, object())
    assert result["_identity_token"] == "opaque-session-token"
    assert result["permissions"] == ["analytics.read"]


def test_identity_outage_fails_closed_without_legacy_session_fallback(monkeypatch):
    monkeypatch.setenv("IDENTITY_SERVICE_URL", "http://identity-api:8000")
    def unavailable(_token):
        raise IdentityUnavailable("offline")
    monkeypatch.setattr(auth_api, "identity_introspect", unavailable)
    monkeypatch.setattr(auth_api, "authenticate_session", lambda *args: (_ for _ in ()).throw(AssertionError("unsafe fallback")))
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="opaque-session-token")
    with pytest.raises(HTTPException) as error:
        auth_api.current_user(_request(), credentials, object())
    assert error.value.status_code == 503


def test_identity_permissions_override_any_local_role_projection():
    principal = {"permissions": ["crm.read"], "roles": ["user"]}
    assert has_permission(None, uuid4(), "crm.read", principal=principal) is True
    assert has_permission(None, uuid4(), "users.manage", principal=principal) is False


def test_identity_superadmin_claim_does_not_need_local_role_lookup_for_entitlement():
    assert has_entitlement(None, uuid4(), "premium_analytics",
                           principal={"roles": ["superadmin"], "permissions": []}) is True


def test_unknown_permission_and_entitlement_are_not_granted():
    with pytest.raises(ValueError):
        has_entitlement(None, uuid4(), "unregistered_feature",
                        principal={"roles": [], "permissions": []})
