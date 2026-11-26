"""PostgreSQL-backed integration tests for the complete SaaS schema and critical flows.

These tests run only when SAAS_TEST_DATABASE_URL and SAAS_TEST_DATABASE_NAME point
at an explicitly named disposable database. GitHub Actions provisions a fresh
PostgreSQL 16 service and applies every migration before running pytest.
"""
from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from saas_core.auth.repository import create_session, register_password_user
from saas_core.auth.service import hash_session_token
from saas_core.admin.service import adjust_wallet
from saas_core.wallet.wallet_repository import post_ledger_entry
from saas_core.payments.domain import PaymentRejected, PaymentNotConfigured, PaymentRequest, VerifiedPayment
from saas_core.payments.gateways import get_gateway
from saas_core.payments.repository import (
    attach_provider_checkout,
    create_payment_attempt,
    create_subscription_order,
    settle_verified_payment,
)

_DSN = os.environ.get("SAAS_TEST_DATABASE_URL", "").strip()
_EXPECTED_DB = os.environ.get("SAAS_TEST_DATABASE_NAME", "").strip()

pytestmark = pytest.mark.skipif(
    not _DSN or not _EXPECTED_DB,
    reason="PostgreSQL integration tests require an explicitly named disposable SAAS_TEST_DATABASE_URL",
)


@pytest.fixture(scope="module", autouse=True)
def verify_disposable_saas_database():
    if not _DSN or not _EXPECTED_DB:
        pytest.skip("disposable PostgreSQL database is not configured")
    with psycopg.connect(_DSN, row_factory=dict_row) as conn:
        current = conn.execute("SELECT current_database() AS name").fetchone()["name"]
        if current != _EXPECTED_DB:
            pytest.fail(
                "Refusing to run integration tests: connected database name does not match "
                "SAAS_TEST_DATABASE_NAME"
            )
        required = ("users", "sessions", "payment_orders", "wallet_ledger_entries",
                    "tickets", "analytics_events", "payment_reconciliation_notes")
        row = conn.execute(
            "SELECT " + ", ".join(
                f"to_regclass('public.{name}') IS NOT NULL AS {name}" for name in required
            )
        ).fetchone()
        missing = [name for name in required if not row[name]]
        if missing:
            pytest.fail(
                "SaaS migrations are incomplete in the disposable integration database: "
                + ", ".join(missing)
            )


@pytest.fixture()
def db(verify_disposable_saas_database):
    conn = psycopg.connect(_DSN, row_factory=dict_row)
    # Truncate user-owned application data between tests. Reference tables
    # (roles, permissions and plans) and their seeded rows remain in place.
    conn.execute("TRUNCATE TABLE users CASCADE")
    conn.execute("TRUNCATE TABLE phone_verifications")
    conn.commit()
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture()
def client(verify_disposable_saas_database):
    from saas_core.auth.api import app, get_conn

    def test_connection():
        with psycopg.connect(_DSN, row_factory=dict_row) as conn:
            yield conn

    previous = app.dependency_overrides.get(get_conn)
    app.dependency_overrides[get_conn] = test_connection
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_conn, None)
        else:
            app.dependency_overrides[get_conn] = previous


def _new_user(conn, username: str):
    user = register_password_user(
        conn,
        username,
        "integration-test-password-2026",
        display_name=username.title(),
    )
    token, _expires = create_session(conn, user["id"])
    conn.commit()
    return user, token


def _grant_role(conn, user_id, role_code: str):
    conn.execute(
        """INSERT INTO user_roles(user_id,role_id)
           SELECT %s,id FROM roles WHERE code=%s
           ON CONFLICT DO NOTHING""",
        (user_id, role_code),
    )
    conn.commit()


def _auth(token: str):
    return {"Authorization": f"Bearer {token}"}


def test_real_postgres_authentication_session_hash_and_revocation(db, client):
    from saas_core.auth.repository import revoke_session

    user, token = _new_user(db, "phase9_auth_user")

    with db.cursor() as cur:
        cur.execute("SELECT token_hash FROM sessions WHERE user_id=%s", (user["id"],))
        stored_hash = cur.fetchone()["token_hash"]
    assert stored_hash == hash_session_token(token)
    assert token not in stored_hash

    response = client.get("/auth/me", headers=_auth(token))
    assert response.status_code == 200
    assert response.json()["user"]["id"] == str(user["id"])

    revoke_session(db, token)
    db.commit()
    response = client.get("/auth/me", headers=_auth(token))
    assert response.status_code == 401


