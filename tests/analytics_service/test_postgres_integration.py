"""PostgreSQL integration tests against a dedicated disposable Analytics DB."""
from __future__ import annotations

import os
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from analytics_service.api import app
from analytics_service.db import get_conn
from analytics_service.identity import current_user

_DSN = os.getenv("ANALYTICS_TEST_DATABASE_URL", "").strip()
_EXPECTED = os.getenv("ANALYTICS_TEST_DATABASE_NAME", "").strip()

pytestmark = pytest.mark.skipif(
    not _DSN or not _EXPECTED,
    reason="requires a separately named disposable Analytics PostgreSQL database",
)

_PRINCIPALS: dict[str, dict] = {}


@pytest.fixture(scope="module", autouse=True)
def verify_analytics_database():
    if not _DSN or not _EXPECTED:
        pytest.skip("Analytics PostgreSQL database is not configured")
    with psycopg.connect(_DSN, row_factory=dict_row) as conn:
        actual = conn.execute("SELECT current_database() AS name").fetchone()["name"]
        if actual != _EXPECTED:
            pytest.fail("refusing Analytics integration tests against an unexpected database")
        names = ("analytics_consents", "analytics_consent_history",
                 "analytics_events", "analytics_ingestion_limits",
                 "analytics_audit_logs", "analytics_schema_migrations")
        row = conn.execute(
            "SELECT " + ", ".join(
                f"to_regclass('public.{name}') IS NOT NULL AS {name}" for name in names
            )
        ).fetchone()
        missing = [name for name in names if not row[name]]
        if missing:
            pytest.fail("Analytics migrations are incomplete: " + ", ".join(missing))


@pytest.fixture()
def db(verify_analytics_database):
    with psycopg.connect(_DSN, row_factory=dict_row) as conn:
        conn.execute(
            "TRUNCATE TABLE analytics_audit_logs, analytics_ingestion_limits, analytics_events, "
            "analytics_consent_history, analytics_consents"
        )
        conn.commit()
    yield
    with psycopg.connect(_DSN, row_factory=dict_row) as conn:
        conn.execute(
            "TRUNCATE TABLE analytics_audit_logs, analytics_ingestion_limits, analytics_events, "
            "analytics_consent_history, analytics_consents"
        )
        conn.commit()


@pytest.fixture()
def client(db):
    def database_dependency():
        with psycopg.connect(_DSN, row_factory=dict_row) as conn:
            yield conn

    # Use a request-injected override. The real production dependency uses
    # Identity introspection and is tested separately in unit coverage.
    def test_identity(request: Request):
        from fastapi import HTTPException
        token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not token or token not in _PRINCIPALS:
            raise HTTPException(status_code=401, detail="authentication required")
        return _PRINCIPALS[token]

    previous_conn = app.dependency_overrides.get(get_conn)
    previous_identity = app.dependency_overrides.get(current_user)
    app.dependency_overrides[get_conn] = database_dependency
    app.dependency_overrides[current_user] = test_identity
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        if previous_conn is None:
            app.dependency_overrides.pop(get_conn, None)
        else:
            app.dependency_overrides[get_conn] = previous_conn
        if previous_identity is None:
            app.dependency_overrides.pop(current_user, None)
        else:
            app.dependency_overrides[current_user] = previous_identity
        _PRINCIPALS.clear()


def actor(*permissions: str) -> tuple[str, dict]:
    user_id = uuid4()
    session_id = uuid4()
    token = str(uuid4())
    return token, {
        "id": user_id, "session_id": session_id,
        "permissions": list(permissions), "roles": [],
        "user": {"id": user_id, "status": "active"},
        "mfa_enabled": bool(
            {"analytics.users.read", "analytics.retention.manage"} & set(permissions)
        ),
        "mfa_verified_at": (
            __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
            if {"analytics.users.read", "analytics.retention.manage"} & set(permissions)
            else None
        ),
    }


def send_event(client, token: str, event_name: str, path: str, **extra):
    payload = {
        "client_event_id": str(uuid4()),
        "event_name": event_name,
        "page_path": path,
        **extra,
    }
    return client.post(
        "/analytics/events",
        headers={"Authorization": f"Bearer {token}"},
        json={"events": [payload]},
    )


def opt_in(client, token: str):
    response = client.put(
        "/analytics/consent",
        headers={"Authorization": f"Bearer {token}"},
        json={"product_analytics": True},
    )
    assert response.status_code == 200, response.text


