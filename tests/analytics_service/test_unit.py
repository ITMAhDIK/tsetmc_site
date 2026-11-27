from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from analytics_service.service import (
    ALLOWED_EVENT_NAMES, normalize_page_path, validate_event,
)


def event(**overrides):
    value = {
        "client_event_id": uuid4(),
        "event_name": "page_view",
        "page_path": "/stocks/123456/details",
        "component_id": None,
        "element_role": None,
        "viewport_width": 1440,
        "viewport_height": 900,
        "scroll_depth_pct": None,
        "active_duration_ms": None,
        "x_pct": None,
        "y_pct": None,
    }
    value.update(overrides)
    return value


def test_event_vocabulary_is_closed():
    assert ALLOWED_EVENT_NAMES == {
        "page_view", "click", "navigation", "scroll", "active_time", "feature_view"
    }


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/", "/"),
        ("/dashboard", "/dashboard"),
        ("/stocks/:id", "/stocks/:id"),
        ("/stocks/123456/details", "/stocks/:id/details"),
        ("/investors/550e8400-e29b-41d4-a716-446655440000", "/investors/:id"),
        ("/profile/john-doe", "/profile/:id"),
        ("/companies/Foolad", "/companies/:id"),
    ],
)
def test_path_normalization(path, expected):
    assert normalize_page_path(path) == expected


def test_path_normalization_is_idempotent_for_redacted_routes():
    normalized = normalize_page_path("/stocks/123456/details")
    assert normalize_page_path(normalized) == normalized


@pytest.mark.parametrize(
    "path",
    [
        "https://example.com/dashboard",
        "//example.com/dashboard",
        "/account?token=secret",
        "/account#private",
        "/../../secret",
        "/account/user@example.com",
        "/path with spaces",
    ],
)
def test_path_normalization_rejects_personal_or_external_paths(path):
    with pytest.raises(ValueError):
        normalize_page_path(path)


def test_event_validator_drops_unknown_and_sensitive_fields():
    clean = validate_event({
        **event(),
        "password": "secret",
        "typed_text": "private",
        "email": "person@example.com",
        "url": "https://example.com/?token=secret",
    })
    assert clean["page_path"] == "/stocks/:id/details"
    assert not {"password", "typed_text", "email", "url"} & clean.keys()


@pytest.mark.parametrize(
    "override",
    [
        {"event_name": "purchase", "metadata": {"amount": 100}},
        {"event_name": "scroll", "scroll_depth_pct": True},
        {"event_name": "scroll", "scroll_depth_pct": 101},
        {"event_name": "active_time", "active_duration_ms": -1},
        {"event_name": "active_time", "active_duration_ms": 180001},
        {"event_name": "click", "x_pct": 20, "y_pct": None},
        {"event_name": "page_view", "x_pct": 20, "y_pct": 30},
        {"event_name": "page_view", "component_id": "customer@example.com"},
        {"event_name": "page_view", "component_id": "09121234567"},
        {"event_name": "page_view", "viewport_width": True},
    ],
)
def test_event_validator_rejects_invalid_payloads(override):
    with pytest.raises((ValueError, TypeError)):
        validate_event(event(**override))


def test_api_schema_rejects_oversized_batches_and_boolean_measurements():
    from analytics_service.api import EventBatch, EventInput

    with pytest.raises(ValidationError):
        EventBatch(events=[
            EventInput(**event(event_name="page_view")) for _ in range(51)
        ])
    with pytest.raises(ValidationError):
        EventInput(**event(event_name="active_time", active_duration_ms=True))


def test_analytics_service_exposes_expected_routes():
    from analytics_service.api import app

    paths = set(app.openapi()["paths"])
    expected = {
        "/health", "/ready", "/analytics/consent", "/analytics/events",
        "/analytics/admin/overview", "/analytics/admin/pages",
        "/analytics/admin/heatmap", "/analytics/admin/funnel",
        "/analytics/admin/retention", "/analytics/admin/paths",
        "/analytics/admin/users/{user_id}/events",
        "/analytics/admin/users/{user_id}/paths",
    }
    assert expected <= paths


def test_production_pool_settings_are_bounded(monkeypatch):
    from analytics_service.db import pool_settings

    monkeypatch.setenv("ANALYTICS_DB_POOL_MIN", "0")
    monkeypatch.setenv("ANALYTICS_DB_POOL_MAX", "8")
    with pytest.raises(RuntimeError, match="1 <= min <= max"):
        pool_settings()
    monkeypatch.setenv("ANALYTICS_DB_POOL_MIN", "1")
    monkeypatch.setenv("ANALYTICS_DB_POOL_MAX", "101")
    with pytest.raises(RuntimeError, match="1 <= min <= max"):
        pool_settings()