def test_rbac_support_internal_note_isolation_and_account_ownership(db, client, monkeypatch):
    customer, customer_token = _new_user(db, "phase9_customer")
    outsider, outsider_token = _new_user(db, "phase9_outsider")
    support, support_token = _new_user(db, "phase9_support")
    superadmin, superadmin_token = _new_user(db, "phase9_superadmin")
    _grant_role(db, support["id"], "support")
    _grant_role(db, superadmin["id"], "superadmin")

    # A privileged role alone is not sufficient: enroll and verify MFA for
    # this exact session before entering the superadmin API.
    from cryptography.fernet import Fernet
    from saas_core.auth.mfa import totp_code
    monkeypatch.setenv("SAAS_ADMIN_MFA_FERNET_KEY", Fernet.generate_key().decode("ascii"))
    setup = client.post("/auth/mfa/setup", headers=_auth(superadmin_token))
    assert setup.status_code == 200, setup.text
    confirmed = client.post(
        "/auth/mfa/confirm",
        headers=_auth(superadmin_token),
        json={"code": totp_code(setup.json()["secret"])},
    )
    assert confirmed.status_code == 200, confirmed.text

    opened = client.post(
        "/support/tickets",
        headers=_auth(customer_token),
        json={"subject": "Account access problem", "message": "I cannot access my account."},
    )
    assert opened.status_code == 201
    ticket_id = opened.json()["ticket"]["id"]

    note = client.post(
        f"/support/manage/tickets/{ticket_id}/internal-notes",
        headers=_auth(support_token),
        json={"message": "Internal investigation note - never show to customer."},
    )
    assert note.status_code == 201

    customer_view = client.get(
        f"/support/tickets/{ticket_id}", headers=_auth(customer_token)
    )
    assert customer_view.status_code == 200
    customer_bodies = [item["body"] for item in customer_view.json()["messages"]]
    assert "Internal investigation note - never show to customer." not in customer_bodies

    outsider_view = client.get(
        f"/support/tickets/{ticket_id}", headers=_auth(outsider_token)
    )
    assert outsider_view.status_code == 404

    staff_view = client.get(
        f"/support/manage/tickets/{ticket_id}", headers=_auth(support_token)
    )
    assert staff_view.status_code == 200
    assert any(
        message["is_internal_note"]
        for message in staff_view.json()["messages"]
    )

    # The support role alone is never sufficient to enter the separate
    # superadmin console, even though it can access support management.
    assert client.get("/admin/dashboard", headers=_auth(support_token)).status_code == 403
    assert client.get("/admin/dashboard", headers=_auth(customer_token)).status_code == 403
    assert client.get("/admin/dashboard", headers=_auth(superadmin_token)).status_code == 200


def test_analytics_requires_consent_deduplicates_and_limits_user_history(db, client):
    user, token = _new_user(db, "phase9_analytics_user")
    superadmin, superadmin_token = _new_user(db, "phase9_analytics_admin")
    _grant_role(db, superadmin["id"], "superadmin")

    event_id = str(uuid4())
    event = {
        "client_event_id": event_id,
        "event_name": "page_view",
        "page_path": f"/company/{123456}",
        "component_id": "company-summary",
        "element_role": "region",
        "typed_text": "this must never be stored",
    }

    denied = client.post(
        "/analytics/events", headers=_auth(token), json={"events": [event]}
    )
    assert denied.status_code == 403

    consent = client.put(
        "/analytics/consent",
        headers=_auth(token),
        json={"product_analytics": True},
    )
    assert consent.status_code == 200
    assert consent.json()["consent"]["product_analytics"] is True

    first = client.post(
        "/analytics/events", headers=_auth(token), json={"events": [event]}
    )
    assert first.status_code == 200
    assert first.json() == {"received": 1, "inserted": 1, "duplicates": 0}

    duplicate = client.post(
        "/analytics/events", headers=_auth(token), json={"events": [event]}
    )
    assert duplicate.status_code == 200
    assert duplicate.json() == {"received": 1, "inserted": 0, "duplicates": 1}

    regular_history = client.get(
        f"/analytics/admin/users/{user['id']}/events", headers=_auth(token)
    )
    assert regular_history.status_code == 403
    admin_history = client.get(
        f"/analytics/admin/users/{user['id']}/events", headers=_auth(superadmin_token)
    )
    assert admin_history.status_code == 200
    assert len(admin_history.json()["events"]) == 1

    with db.cursor() as cur:
        cur.execute(
            "SELECT page_path,metadata FROM analytics_events WHERE user_id=%s",
            (user["id"],),
        )
        stored = cur.fetchone()
    assert stored["page_path"] == "/company/:id"
    assert "typed_text" not in stored["metadata"]

    revoked = client.put(
        "/analytics/consent",
        headers=_auth(token),
        json={"product_analytics": False},
    )
    assert revoked.status_code == 200
    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS total FROM analytics_events WHERE user_id=%s", (user["id"],))
        assert cur.fetchone()["total"] == 0