def test_consent_ingestion_idempotency_auditing_and_erasure(client, db):
    readiness = client.get("/ready")
    assert readiness.status_code == 200
    assert readiness.json() == {"status": "ready", "service": "analytics"}
    user_token, user = actor()
    admin_token, admin = actor("analytics.users.read", "analytics.read")
    admin["roles"] = ["superadmin"]
    _PRINCIPALS[user_token] = user
    _PRINCIPALS[admin_token] = admin
    headers = {"Authorization": f"Bearer {user_token}"}

    status = client.get("/analytics/consent", headers=headers)
    assert status.status_code == 200
    assert status.json()["consent"]["product_analytics"] is False
    event_id = str(uuid4())
    payload = {"events": [{
        "client_event_id": event_id, "event_name": "page_view",
        "page_path": "/stocks/123456",
    }]}
    assert client.post("/analytics/events", headers=headers, json=payload).status_code == 403

    opt_in(client, user_token)
    first = client.post("/analytics/events", headers=headers, json=payload)
    assert first.status_code == 200
    assert first.json() == {"received": 1, "inserted": 1, "duplicates": 0}
    duplicate = client.post("/analytics/events", headers=headers, json=payload)
    assert duplicate.status_code == 200
    assert duplicate.json() == {"received": 1, "inserted": 0, "duplicates": 1}

    forbidden = client.get(
        f"/analytics/admin/users/{user['id']}/events", headers=headers
    )
    assert forbidden.status_code == 403
    history = client.get(
        f"/analytics/admin/users/{user['id']}/events",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert history.status_code == 200
    assert len(history.json()["events"]) == 1

    with psycopg.connect(_DSN, row_factory=dict_row) as conn:
        audit = conn.execute(
            "SELECT action,target_id FROM analytics_audit_logs "
            "WHERE actor_user_id=%s ORDER BY created_at DESC LIMIT 1",
            (admin["id"],),
        ).fetchone()
        assert audit["action"] == "analytics.user_events.read"
        assert audit["target_id"] == str(user["id"])

    revoked = client.put(
        "/analytics/consent", headers=headers, json={"product_analytics": False}
    )
    assert revoked.status_code == 200
    with psycopg.connect(_DSN, row_factory=dict_row) as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM analytics_events WHERE user_id=%s",
            (user["id"],),
        ).fetchone()["n"] == 0
        consent_rows = conn.execute(
            "SELECT count(*) AS n FROM analytics_consent_history WHERE user_id=%s",
            (user["id"],),
        ).fetchone()["n"]
        assert consent_rows == 2
        with conn.transaction():
            with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
                conn.execute(
                    "UPDATE analytics_consent_history SET product_analytics=TRUE WHERE user_id=%s",
                    (user["id"],),
                )


