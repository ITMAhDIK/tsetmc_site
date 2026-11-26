"""Real PostgreSQL integration tests for Phase 4 payment lifecycle.

These tests only run against the disposable SAAS_TEST_DATABASE_URL database.
They never connect to the production SaaS or market-data databases.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from uuid import uuid4

import pytest
import psycopg
from psycopg.rows import dict_row

from saas_core.payments.domain import PaymentRejected, VerifiedPayment
from saas_core.payments.repository import (
    attach_provider_checkout, claim_refund, create_payment_attempt,
    create_subscription_order, enqueue_stale_payment_reconciliation,
    expire_payment_orders, finish_refund, list_payment_reconciliation_cases,
    request_full_refund,
)
from saas_core.payments.settlement import settle_provider_callback
from saas_core.payments.worker import process_refund

DSN = os.getenv("SAAS_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="SAAS_TEST_DATABASE_URL is not configured")


@pytest.fixture(scope="session", autouse=True)
def migrated_test_database():
    root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    schema_dir = os.path.join(root, "saas_core", "schema")
    with psycopg.connect(DSN, autocommit=True, row_factory=dict_row) as conn:
        for filename in ("001_initial.sql", "002_payment_lifecycle.sql", "002_payment_lifecycle.sql"):
            with open(os.path.join(schema_dir, filename), encoding="utf-8") as stream:
                conn.execute(stream.read())
    yield


def connect():
    return psycopg.connect(DSN, row_factory=dict_row)


def make_user(conn, *, verified=True):
    user_id = uuid4()
    phone = f"+4915{str(user_id.int)[:10]}"
    with conn.transaction():
        conn.execute(
            """INSERT INTO users(id,public_id,username,status,phone_e164,phone_verified_at)
               VALUES (%s,%s,%s,'active',%s,%s)""",
            (user_id, str(user_id)[:32], f"pay-{user_id.hex[:16]}",
             phone if verified else None, datetime.now(timezone.utc) if verified else None),
        )
    return user_id


def make_pending_order(conn, user_id=None):
    user_id = user_id or make_user(conn)
    order = create_subscription_order(
        conn, user_id=user_id, plan_code="premium_monthly",
        idempotency_key=f"order-{uuid4()}",
    )
    attempt = create_payment_attempt(conn, order_id=order["id"], provider="snappay")
    provider_ref = f"provider-{uuid4()}"
    attach_provider_checkout(
        conn, attempt_id=attempt["id"], provider_order_id=provider_ref,
        redirect_url="https://sandbox.invalid/checkout", request_ref="request-ref",
    )
    return user_id, order, attempt, provider_ref


def verified_for(provider_ref, event_id=None, amount=899000):
    return VerifiedPayment(
        provider_event_id=event_id or f"event-{uuid4()}",
        provider_order_id=provider_ref,
        amount_toman=amount,
        success=True,
        reference="sandbox-reference",
    )


def test_order_idempotency_and_server_side_price(migrated_test_database):
    with connect() as conn:
        user_id = make_user(conn)
        key = f"same-key-{uuid4()}"
        first = create_subscription_order(conn, user_id=user_id,
            plan_code="premium_monthly", idempotency_key=key)
        second = create_subscription_order(conn, user_id=user_id,
            plan_code="premium_monthly", idempotency_key=key)
        assert first["id"] == second["id"]
        assert first["amount_toman"] == 899000
        other_user = make_user(conn)
        with pytest.raises(ValueError, match="different order"):
            create_subscription_order(conn, user_id=other_user,
                plan_code="premium_monthly", idempotency_key=key)


def test_purchase_requires_verified_phone(migrated_test_database):
    with connect() as conn:
        user_id = make_user(conn, verified=False)
        with pytest.raises(PermissionError, match="verified phone"):
            create_subscription_order(conn, user_id=user_id,
                plan_code="premium_monthly", idempotency_key=f"unverified-{uuid4()}")


def test_settlement_is_atomic_and_callback_retry_is_idempotent(migrated_test_database):
    with connect() as conn:
        _, order, attempt, provider_ref = make_pending_order(conn)
        verified = verified_for(provider_ref, event_id=f"event-{uuid4()}")
        first = settle_provider_callback(
            conn, order_id=order["id"], attempt_id=attempt["id"],
            provider="snappay", verified=verified, payload_sha256="a" * 64,
        )
        second = settle_provider_callback(
            conn, order_id=order["id"], attempt_id=attempt["id"],
            provider="snappay", verified=verified, payload_sha256="a" * 64,
        )
        assert first["already_processed"] is False
        assert second["already_processed"] is True
        assert conn.execute("SELECT status FROM payment_orders WHERE id=%s",
                            (order["id"],)).fetchone()["status"] == "paid"
        assert conn.execute("SELECT count(*) AS n FROM subscriptions WHERE payment_order_id=%s",
                            (order["id"],)).fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM gateway_events WHERE order_id=%s",
                            (order["id"],)).fetchone()["n"] == 1


def test_same_callback_concurrently_creates_one_subscription(migrated_test_database):
    with connect() as conn:
        _, order, attempt, provider_ref = make_pending_order(conn)
    verified = verified_for(provider_ref, event_id=f"concurrent-{uuid4()}")

    def settle_once():
        with connect() as local:
            return settle_provider_callback(
                local, order_id=order["id"], attempt_id=attempt["id"],
                provider="snappay", verified=verified, payload_sha256="b" * 64,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: settle_once(), range(2)))
    assert sorted(result["already_processed"] for result in results) == [False, True]
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM subscriptions WHERE payment_order_id=%s",
                            (order["id"],)).fetchone()["n"] == 1


def test_callback_amount_mismatch_does_not_mark_order_paid(migrated_test_database):
    with connect() as conn:
        _, order, attempt, provider_ref = make_pending_order(conn)
        bad = verified_for(provider_ref, amount=899001)
        with pytest.raises(PaymentRejected, match="amount"):
            settle_provider_callback(
                conn, order_id=order["id"], attempt_id=attempt["id"],
                provider="snappay", verified=bad, payload_sha256="c" * 64,
            )
        assert conn.execute("SELECT status FROM payment_orders WHERE id=%s",
                            (order["id"],)).fetchone()["status"] == "pending"


def test_expiry_cancels_orders_and_releases_attempts(migrated_test_database):
    with connect() as conn:
        _, order, attempt, _ = make_pending_order(conn)
        conn.execute("UPDATE payment_orders SET expires_at=now()-interval '1 minute' WHERE id=%s",
                     (order["id"],))
        assert expire_payment_orders(conn) >= 1
        assert conn.execute("SELECT status FROM payment_orders WHERE id=%s",
                            (order["id"],)).fetchone()["status"] == "cancelled"
        assert conn.execute("SELECT status FROM payment_attempts WHERE id=%s",
                            (attempt["id"],)).fetchone()["status"] == "expired"


def test_refund_request_is_idempotent_and_full_refund_reverses_subscription(migrated_test_database):
    with connect() as conn:
        user_id, order, attempt, provider_ref = make_pending_order(conn)
        settle_provider_callback(
            conn, order_id=order["id"], attempt_id=attempt["id"], provider="snappay",
            verified=verified_for(provider_ref), payload_sha256="d" * 64,
        )
        key = f"refund-{uuid4()}"
        first = request_full_refund(conn, order_id=order["id"], requested_by=user_id,
                                    idempotency_key=key, reason="duplicate purchase")
        second = request_full_refund(conn, order_id=order["id"], requested_by=user_id,
                                     idempotency_key=key, reason="duplicate purchase")
        assert first["id"] == second["id"]
        claimed = claim_refund(conn, refund_id=first["id"])
        assert claimed is not None
        finish_refund(conn, refund_id=first["id"], provider_refund_id=f"refund-provider-{uuid4()}")
        assert conn.execute("SELECT status FROM payment_orders WHERE id=%s",
                            (order["id"],)).fetchone()["status"] == "refunded"
        assert conn.execute("SELECT status FROM subscriptions WHERE payment_order_id=%s",
                            (order["id"],)).fetchone()["status"] == "refunded"


def test_stale_pending_payments_enter_reconciliation_queue(migrated_test_database):
    with connect() as conn:
        _, order, attempt, _ = make_pending_order(conn)
        conn.execute("UPDATE payment_attempts SET created_at=now()-interval '2 hours' WHERE id=%s",
                     (attempt["id"],))
        assert enqueue_stale_payment_reconciliation(conn, older_than_minutes=30) >= 1
        cases = list_payment_reconciliation_cases(conn)
        assert any(case["payment_attempt_id"] == attempt["id"] for case in cases)


def test_refund_worker_keeps_request_queued_until_gateway_is_configured(migrated_test_database):
    with connect() as conn:
        user_id, order, attempt, provider_ref = make_pending_order(conn)
        settle_provider_callback(
            conn, order_id=order["id"], attempt_id=attempt["id"], provider="snappay",
            verified=verified_for(provider_ref), payload_sha256="e" * 64,
        )
        refund = request_full_refund(
            conn, order_id=order["id"], requested_by=user_id,
            idempotency_key=f"worker-refund-{uuid4()}",
        )
        result = process_refund(conn, refund_id=refund["id"])
        assert result == {"processed": False, "reason": "provider_not_configured"}
        status = conn.execute("SELECT status FROM payment_refunds WHERE id=%s",
                              (refund["id"],)).fetchone()["status"]
        assert status == "requested"