def test_payment_order_settlement_and_gateway_event_replay_are_idempotent(db):
    user, _token = _new_user(db, "phase9_payer")
    db.execute(
        "UPDATE users SET phone_e164=%s,phone_verified_at=now() WHERE id=%s",
        ("+989121234567", user["id"]),
    )
    db.commit()

    order = create_subscription_order(
        db, user_id=user["id"], plan_code="premium_monthly",
        idempotency_key="phase9-order-idempotency-1",
    )
    duplicate_order = create_subscription_order(
        db, user_id=user["id"], plan_code="premium_monthly",
        idempotency_key="phase9-order-idempotency-1",
    )
    assert duplicate_order["id"] == order["id"]
    with pytest.raises(ValueError):
        create_subscription_order(
            db, user_id=user["id"], plan_code="premium_quarterly",
            idempotency_key="phase9-order-idempotency-1",
        )

    attempt = create_payment_attempt(db, order_id=order["id"], provider="snappay")
    with pytest.raises(ValueError):
        create_payment_attempt(db, order_id=order["id"], provider="snappay")
    attach_provider_checkout(
        db,
        attempt_id=attempt["id"],
        provider_order_id="sandbox-order-phase9",
        redirect_url="https://sandbox.invalid/checkout",
        request_ref="phase9-request",
    )

    amount = int(order["amount_toman"])
    verified = VerifiedPayment(
        provider_event_id="phase9-provider-event-1",
        provider_order_id="sandbox-order-phase9",
        amount_toman=amount,
        success=True,
    )
    wrong_amount = VerifiedPayment(
        provider_event_id="phase9-provider-event-bad",
        provider_order_id="sandbox-order-phase9",
        amount_toman=amount - 1,
        success=True,
    )
    payload_hash = hashlib.sha256(b"verified-provider-payload").hexdigest()
    with pytest.raises(PaymentRejected):
        settle_verified_payment(
            db, order_id=order["id"], attempt_id=attempt["id"],
            provider="snappay", verified=wrong_amount, payload_sha256=payload_hash,
        )

    with db.cursor() as cur:
        cur.execute("SELECT status FROM payment_orders WHERE id=%s", (order["id"],))
        assert cur.fetchone()["status"] == "pending"
        cur.execute("SELECT COUNT(*) AS total FROM subscriptions WHERE payment_order_id=%s", (order["id"],))
        assert cur.fetchone()["total"] == 0

    settled = settle_verified_payment(
        db, order_id=order["id"], attempt_id=attempt["id"],
        provider="snappay", verified=verified, payload_sha256=payload_hash,
    )
    assert settled["already_processed"] is False

    replay = settle_verified_payment(
        db, order_id=order["id"], attempt_id=attempt["id"],
        provider="snappay", verified=verified, payload_sha256=payload_hash,
    )
    assert replay["already_processed"] is True

    changed_payload = hashlib.sha256(b"same event id but different body").hexdigest()
    with pytest.raises(PaymentRejected):
        settle_verified_payment(
            db, order_id=order["id"], attempt_id=attempt["id"],
            provider="snappay", verified=verified, payload_sha256=changed_payload,
        )

    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS total FROM subscriptions WHERE payment_order_id=%s", (order["id"],))
        assert cur.fetchone()["total"] == 1
        cur.execute("SELECT processing_status FROM gateway_events WHERE provider_event_id=%s",
                    ("phase9-provider-event-1",))
        assert cur.fetchone()["processing_status"] == "processed"


def test_gateway_adapters_fail_closed_without_official_provider_configuration():
    gateway = get_gateway("snappay")
    with pytest.raises(PaymentNotConfigured):
        gateway.initiate(
            PaymentRequest(
                order_id=uuid4(),
                amount_toman=100_000,
                callback_url="https://example.invalid/callback",
                description="integration test",
            )
        )