def test_identity_adapter_uses_identity_service_claims_and_private_token(monkeypatch):
    import json
    import analytics_service.identity as identity

    user_id, session_id = uuid4(), uuid4()
    payload = {
        "principal": {
            "user": {"id": str(user_id), "status": "active"},
            "session_id": str(session_id),
            "roles": ["admin"],
            "permissions": ["analytics.read"],
            "mfa_enabled": False,
        }
    }
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return json.dumps(payload).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = request.headers
        captured["body"] = request.data
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setenv("IDENTITY_SERVICE_URL", "http://identity-api:8000")
    monkeypatch.setenv("IDENTITY_SERVICE_TOKEN", "s" * 40)
    monkeypatch.setattr(identity, "urlopen", fake_urlopen)

    principal = identity._request_identity("opaque-session-token")
    assert principal["id"] == user_id
    assert principal["session_id"] == session_id
    assert principal["permissions"] == ["analytics.read"]
    assert captured["url"] == "http://identity-api:8000/internal/introspect"
    normalized_headers = {key.lower(): value for key, value in captured["headers"].items()}
    assert normalized_headers["x-identity-service-token"] == "s" * 40
    assert json.loads(captured["body"]) == {"token": "opaque-session-token"}
    assert captured["timeout"] == 3.0


def test_identity_adapter_rejects_missing_authorization_claims(monkeypatch):
    import json
    import analytics_service.identity as identity

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return json.dumps({"principal": {
                "user": {"id": str(uuid4()), "status": "active"},
                "session_id": str(uuid4()),
            }}).encode("utf-8")

    monkeypatch.setenv("IDENTITY_SERVICE_URL", "http://identity-api:8000")
    monkeypatch.setenv("IDENTITY_SERVICE_TOKEN", "s" * 40)
    monkeypatch.setattr(identity, "urlopen", lambda *_args, **_kwargs: FakeResponse())
    with pytest.raises(identity.IdentityUnavailable):
        identity._request_identity("opaque-session-token")


def test_importer_rejects_remote_connections_without_verified_tls():
    from scripts.migrate_legacy_analytics import validate_connection_tls

    validate_connection_tls("postgresql://reader:secret@127.0.0.1:5432/legacy")
    with pytest.raises(RuntimeError, match="verify-full"):
        validate_connection_tls("postgresql://reader:secret@db.example:5432/legacy")
    validate_connection_tls(
        "postgresql://reader:secret@db.example:5432/legacy?sslmode=verify-full"
    )


def test_production_analytics_requires_private_identity_and_separate_database(monkeypatch):
    from analytics_service.db import validate_production_configuration

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "ANALYTICS_DATABASE_URL",
        "postgresql://analytics:secret@db.example/analytics?sslmode=verify-full",
    )
    monkeypatch.setenv("ANALYTICS_EXPECTED_DATABASE_NAME", "tloomberg_analytics")
    monkeypatch.setenv("SAAS_EXPECTED_DATABASE_NAME", "tloomberg_saas")
    monkeypatch.setenv("IDENTITY_EXPECTED_DATABASE_NAME", "tloomberg_identity")
    monkeypatch.setenv("COMMERCE_EXPECTED_DATABASE_NAME", "tloomberg_commerce")
    monkeypatch.setenv("IDENTITY_SERVICE_URL", "http://identity-api:8000")
    monkeypatch.setenv("IDENTITY_SERVICE_TOKEN", "i" * 40)
    monkeypatch.setenv("ANALYTICS_TRUSTED_HOSTS", "analytics.example.com")
    monkeypatch.setenv("ANALYTICS_ALLOWED_ORIGINS", "https://app.example.com")
    validate_production_configuration()

    monkeypatch.setenv("ANALYTICS_EXPECTED_DATABASE_NAME", "tloomberg_saas")
    with pytest.raises(RuntimeError, match="dedicated"):
        validate_production_configuration()




def test_production_saas_composition_does_not_mount_legacy_analytics_router():
    import os
    import subprocess
    import sys

    script = (
        "from saas_core.auth.api import app; "
        "assert '/analytics/events' not in app.openapi()['paths']; "
        "assert '/analytics/admin/overview' not in app.openapi()['paths']"
    )
    env = os.environ.copy()
    env["APP_ENV"] = "production"
    env.pop("SAAS_DATABASE_URL", None)
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr



def test_privileged_access_rejects_old_future_and_naive_mfa_timestamps():
    from datetime import datetime, timedelta, timezone
    from fastapi import HTTPException
    from analytics_service.api import _require

    now = datetime.now(timezone.utc)
    principal = {
        "permissions": ["analytics.users.read"],
        "roles": ["superadmin"],
        "mfa_enabled": True,
        "mfa_verified_at": (now - timedelta(minutes=16)).isoformat(),
    }
    with pytest.raises(HTTPException) as old:
        _require(principal, "analytics.users.read")
    assert old.value.status_code == 403

    principal["mfa_verified_at"] = (now + timedelta(minutes=3)).isoformat()
    with pytest.raises(HTTPException) as future:
        _require(principal, "analytics.users.read")
    assert future.value.status_code == 403

    principal["mfa_verified_at"] = now.replace(tzinfo=None).isoformat()
    with pytest.raises(HTTPException) as naive:
        _require(principal, "analytics.users.read")
    assert naive.value.status_code == 403



def test_retention_worker_requires_verified_tls_for_remote_databases():
    from analytics_service.retention_worker import validate_retention_connection

    validate_retention_connection(
        "postgresql://analytics@127.0.0.1:5432/tloomberg_analytics"
    )
    with pytest.raises(RuntimeError, match="sslmode=verify-full"):
        validate_retention_connection(
            "postgresql://analytics@analytics-db.internal:5432/tloomberg_analytics"
        )
    validate_retention_connection(
        "postgresql://analytics@analytics-db.internal:5432/tloomberg_analytics?sslmode=verify-full"
    )


def test_retention_worker_settings_are_bounded(monkeypatch):
    from analytics_service.retention_worker import retention_settings

    monkeypatch.setenv("SAAS_ANALYTICS_RETENTION_DAYS", "29")
    with pytest.raises(ValueError, match="30 and 730"):
        retention_settings()
    monkeypatch.setenv("SAAS_ANALYTICS_RETENTION_DAYS", "365")
    monkeypatch.setenv("ANALYTICS_RETENTION_POLL_SECONDS", "10")
    with pytest.raises(ValueError, match="60 and 86400"):
        retention_settings()



def test_api_rejects_oversized_json_before_authentication():
    from fastapi.testclient import TestClient
    from analytics_service.api import app

    with TestClient(app) as client:
        response = client.post(
            "/analytics/events",
            content=b'{"events":[],"untrusted":"' + (b"x" * (65 * 1024)) + b'"}',
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 413
    assert response.json() == {"detail": "request body too large"}




def test_sensitive_analytics_permissions_require_superadmin_even_with_mfa():
    from datetime import datetime, timezone
    from fastapi import HTTPException
    from analytics_service.api import _require

    principal = {
        "permissions": ["analytics.users.read"],
        "roles": ["admin"],
        "mfa_enabled": True,
        "mfa_verified_at": datetime.now(timezone.utc).isoformat(),
    }
    with pytest.raises(HTTPException) as denied:
        _require(principal, "analytics.users.read")
    assert denied.value.status_code == 403

    principal["roles"] = ["superadmin"]
    _require(principal, "analytics.users.read")



def test_legacy_import_path_drops_query_fragment_and_unsafe_identifiers():
    from scripts.migrate_legacy_analytics import normalize_legacy_path

    assert normalize_legacy_path("/profile/john-doe?email=secret@example.com#private") == "/profile/:id"
    assert normalize_legacy_path("https://external.example/account?token=secret") == "/account"
    assert normalize_legacy_path("not-a-path") == "/"



def test_events_per_minute_limit_is_bounded(monkeypatch):
    from analytics_service.service import events_per_minute_limit

    monkeypatch.setenv("ANALYTICS_EVENTS_PER_MINUTE", "59")
    with pytest.raises(ValueError, match="60 and 10000"):
        events_per_minute_limit()
    monkeypatch.setenv("ANALYTICS_EVENTS_PER_MINUTE", "10001")
    with pytest.raises(ValueError, match="60 and 10000"):
        events_per_minute_limit()
    monkeypatch.setenv("ANALYTICS_EVENTS_PER_MINUTE", "600")
    assert events_per_minute_limit() == 600



def test_retention_worker_requires_verified_tls_in_production_even_for_loopback(monkeypatch):
    from analytics_service.retention_worker import validate_retention_connection

    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(RuntimeError, match="Production Analytics retention"):
        validate_retention_connection(
            "postgresql://analytics@127.0.0.1:5432/tloomberg_analytics"
        )
    validate_retention_connection(
        "postgresql://analytics@127.0.0.1:5432/tloomberg_analytics?sslmode=verify-full"
    )
