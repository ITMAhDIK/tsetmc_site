"""Real PostgreSQL integration tests for the Phase 8 admin domain.

The suite is enabled only when SAAS_TEST_DATABASE_URL points at a disposable
SaaS test database whose schema migrations 001–005 have been applied.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import pytest

from saas_core.domain.admin_console import AdminConflict, AdminConsoleService
from saas_core.http.admin_api import CSRF_COOKIE, SESSION_COOKIE, SaasAdminAPI


DATABASE_URL = os.environ.get("SAAS_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="SAAS_TEST_DATABASE_URL is not configured; PostgreSQL integration test skipped",
)


def _connect():
    psycopg = pytest.importorskip("psycopg")
    return psycopg.connect(DATABASE_URL)


def _user(cursor, label: str) -> uuid.UUID:
    public_id = "IT-" + uuid.uuid4().hex[:24]
    cursor.execute(
        "INSERT INTO users(public_id,display_name) VALUES (%s,%s) RETURNING id",
        (public_id, label),
    )
    return cursor.fetchone()[0]


def _service() -> AdminConsoleService:
    return AdminConsoleService(
        connection_factory=_connect,
        # The domain receives an authoritative permission decision as it would
        # from the authenticated SaaS application boundary. These tests focus
        # on the real PostgreSQL transaction, constraints and audit behavior.
        permission_checker=lambda _actor, _permission: True,
    )


def test_payment_review_and_audit_commit_without_settling_the_order():
    actor = uuid.uuid4()
    with _connect() as connection, connection.cursor() as cursor:
        actor = _user(cursor, "Phase 8 integration reviewer")
        owner = _user(cursor, "Phase 8 integration customer")
        cursor.execute(
            """
            INSERT INTO payment_orders
              (user_id,amount_toman,status,idempotency_key,phone_e164)
            VALUES (%s,250000,'pending',%s,'+989120000001')
            RETURNING id
            """,
            (owner, "it-payment-" + uuid.uuid4().hex),
        )
        order_id = cursor.fetchone()[0]

    result = _service().review_payment(
        actor_user_id=actor,
        order_id=order_id,
        outcome="provider_followup",
        note="Integration review; provider evidence is required.",
        request_id="it-payment-review",
    )

    assert result["order_status_unchanged"] == "pending"
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT status FROM payment_orders WHERE id=%s", (order_id,))
        assert cursor.fetchone()[0] == "pending"
        cursor.execute(
            """
            SELECT outcome,note FROM admin_payment_reviews
            WHERE order_id=%s AND actor_user_id=%s
            """,
            (order_id, actor),
        )
        assert cursor.fetchone() == (
            "provider_followup",
            "Integration review; provider evidence is required.",
        )
        cursor.execute(
            """
            SELECT action,request_id FROM admin_audit_logs
            WHERE actor_user_id=%s AND target_id=%s
            """,
            (actor, str(order_id)),
        )
        assert cursor.fetchone() == (
            "payments.reconciliation.review",
            "it-payment-review",
        )


def test_last_active_superadmin_guard_rolls_back_real_status_change():
    target = uuid.uuid4()
    with _connect() as connection, connection.cursor() as cursor:
        # Keep this invariant test independent of superadmins created by other
        # integration cases in the same disposable database.
        cursor.execute(
            """
            UPDATE users SET status='suspended'
            WHERE id IN (
              SELECT ur.user_id FROM user_roles ur
              JOIN roles r ON r.id=ur.role_id WHERE r.code='superadmin'
            )
            """
        )
        target = _user(cursor, "Only active superadmin")
        cursor.execute("SELECT id FROM roles WHERE code='superadmin'")
        role_id = cursor.fetchone()[0]
        cursor.execute(
            "INSERT INTO user_roles(user_id,role_id) VALUES (%s,%s)",
            (target, role_id),
        )

    with pytest.raises(AdminConflict, match="last active superadmin"):
        _service().set_user_status(
            actor_user_id=target,
            target_user_id=target,
            status="suspended",
            request_id="it-last-superadmin",
        )

    with pytest.raises(AdminConflict, match="last active superadmin"):
        _service().revoke_role(
            actor_user_id=target,
            target_user_id=target,
            role_code="superadmin",
            request_id="it-last-superadmin-role",
        )

    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT status FROM users WHERE id=%s", (target,))
        assert cursor.fetchone()[0] == "active"
        cursor.execute(
            """
            SELECT count(*) FROM user_roles ur
            JOIN roles r ON r.id=ur.role_id
            WHERE ur.user_id=%s AND r.code='superadmin'
            """,
            (target,),
        )
        assert cursor.fetchone()[0] == 1
        cursor.execute(
            "SELECT count(*) FROM admin_audit_logs WHERE target_id=%s",
            (str(target),),
        )
        assert cursor.fetchone()[0] == 0



def _session(cursor, user_id):
    token = uuid.uuid4().hex + uuid.uuid4().hex
    csrf = uuid.uuid4().hex + uuid.uuid4().hex
    cursor.execute(
        """
        INSERT INTO sessions
          (user_id,token_hash,expires_at,mfa_verified_at,csrf_token_hash)
        VALUES (%s,%s,now()+interval '1 hour',now(),%s)
        """,
        (
            user_id,
            hashlib.sha256(token.encode("utf-8")).hexdigest(),
            hashlib.sha256(csrf.encode("utf-8")).hexdigest(),
        ),
    )
    return token, csrf


def _wsgi_get(api, token, csrf, path):
    captured = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = headers

    environ = {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": path,
        "QUERY_STRING": "",
        "HTTP_COOKIE": f"{SESSION_COOKIE}={token}; {CSRF_COOKIE}={csrf}",
        "wsgi.input": BytesIO(b""),
        "CONTENT_LENGTH": "0",
        "SERVER_NAME": "admin.example.test",
        "SERVER_PORT": "443",
        "wsgi.url_scheme": "https",
    }
    body = b"".join(api(environ, start_response))
    return captured["status"], json.loads(body.decode("utf-8"))


def test_admin_http_api_authenticates_mfa_session_and_enforces_database_roles():
    with _connect() as connection, connection.cursor() as cursor:
        superadmin = _user(cursor, "HTTP integration superadmin")
        regular_user = _user(cursor, "HTTP integration regular user")
        cursor.execute("SELECT id FROM roles WHERE code='superadmin'")
        superadmin_role = cursor.fetchone()[0]
        cursor.execute(
            "INSERT INTO user_roles(user_id,role_id) VALUES (%s,%s)",
            (superadmin, superadmin_role),
        )
        superadmin_session = _session(cursor, superadmin)
        regular_session = _session(cursor, regular_user)

    api = SaasAdminAPI(
        connection_factory=_connect,
        allowed_origins={"https://admin.example.test"},
    )

    status, payload = _wsgi_get(
        api, *superadmin_session, "/api/v1/admin/overview"
    )
    assert status.startswith("200")
    assert payload["ok"] is True
    assert "users_total" in payload["data"]

    status, payload = _wsgi_get(
        api, *regular_session, "/api/v1/admin/overview"
    )
    assert status.startswith("403")
    assert payload["ok"] is False
    assert payload["error"]["code"] == "permission_denied"



def test_superadmin_role_revocation_waits_for_global_role_lock():
    with _connect() as connection, connection.cursor() as cursor:
        actor = _user(cursor, "Concurrent revocation actor")
        target = _user(cursor, "Concurrent revocation target")
        cursor.execute("SELECT id FROM roles WHERE code='superadmin'")
        role_id = cursor.fetchone()[0]
        cursor.executemany(
            "INSERT INTO user_roles(user_id,role_id) VALUES (%s,%s)",
            [(actor, role_id), (target, role_id)],
        )

    # Hold the shared role row lock. A revocation must wait here rather than
    # count the superadmin set without serialization.
    lock_connection = _connect()
    lock_cursor = lock_connection.cursor()
    lock_cursor.execute("SELECT id FROM roles WHERE code='superadmin' FOR UPDATE")
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(
        _service().revoke_role,
        actor_user_id=actor,
        target_user_id=target,
        role_code="superadmin",
        request_id="it-role-lock",
    )
    try:
        deadline = time.monotonic() + 5
        waiting_on_role_lock = False
        while time.monotonic() < deadline:
            with _connect() as probe, probe.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT EXISTS (
                      SELECT 1 FROM pg_stat_activity
                      WHERE datname=current_database()
                        AND pid<>pg_backend_pid()
                        AND wait_event_type='Lock'
                        AND query ILIKE '%SELECT id FROM roles%'
                        AND query ILIKE '%FOR UPDATE%'
                    )
                    """
                )
                waiting_on_role_lock = cursor.fetchone()[0]
            if waiting_on_role_lock:
                break
            time.sleep(0.05)
        assert waiting_on_role_lock, "role revocation did not wait for the superadmin lock"
    finally:
        # Release the row lock before waiting for the worker thread to finish.
        lock_connection.commit()
        lock_cursor.close()
        lock_connection.close()
        executor.shutdown(wait=True)

    result = future.result(timeout=5)
    assert result["revoked"] is True
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*) FROM user_roles ur
            JOIN roles r ON r.id=ur.role_id
            WHERE ur.user_id=%s AND r.code='superadmin'
            """,
            (actor,),
        )
        assert cursor.fetchone()[0] == 1