def test_wallet_debit_race_cannot_overdraw_cash_and_ledger_is_immutable(db):
    actor, _actor_token = _new_user(db, "phase9_wallet_admin")
    target, _target_token = _new_user(db, "phase9_wallet_target")
    _grant_role(db, actor["id"], "superadmin")

    credit = post_ledger_entry(
        db, user_id=target["id"], bucket="cash", entry_type="topup",
        amount_toman=100, idempotency_key="phase9-initial-credit",
    )
    duplicate = post_ledger_entry(
        db, user_id=target["id"], bucket="cash", entry_type="topup",
        amount_toman=100, idempotency_key="phase9-initial-credit",
    )
    assert duplicate["id"] == credit["id"]
    db.commit()

    def debit(key: str):
        with psycopg.connect(_DSN, row_factory=dict_row) as conn:
            try:
                adjust_wallet(
                    conn, actor_id=actor["id"], user_id=target["id"], bucket="cash",
                    amount_toman=-70, reason="Concurrent debit test",
                    idempotency_key=key,
                )
                return "accepted"
            except ValueError:
                return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(debit, ("phase9-debit-a", "phase9-debit-b")))
    assert sorted(outcomes) == ["accepted", "rejected"]

    with db.cursor() as cur:
        cur.execute(
            """SELECT COALESCE(SUM(amount_toman),0)::bigint AS balance,
                      COUNT(*) FILTER (WHERE entry_type='admin_debit') AS debits
               FROM wallet_ledger_entries WHERE user_id=%s AND bucket='cash'""",
            (target["id"],),
        )
        result = cur.fetchone()
    assert result["balance"] == 30
    assert result["debits"] == 1

    with pytest.raises(psycopg.errors.RaiseException):
        with db.transaction():
            db.execute(
                "UPDATE wallet_ledger_entries SET amount_toman=999 WHERE id=%s",
                (credit["id"],),
            )


def test_readiness_probe_and_security_headers_report_real_database_state(db, client):
    ready = client.get("/ready")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready", "service": "saas-api"}

    health = client.get("/health", headers={"X-Request-ID": "unsafe request id"})
    assert health.status_code == 200
    assert health.headers["x-content-type-options"] == "nosniff"
    assert health.headers["x-frame-options"] == "DENY"
    assert health.headers["cache-control"] == "no-store"
    assert health.headers["content-security-policy"].startswith("default-src 'none'")
    assert health.headers["x-request-id"] != "unsafe request id"


def test_cookie_authenticated_state_changes_require_same_origin(db, client):
    _user, token = _new_user(db, "phase9_cookie_csrf")
    client.cookies.set("tloomberg_session", token)

    without_origin = client.post(
        "/support/tickets",
        json={"subject": "Cookie request test", "message": "This must be blocked without an origin."},
    )
    assert without_origin.status_code == 403

    with_same_origin = client.post(
        "/support/tickets",
        headers={"Origin": "http://testserver"},
        json={"subject": "Cookie request test", "message": "This same-origin request is allowed."},
    )
    assert with_same_origin.status_code == 201


def test_database_migration_runner_reports_current_checksum_history():
    from scripts.manage_saas_migrations import run

    assert run(apply=False) == 0


def test_otp_request_ceiling_is_serialized_under_concurrent_requests(db, monkeypatch):
    from cryptography.fernet import Fernet
    from saas_core.auth.repository import request_phone_otp

    monkeypatch.setenv("SAAS_OTP_HMAC_SECRET", "phase9-otp-hmac-secret-" + "x" * 32)
    monkeypatch.setenv("SAAS_OUTBOX_FERNET_KEY", Fernet.generate_key().decode("ascii"))
    phone = "+989" + f"{uuid4().int % 1_000_000_000:09d}"

    def request_one(_index: int):
        with psycopg.connect(_DSN, row_factory=dict_row) as conn:
            try:
                request_phone_otp(conn, phone, "login")
                return "accepted"
            except ValueError as exc:
                assert "too many verification requests" in str(exc)
                return "limited"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(request_one, range(8)))

    assert outcomes.count("accepted") == 3
    assert outcomes.count("limited") == 5
    with db.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS total FROM phone_verifications WHERE phone_e164=%s",
            (phone,),
        )
        assert cur.fetchone()["total"] == 3
        phone_tag = hashlib.sha256(phone.encode("utf-8")).hexdigest()[:20]
        cur.execute(
            """SELECT COUNT(*) AS total FROM notification_outbox
               WHERE channel='sms' AND template_code='phone_otp'
                 AND idempotency_key LIKE %s""",
            (f"otp:{phone_tag}:login:%",),
        )
        assert cur.fetchone()["total"] == 3