def test_aggregate_cohort_suppression_and_same_session_paths(client):
    admin_token, admin = actor("analytics.read")
    _PRINCIPALS[admin_token] = admin
    tokens = []
    for index in range(4):
        token, principal = actor()
        tokens.append(token)
        _PRINCIPALS[token] = principal
        opt_in(client, token)
        for path in ("/", "/market", "/stocks/123456",
                     "/", "/market", "/stocks/123456"):
            response = send_event(client, token, "page_view", path)
            assert response.status_code == 200, response.text
        click = send_event(
            client, token, "click", "/stocks/123456",
            x_pct=52.5, y_pct=47.5,
        )
        assert click.status_code == 200, click.text

    suppressed = client.get(
        "/analytics/admin/overview?days=30",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert suppressed.status_code == 200
    assert suppressed.json()["overview"] == {
        "suppressed": True, "minimum_cohort": 5
    }

    token, principal = actor()
    _PRINCIPALS[token] = principal
    opt_in(client, token)
    for path in ("/", "/market", "/stocks/123456",
                 "/", "/market", "/stocks/123456"):
        response = send_event(client, token, "page_view", path)
        assert response.status_code == 200, response.text
    click = send_event(
        client, token, "click", "/stocks/123456",
        x_pct=52.5, y_pct=47.5,
    )
    assert click.status_code == 200, click.text

    overview = client.get(
        "/analytics/admin/overview?days=30",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert overview.status_code == 200
    result = overview.json()["overview"]
    assert result["suppressed"] is False
    assert result["active_users"] == 5

    paths = client.get(
        "/analytics/admin/paths?days=30&max_steps=3",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert paths.status_code == 200
    journey = next(
        item for item in paths.json()["paths"]
        if item["path"] == ["/", "/market", "/stocks/:id"]
    )
    assert journey["users"] == 5
    assert journey["repetitions"] == 10

    heatmap = client.get(
        "/analytics/admin/heatmap",
        params={"page_path": "/stocks/123456", "days": 30},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert heatmap.status_code == 200, heatmap.text
    assert heatmap.json()["page_path"] == "/stocks/:id"
    assert heatmap.json()["cells"] == [{
        "x_bin": 50, "y_bin": 45, "clicks": 5, "users": 5,
    }]



def test_legacy_transfer_is_read_only_compatible_and_idempotent(db):
    source_dsn = os.getenv("SAAS_TEST_DATABASE_URL", "").strip()
    source_name = os.getenv("SAAS_TEST_DATABASE_NAME", "").strip()
    if not source_dsn or not source_name:
        pytest.skip("legacy SaaS test database is not configured")

    from saas_core.auth.repository import create_session, register_password_user
    from saas_core.auth.service import hash_session_token
    from scripts.migrate_legacy_analytics import migrate, source_counts

    username = "phase7_import_" + uuid4().hex[:16]
    with psycopg.connect(source_dsn, row_factory=dict_row) as source:
        actual = source.execute("SELECT current_database() AS name").fetchone()["name"]
        assert actual == source_name
        initial_source_counts = source_counts(source)
        user = register_password_user(
            source, username, "legacy-import-test-password-2026",
            display_name="Legacy Import Test",
        )
        token, _expires = create_session(source, user["id"])
        session_id = source.execute(
            "SELECT id FROM sessions WHERE token_hash=%s",
            (hash_session_token(token),),
        ).fetchone()["id"]
        event_id = uuid4()
        source.execute(
            """INSERT INTO analytics_consents
               (user_id,essential,product_analytics,consent_version,updated_at)
               VALUES (%s,TRUE,TRUE,'v1',now())""",
            (user["id"],),
        )
        source.execute(
            """INSERT INTO analytics_events
               (user_id,session_id,event_name,page_path,occurred_at,metadata,client_event_id)
               VALUES
                 (%s,%s,'page_view','/legacy/:id',now(),'{"source":"legacy-test"}'::jsonb,%s),
                 (%s,%s,'click','/profile/john-doe?email=secret@example.com#private',now(),'{}'::jsonb,NULL)""",
            (user["id"],session_id,event_id,user["id"],session_id),
        )
        source.execute(
            """INSERT INTO admin_audit_logs
               (actor_user_id,action,target_type,target_id)
               VALUES (%s,'analytics.user_events.read','user',%s)""",
            (user["id"],str(user["id"])),
        )
        source.commit()
        source.execute("SET default_transaction_read_only = on")
        source.commit()

        before = source_counts(source)
        assert before["events"] == initial_source_counts["events"] + 2
        assert before["linked_events"] == initial_source_counts["linked_events"] + 2
        with psycopg.connect(_DSN, row_factory=dict_row) as target:
            first = migrate(source, target, batch_size=1)
            second = migrate(source, target, batch_size=1)
            assert first == second
            assert target.execute(
                "SELECT count(*) AS n FROM analytics_events WHERE user_id=%s",
                (user["id"],),
            ).fetchone()["n"] == 2
            assert target.execute(
                "SELECT count(*) AS n FROM analytics_consent_history "
                "WHERE user_id=%s AND source='legacy_import'",
                (user["id"],),
            ).fetchone()["n"] == 1
            assert target.execute(
                "SELECT count(*) AS n FROM analytics_audit_logs "
                "WHERE actor_user_id=%s AND action='analytics.user_events.read'",
                (user["id"],),
            ).fetchone()["n"] == 1
            imported = target.execute(
                "SELECT client_event_id,id FROM analytics_events "
                "WHERE user_id=%s AND event_name='click'",
                (user["id"],),
            ).fetchone()
            assert imported["client_event_id"] == imported["id"]
            imported_path = target.execute(
                "SELECT page_path FROM analytics_events "
                "WHERE user_id=%s AND event_name='click'",
                (user["id"],),
            ).fetchone()
            assert imported_path["page_path"] == "/profile/:id"
            imported_metadata = target.execute(
                "SELECT metadata FROM analytics_events "
                "WHERE user_id=%s AND event_name='page_view'",
                (user["id"],),
            ).fetchone()
            assert imported_metadata["metadata"] == {}

        after = source_counts(source)
        assert after == before



def test_consent_policy_version_rotation_requires_fresh_opt_in(client, db, monkeypatch):
    from analytics_service import api as analytics_api

    token, principal = actor()
    _PRINCIPALS[token] = principal
    opt_in(client, token)

    monkeypatch.setattr(analytics_api, "CONSENT_VERSION", "v2")
    status = client.get(
        "/analytics/consent",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert status.status_code == 200
    assert status.json()["consent"]["product_analytics"] is False
    assert status.json()["consent"]["consent_stale"] is True

    denied = send_event(client, token, "page_view", "/market")
    assert denied.status_code == 403

    renewed = client.put(
        "/analytics/consent",
        headers={"Authorization": f"Bearer {token}"},
        json={"product_analytics": True},
    )
    assert renewed.status_code == 200
    assert renewed.json()["consent"]["consent_version"] == "v2"
    accepted = send_event(client, token, "page_view", "/market")
    assert accepted.status_code == 200



def test_privileged_retention_purge_is_mfa_gated_and_audited(client, db, monkeypatch):
    user_token, user = actor()
    admin_token, admin = actor("analytics.retention.manage")
    admin["roles"] = ["superadmin"]
    _PRINCIPALS[user_token] = user
    _PRINCIPALS[admin_token] = admin
    opt_in(client, user_token)
    event = send_event(client, user_token, "page_view", "/market")
    assert event.status_code == 200

    with psycopg.connect(_DSN, row_factory=dict_row) as conn:
        conn.execute(
            "UPDATE analytics_events SET occurred_at=now()-interval '31 days' WHERE user_id=%s",
            (user["id"],),
        )
        conn.commit()

    monkeypatch.setenv("SAAS_ANALYTICS_RETENTION_DAYS", "30")
    # The account has permission but an absent/expired MFA proof must still fail closed.
    admin_without_mfa = dict(admin)
    admin_without_mfa["mfa_verified_at"] = None
    _PRINCIPALS["no-mfa"] = admin_without_mfa
    denied = client.delete(
        "/analytics/admin/retention/expired",
        headers={"Authorization": "Bearer no-mfa"},
    )
    assert denied.status_code == 403

    purged = client.delete(
        "/analytics/admin/retention/expired",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert purged.status_code == 200
    assert purged.json()["deleted_events"] == 1
    with psycopg.connect(_DSN, row_factory=dict_row) as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM analytics_events WHERE user_id=%s",
            (user["id"],),
        ).fetchone()["n"] == 0
        audit = conn.execute(
            "SELECT action,target_id FROM analytics_audit_logs WHERE actor_user_id=%s "
            "ORDER BY created_at DESC LIMIT 1",
            (admin["id"],),
        ).fetchone()
        assert audit["action"] == "analytics.retention.purge"
        assert audit["target_id"] == "retention_days=30;deleted=1"



def test_event_ingestion_is_rate_limited_per_user_across_batches(client, db, monkeypatch):
    token, principal = actor()
    _PRINCIPALS[token] = principal
    opt_in(client, token)
    monkeypatch.setenv("ANALYTICS_EVENTS_PER_MINUTE", "60")

    first_batch = {
        "events": [
            {
                "client_event_id": str(uuid4()),
                "event_name": "page_view",
                "page_path": "/market",
            }
            for _ in range(50)
        ]
    }
    second_batch = {
        "events": [
            {
                "client_event_id": str(uuid4()),
                "event_name": "page_view",
                "page_path": "/market",
            }
            for _ in range(10)
        ]
    }
    headers = {"Authorization": f"Bearer {token}"}
    first = client.post("/analytics/events", headers=headers, json=first_batch)
    second = client.post("/analytics/events", headers=headers, json=second_batch)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text

    duplicate = client.post(
        "/analytics/events",
        headers=headers,
        json={"events": [first_batch["events"][0]]},
    )
    assert duplicate.status_code == 200
    assert duplicate.json() == {"received": 1, "inserted": 0, "duplicates": 1}

    overflow = send_event(client, token, "page_view", "/market")
    assert overflow.status_code == 429
    assert overflow.headers["retry-after"] == "60"

    with psycopg.connect(_DSN, row_factory=dict_row) as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM analytics_events WHERE user_id=%s",
            (principal["id"],),
        ).fetchone()["n"] == 60
        count = conn.execute(
            "SELECT event_count FROM analytics_ingestion_limits WHERE user_id=%s",
            (principal["id"],),
        ).fetchone()["event_count"]
        assert count == 60



def test_funnel_uses_database_event_order_for_equal_timestamps(client, db):
    admin_token, admin = actor("analytics.read")
    _PRINCIPALS[admin_token] = admin
    event_time = "2026-11-27T12:00:00+00:00"

    for index in range(6):
        user_id, session_id = uuid4(), uuid4()
        with psycopg.connect(_DSN, row_factory=dict_row) as conn:
            conn.execute(
                """INSERT INTO analytics_consents
                   (user_id,essential,product_analytics,consent_version)
                   VALUES (%s,TRUE,TRUE,'v1')""",
                (user_id,),
            )
            if index == 5:
                event_names = ("click", "page_view")
            else:
                event_names = ("page_view", "click")
            for event_name in event_names:
                conn.execute(
                    """INSERT INTO analytics_events
                       (user_id,session_id,event_name,page_path,occurred_at,client_event_id)
                       VALUES (%s,%s,%s,'/market',%s,%s)""",
                    (user_id,session_id,event_name,event_time,uuid4()),
                )
            conn.commit()

    response = client.get(
        "/analytics/admin/funnel?steps=page_view&steps=click&days=30",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["suppressed"] is False
    assert result["steps"][0]["users"] == 6
    assert result["steps"][1]["users"] == 5