def test_wallet_admin_adjustment_exact_retry_returns_original_entry(db):
    actor, _ = _new_user(db, "phase9_wallet_replay_admin")
    target, _ = _new_user(db, "phase9_wallet_replay_target")

    post_ledger_entry(
        db, user_id=target["id"], bucket="cash", entry_type="topup",
        amount_toman=100, idempotency_key="phase9-replay-opening-credit",
    )
    db.commit()

    first = adjust_wallet(
        db, actor_id=actor["id"], user_id=target["id"], bucket="cash",
        amount_toman=-70, reason="Approved cash correction",
        idempotency_key="phase9-replay-debit-key",
    )
    retry = adjust_wallet(
        db, actor_id=actor["id"], user_id=target["id"], bucket="cash",
        amount_toman=-70, reason="Approved cash correction",
        idempotency_key="phase9-replay-debit-key",
    )
    assert retry["id"] == first["id"]

    with pytest.raises(ValueError, match="idempotency key was reused"):
        adjust_wallet(
            db, actor_id=actor["id"], user_id=target["id"], bucket="cash",
            amount_toman=-60, reason="Changed retry request",
            idempotency_key="phase9-replay-debit-key",
        )

    with db.cursor() as cur:
        cur.execute(
            """SELECT COALESCE(SUM(amount_toman),0)::bigint AS balance,
                      COUNT(*) FILTER (WHERE idempotency_key='phase9-replay-debit-key') AS adjustments
               FROM wallet_ledger_entries WHERE user_id=%s AND bucket='cash'""",
            (target["id"],),
        )
        row = cur.fetchone()
    assert row["balance"] == 30
    assert row["adjustments"] == 1


def test_admin_audit_logs_are_append_only_in_postgresql(db):
    actor, _ = _new_user(db, "phase9_audit_immutable")
    db.execute(
        """INSERT INTO admin_audit_logs(actor_user_id,action,target_type,target_id)
           VALUES (%s,'phase9.test','user','target')""",
        (actor["id"],),
    )
    db.commit()
    with db.cursor() as cur:
        cur.execute(
            "SELECT id FROM admin_audit_logs WHERE actor_user_id=%s AND action='phase9.test'",
            (actor["id"],),
        )
        audit_id = cur.fetchone()["id"]
    with pytest.raises(psycopg.errors.RaiseException):
        with db.transaction():
            db.execute("UPDATE admin_audit_logs SET action='tampered' WHERE id=%s", (audit_id,))
    with pytest.raises(psycopg.errors.RaiseException):
        with db.transaction():
            db.execute("DELETE FROM admin_audit_logs WHERE id=%s", (audit_id,))


def test_migration_grant_gives_runtime_dml_without_migration_ledger_access(db):
    from uuid import uuid4
    from psycopg import sql
    from scripts.manage_saas_migrations import _grant_runtime_role

    role_name = "phase9_runtime_" + uuid4().hex[:16]
    db.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role_name)))
    db.commit()
    try:
        _grant_runtime_role(db, role_name)
        with db.cursor() as cur:
            cur.execute(
                """SELECT
                     has_schema_privilege(%s,'public','USAGE') AS schema_usage,
                     has_schema_privilege(%s,'public','CREATE') AS schema_create,
                     has_table_privilege(%s,'public.users','SELECT') AS users_select,
                     has_table_privilege(%s,'public.users','INSERT') AS users_insert,
                     has_table_privilege(%s,'public.saas_schema_migrations','SELECT') AS ledger_select""",
                (role_name,role_name,role_name,role_name,role_name),
            )
            grants = cur.fetchone()
        assert grants["schema_usage"] is True
        assert grants["schema_create"] is False
        assert grants["users_select"] is True
        assert grants["users_insert"] is True
        assert grants["ledger_select"] is False
    finally:
        db.execute(
            sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role_name))
        )
        db.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role_name)))
        db.commit()


def test_production_connection_pool_opens_checks_database_identity_and_serves_readiness(db, monkeypatch):
    from cryptography.fernet import Fernet
    import saas_core.auth.api as auth_api

    monkeypatch.setattr(auth_api, "APP_ENV", "staging")
    monkeypatch.setattr(auth_api, "COOKIE_SECURE", True)
    monkeypatch.setattr(auth_api, "TRUSTED_HOSTS", ["testserver"])
    monkeypatch.setattr(auth_api, "ALLOWED_ORIGINS", ["https://app.example.com"])
    monkeypatch.setenv("SAAS_DATABASE_URL", _DSN)
    monkeypatch.setenv("SAAS_EXPECTED_DATABASE_NAME", _EXPECTED_DB)
    monkeypatch.setenv("SAAS_OTP_HMAC_SECRET", "phase9-production-otp-secret-" + "x" * 32)
    monkeypatch.setenv("SAAS_OUTBOX_FERNET_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("SAAS_DB_POOL_MIN", "1")
    monkeypatch.setenv("SAAS_DB_POOL_MAX", "3")

    with TestClient(auth_api.app) as production_client:
        ready = production_client.get("/ready")
        assert ready.status_code == 200
        assert ready.json()["status"] == "ready"
        assert auth_api._DB_POOL is not None
    assert auth_api._DB_POOL is None


def test_production_connection_pool_refuses_wrong_database_name(db, monkeypatch):
    from cryptography.fernet import Fernet
    import saas_core.auth.api as auth_api

    monkeypatch.setattr(auth_api, "APP_ENV", "staging")
    monkeypatch.setattr(auth_api, "COOKIE_SECURE", True)
    monkeypatch.setattr(auth_api, "TRUSTED_HOSTS", ["testserver"])
    monkeypatch.setattr(auth_api, "ALLOWED_ORIGINS", ["https://app.example.com"])
    monkeypatch.setenv("SAAS_DATABASE_URL", _DSN)
    monkeypatch.setenv("SAAS_EXPECTED_DATABASE_NAME", "not_the_test_database")
    monkeypatch.setenv("SAAS_OTP_HMAC_SECRET", "phase9-production-otp-secret-" + "x" * 32)
    monkeypatch.setenv("SAAS_OUTBOX_FERNET_KEY", Fernet.generate_key().decode("ascii"))

    with pytest.raises(RuntimeError, match="does not match SAAS_EXPECTED_DATABASE_NAME"):
        with TestClient(auth_api.app):
            pass
    assert auth_api._DB_POOL is None


def test_access_log_uses_route_template_and_never_records_query_string(db, client, caplog):
    import json
    import logging

    with caplog.at_level(logging.INFO, logger="tloomberg.saas.access"):
        response = client.get("/health?phone=%2B989111122233&token=do-not-log")
    assert response.status_code == 200

    entries = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "tloomberg.saas.access"
    ]
    assert entries
    assert entries[-1]["route"] == "/health"
    assert entries[-1]["method"] == "GET"
    assert "phone" not in entries[-1]
    assert "token" not in entries[-1]
    assert "do-not-log" not in json.dumps(entries)


def test_standard_user_is_denied_every_staff_and_admin_read_route(db, client):
    user, token = _new_user(db, "phase9_rbac_route_matrix")
    from saas_core.auth.api import app

    denied_prefixes = ("/admin/", "/analytics/admin/", "/support/manage/")
    protected_get_routes = sorted(
        path
        for path, methods in app.openapi()["paths"].items()
        if path.startswith(denied_prefixes) and "get" in methods
    )
    assert protected_get_routes

    path_ids = {
        "user_id": str(uuid4()),
        "ticket_id": str(uuid4()),
        "subscription_id": str(uuid4()),
        "order_id": str(uuid4()),
        "withdrawal_id": str(uuid4()),
        "plan_id": str(uuid4()),
    }

    checked = []
    for template in protected_get_routes:
        path = template
        for name, value in path_ids.items():
            path = path.replace("{" + name + "}", value)
        params = {}
        if template == "/analytics/admin/heatmap":
            params["page_path"] = "/"
        elif template == "/analytics/admin/funnel":
            params["steps"] = ["page_view", "click"]
        response = client.get(path, headers=_auth(token), params=params)
        assert response.status_code == 403, (
            f"Standard user unexpectedly accessed {template}: "
            f"{response.status_code} {response.text[:200]}"
        )
        checked.append(template)

    # Keep a visible test assertion that the full current protected GET surface
    # was exercised, so newly added routes automatically enter this matrix.
    assert len(checked) == len(protected_get_routes)


def _insert_page_view_sequence(conn, user_id, session_id, paths):
    for page_path in paths:
        conn.execute(
            """INSERT INTO analytics_events
               (user_id,anonymous_id,session_id,event_name,page_path,occurred_at)
               VALUES (%s,NULL,%s,'page_view',%s,now())""",
            (user_id, session_id, page_path),
        )
    conn.commit()


def test_repeated_path_reports_are_session_scoped_private_and_audited(db, client):
    from uuid import uuid4

    users = []
    for index in range(5):
        user, token = _new_user(db, f"phase9_path_user_{index}")
        users.append((user, token))
        db.execute(
            """INSERT INTO analytics_consents
               (user_id,essential,product_analytics,consent_version)
               VALUES (%s,TRUE,TRUE,'v1')""",
            (user["id"],),
        )
    superadmin, superadmin_token = _new_user(db, "phase9_path_superadmin")
    _grant_role(db, superadmin["id"], "superadmin")

    target_session_ids = []
    for user, _token in users:
        first_session = uuid4()
        _insert_page_view_sequence(
            db,
            user["id"],
            first_session,
            ["/", "/market", "/company/:id", "/", "/market", "/company/:id"],
        )
        target_session_ids.append(first_session)

    # The second session starts midway through the same route. It must not
    # create a synthetic "/" -> "/market" transition across session boundaries.
    second_session = uuid4()
    _insert_page_view_sequence(
        db, users[0][0]["id"], second_session, ["/market", "/company/:id"]
    )
    with db.cursor() as cur:
        cur.execute(
            """SELECT COUNT(DISTINCT occurred_at) AS timestamp_count,
                      array_agg(event_order ORDER BY event_order) AS event_orders
               FROM analytics_events WHERE session_id=%s""",
            (target_session_ids[0],),
        )
        ordering = cur.fetchone()
    assert ordering["timestamp_count"] == 1
    assert ordering["event_orders"] == sorted(ordering["event_orders"])
    # Non-page events must never enter navigation sequence reports.
    db.execute(
        """INSERT INTO analytics_events
           (user_id,session_id,event_name,page_path,occurred_at)
           VALUES (%s,%s,'click','/private-component',now())""",
        (users[0][0]["id"], second_session),
    )
    db.commit()

    # Aggregate route patterns require at least five distinct opted-in users.
    aggregate = client.get(
        "/analytics/admin/paths?days=30&max_steps=3",
        headers=_auth(superadmin_token),
    )
    assert aggregate.status_code == 200
    assert aggregate.json()["suppressed"] is False
    aggregate_pattern = next(
        item for item in aggregate.json()["paths"]
        if item["path"] == ["/", "/market", "/company/:id"]
    )
    assert aggregate_pattern["repetitions"] == 10
    assert aggregate_pattern["sessions"] == 5
    assert aggregate_pattern["users"] == 5

    # User-level inspection is denied to an ordinary account.
    forbidden = client.get(
        f"/analytics/admin/users/{users[0][0]['id']}/paths",
        headers=_auth(users[0][1]),
    )
    assert forbidden.status_code == 403

    personal = client.get(
        f"/analytics/admin/users/{users[0][0]['id']}/paths?days=30&max_steps=3",
        headers=_auth(superadmin_token),
    )
    assert personal.status_code == 200
    personal_paths = personal.json()["paths"]

    full_path = next(
        item for item in personal_paths
        if item["path"] == ["/", "/market", "/company/:id"]
    )
    assert full_path["repetitions"] == 2
    assert full_path["sessions"] == 1
    assert full_path["users"] == 1

    partial_path = next(
        item for item in personal_paths
        if item["path"] == ["/market", "/company/:id"]
    )
    assert partial_path["repetitions"] == 3
    assert partial_path["sessions"] == 2
    assert not any("/private-component" in item["path"] for item in personal_paths)

    with db.cursor() as cur:
        cur.execute(
            """SELECT COUNT(*) AS total FROM admin_audit_logs
               WHERE actor_user_id=%s AND action='analytics.user_paths.read'
                 AND target_id=%s""",
            (superadmin["id"], str(users[0][0]["id"])),
        )
        assert cur.fetchone()["total"] == 1


def test_aggregate_repeated_paths_are_suppressed_below_minimum_cohort(db, client):
    user, _token = _new_user(db, "phase9_small_path_cohort_user")
    admin, admin_token = _new_user(db, "phase9_small_path_cohort_admin")
    _grant_role(db, admin["id"], "superadmin")

    for index in range(4):
        cohort_user, _ = (
            (user, None) if index == 0
            else _new_user(db, f"phase9_small_path_cohort_{index}")
        )
        db.execute(
            """INSERT INTO analytics_consents
               (user_id,essential,product_analytics,consent_version)
               VALUES (%s,TRUE,TRUE,'v1')""",
            (cohort_user["id"],),
        )
        _insert_page_view_sequence(
            db, cohort_user["id"], uuid4(), ["/", "/market", "/", "/market"]
        )

    response = client.get(
        "/analytics/admin/paths?days=30&max_steps=2",
        headers=_auth(admin_token),
    )
    assert response.status_code == 200
    assert response.json()["suppressed"] is True
    assert response.json()["paths"] == []


def test_phone_otp_delivery_creates_standard_account_and_bootstraps_only_configured_superadmin(
    db, client, monkeypatch
):
    from cryptography.fernet import Fernet
    from saas_core.auth import repository as auth_repository
    from saas_core.notifications import dispatch_sms_batch

    monkeypatch.setenv("SAAS_OTP_HMAC_SECRET", "phase10-phone-otp-secret-" + "x" * 32)
    encryption_key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("SAAS_OUTBOX_FERNET_KEY", encryption_key)

    superadmin_phone = "+989000000001"
    standard_phone = "+989000000002"
    monkeypatch.setenv("SAAS_SUPERADMIN_PHONE_E164", superadmin_phone)
    monkeypatch.setenv("SAAS_TERMS_VERSION", "terms-test-v1")
    monkeypatch.setenv("SAAS_PRIVACY_VERSION", "privacy-test-v1")
    monkeypatch.setattr(auth_repository, "new_otp", lambda: "135790")

    auth_repository.request_phone_otp(db, superadmin_phone, "signup")
    auth_repository.request_phone_otp(db, standard_phone, "signup")

    class RecordingSmsSender:
        def __init__(self):
            self.sent = []

        def send(self, receptor, message):
            self.sent.append((receptor, message))

    sender = RecordingSmsSender()
    result = dispatch_sms_batch(db, sender=sender, encryption_key=encryption_key, batch_size=10)
    assert result == {"claimed": 2, "delivered": 2, "deferred": 0, "failed": 0}
    assert {item[0] for item in sender.sent} == {superadmin_phone, standard_phone}
    assert all("135790" in item[1] for item in sender.sent)

    superadmin_token, _ = auth_repository.consume_phone_otp(
        db, superadmin_phone, "135790", "signup",
        terms_version="terms-test-v1", privacy_version="privacy-test-v1",
    )
    standard_token, _ = auth_repository.consume_phone_otp(
        db, standard_phone, "135790", "signup",
        terms_version="terms-test-v1", privacy_version="privacy-test-v1",
    )

    superadmin_me = client.get("/auth/me", headers=_auth(superadmin_token))
    standard_me = client.get("/auth/me", headers=_auth(standard_token))
    assert superadmin_me.status_code == 200
    assert standard_me.status_code == 200
    assert superadmin_me.json()["user"]["is_superadmin"] is True
    assert "superadmin" in superadmin_me.json()["user"]["roles"]
    assert standard_me.json()["user"]["is_superadmin"] is False
    assert standard_me.json()["user"]["roles"] == ["user"]

    # Bootstrap grants the role, not an MFA bypass. The new admin session
    # must enroll and verify TOTP before any superadmin endpoint is available.
    monkeypatch.setenv("SAAS_ADMIN_MFA_FERNET_KEY", Fernet.generate_key().decode("ascii"))
    mfa_setup = client.post("/auth/mfa/setup", headers=_auth(superadmin_token))
    assert mfa_setup.status_code == 200, mfa_setup.text
    mfa_confirm = client.post(
        "/auth/mfa/confirm",
        headers=_auth(superadmin_token),
        json={"code": __import__("saas_core.auth.mfa", fromlist=["totp_code"]).totp_code(
            mfa_setup.json()["secret"]
        )},
    )
    assert mfa_confirm.status_code == 200, mfa_confirm.text
    assert client.get("/admin/dashboard", headers=_auth(superadmin_token)).status_code == 200
    assert client.get("/admin/dashboard", headers=_auth(standard_token)).status_code == 403

    with db.cursor() as cur:
        cur.execute(
            """SELECT COUNT(*) AS total FROM admin_audit_logs
               WHERE action='auth.bootstrap_superadmin.granted'
                 AND target_id=%s""",
            (superadmin_me.json()["user"]["id"],),
        )
        assert cur.fetchone()["total"] == 1


def test_unconfigured_superadmin_phone_never_grants_elevated_role(db, monkeypatch):
    from cryptography.fernet import Fernet
    from saas_core.auth import repository as auth_repository

    monkeypatch.setenv("SAAS_OTP_HMAC_SECRET", "phase10-unconfigured-otp-secret-" + "x" * 32)
    monkeypatch.setenv("SAAS_OUTBOX_FERNET_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("SAAS_TERMS_VERSION", "terms-test-v1")
    monkeypatch.setenv("SAAS_PRIVACY_VERSION", "privacy-test-v1")
    monkeypatch.delenv("SAAS_SUPERADMIN_PHONE_E164", raising=False)
    monkeypatch.setattr(auth_repository, "new_otp", lambda: "246802")
    phone = "+989000000003"

    auth_repository.request_phone_otp(db, phone, "signup")
    token, _ = auth_repository.consume_phone_otp(
        db, phone, "246802", "signup",
        terms_version="terms-test-v1", privacy_version="privacy-test-v1",
    )
    assert token
    with db.cursor() as cur:
        cur.execute(
            """SELECT array_agg(r.code ORDER BY r.code) AS roles
               FROM users u JOIN user_roles ur ON ur.user_id=u.id
               JOIN roles r ON r.id=ur.role_id WHERE u.phone_e164=%s""",
            (phone,),
        )
        assert cur.fetchone()["roles"] == ["user"]
