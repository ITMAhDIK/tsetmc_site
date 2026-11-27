from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

import commerce_service.identity as identity

DSN = os.getenv("COMMERCE_TEST_DATABASE_URL", "").strip()
EXPECTED = os.getenv("COMMERCE_TEST_DATABASE_NAME", "").strip()
pytestmark = pytest.mark.skipif(
    not DSN or not EXPECTED,
    reason="requires a disposable Commerce PostgreSQL database",
)
USER_ID = uuid4()
USER_TOKEN = "opaque-commerce-test-session"


@pytest.fixture(scope="module", autouse=True)
def verify_commerce_database():
    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        name = conn.execute("SELECT current_database() AS name").fetchone()["name"]
        if name != EXPECTED:
            pytest.fail("Commerce tests refuse an unexpected or non-disposable database")
        missing = conn.execute(
            """SELECT count(*) AS n FROM unnest(%s::text[]) t(name)
               WHERE to_regclass('public.'||t.name) IS NULL""",
            (["users", "plans", "payment_orders", "subscriptions",
              "wallet_ledger_entries", "withdrawal_requests", "referrals", "wallet_order_payments"],),
        ).fetchone()["n"]
        if missing:
            pytest.fail("Commerce migrations are incomplete")


@pytest.fixture()
def db(verify_commerce_database):
    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        conn.execute(
            """TRUNCATE TABLE users CASCADE"""
        )
        conn.execute(
            """UPDATE plans
               SET duration_days=CASE code
                   WHEN 'premium_monthly' THEN 30
                   WHEN 'premium_quarterly' THEN 90
                   ELSE duration_days END,
                   price_toman=CASE code
                   WHEN 'premium_monthly' THEN 899000
                   WHEN 'premium_quarterly' THEN 2499000
                   ELSE price_toman END"""
        )
        conn.commit()
        yield conn
        conn.rollback()


@pytest.fixture()
def client(monkeypatch):
    from commerce_service.api import app
    from commerce_service.db import get_conn

    principal = {
        "user": {
            "id": str(USER_ID),
            "status": "active",
            "phone_e164": "+989121234567",
            "phone_verified_at": datetime.now(timezone.utc).isoformat(),
        },
        "roles": ["user"],
        "permissions": [],
        "mfa_enabled": False,
    }
    monkeypatch.setattr(identity, "_request_identity", lambda token: principal)
    monkeypatch.setenv("COMMERCE_SERVICE_TOKEN", "commerce-test-service-token-" + "c" * 32)

    def override():
        with psycopg.connect(DSN, row_factory=dict_row) as conn:
            yield conn

    app.dependency_overrides[get_conn] = override
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_conn, None)


def auth_headers():
    return {"Authorization": f"Bearer {USER_TOKEN}"}


def test_health_and_plan_catalog(client):
    assert client.get("/health").json() == {"status": "ok", "service": "commerce"}
    response = client.get("/plans")
    assert response.status_code == 200
    prices = {plan["code"]: plan["price_toman"] for plan in response.json()["plans"]}
    assert prices == {"premium_monthly": 899000, "premium_quarterly": 2499000}


def test_order_uses_server_plan_price_and_is_idempotent(client, db):
    payload = {"plan_code": "premium_monthly", "amount_toman": 1}
    headers = {**auth_headers(), "Idempotency-Key": "commerce-order-key-001"}
    first = client.post("/payments/orders", headers=headers, json=payload)
    assert first.status_code == 201, first.text
    order = first.json()["order"]
    assert order["amount_toman"] == 899000
    assert order["status"] == "created"
    repeated = client.post("/payments/orders", headers=headers, json={"plan_code": "premium_monthly"})
    assert repeated.status_code == 201
    assert repeated.json()["order"]["id"] == order["id"]
    conflict = client.post("/payments/orders", headers=headers, json={"plan_code": "premium_quarterly"})
    assert conflict.status_code == 409


def test_purchase_requires_identity_verified_phone(client, monkeypatch):
    unverified = {
        "user": {"id": str(USER_ID), "status": "active",
                 "phone_e164": "+989121234567", "phone_verified_at": None},
        "roles": ["user"], "permissions": [],
    }
    monkeypatch.setattr(identity, "_request_identity", lambda token: unverified)
    response = client.post(
        "/payments/orders",
        headers={**auth_headers(), "Idempotency-Key": "commerce-unverified-001"},
        json={"plan_code": "premium_monthly"},
    )
    assert response.status_code == 403


def test_identity_outage_fails_closed(client, monkeypatch):
    def unavailable(_token):
        raise identity.IdentityUnavailable("offline")
    monkeypatch.setattr(identity, "_request_identity", unavailable)
    response = client.get("/wallet", headers=auth_headers())
    assert response.status_code == 503


def test_cookie_write_requires_same_origin_csrf_proof(client):
    client.cookies.set("tloomberg_session", USER_TOKEN)
    response = client.post(
        "/payments/orders",
        headers={"Idempotency-Key": "commerce-csrf-test-001"},
        json={"plan_code": "premium_monthly"},
    )
    assert response.status_code == 403


def test_wallet_balances_and_ledger_are_user_scoped(client, db):
    created = client.get("/wallet", headers=auth_headers())
    assert created.status_code == 200
    from commerce_service.wallet.wallet_repository import post_ledger_entry
    post_ledger_entry(
        db, user_id=USER_ID, bucket="cash", entry_type="topup",
        amount_toman=500000, idempotency_key="commerce-ledger-cash-001",
    )
    post_ledger_entry(
        db, user_id=USER_ID, bucket="gift", entry_type="referral_reward",
        amount_toman=100000, idempotency_key="commerce-ledger-gift-001",
    )
    db.commit()
    response = client.get("/wallet", headers=auth_headers())
    assert response.status_code == 200
    assert response.json()["cash_toman"] == 500000
    assert response.json()["gift_toman"] == 100000
    assert response.json()["withdrawable_toman"] == 500000
    assert len(client.get("/wallet/ledger", headers=auth_headers()).json()["entries"]) == 2


def test_withdrawal_is_fail_closed_without_verified_bank_account(client, db):
    client.get("/wallet", headers=auth_headers())
    from commerce_service.wallet.wallet_repository import post_ledger_entry
    post_ledger_entry(
        db, user_id=USER_ID, bucket="cash", entry_type="topup",
        amount_toman=5000, idempotency_key="commerce-withdrawal-test-cash-001",
    )
    db.commit()
    response = client.post(
        "/wallet/withdrawals",
        headers={**auth_headers(), "Idempotency-Key": "commerce-withdrawal-001"},
        json={"amount_toman": 1000, "bank_account_id": str(uuid4())},
    )
    assert response.status_code == 403


def test_internal_entitlements_require_service_token_and_return_active_plan(client, db):
    client.get("/wallet", headers=auth_headers())
    now = datetime.now(timezone.utc)
    plan_id = db.execute("SELECT id FROM plans WHERE code='premium_monthly'").fetchone()["id"]
    db.execute(
        """INSERT INTO subscriptions(user_id,plan_id,status,starts_at,ends_at)
           VALUES (%s,%s,'active',%s,%s)""",
        (USER_ID, plan_id, now - timedelta(days=1), now + timedelta(days=29)),
    )
    db.commit()
    path = f"/internal/users/{USER_ID}/entitlements"
    denied = client.get(path)
    assert denied.status_code == 401
    allowed = client.get(
        path,
        headers={"X-Commerce-Service-Token": "commerce-test-service-token-" + "c" * 32},
    )
    assert allowed.status_code == 200
    assert all(allowed.json()["entitlements"].values())


def test_subscription_history_is_read_from_commerce(client, db):
    client.get("/wallet", headers=auth_headers())
    response = client.get("/subscriptions", headers=auth_headers())
    assert response.status_code == 200
    assert response.json()["subscriptions"] == []


def test_wallet_ledger_is_database_enforced_append_only(client, db):
    client.get("/wallet", headers=auth_headers())
    from commerce_service.wallet.wallet_repository import post_ledger_entry
    row = post_ledger_entry(
        db, user_id=USER_ID, bucket="cash", entry_type="topup",
        amount_toman=5000, idempotency_key="commerce-immutable-ledger-001",
    )
    db.commit()
    with pytest.raises(psycopg.errors.RaiseException):
        db.execute("UPDATE wallet_ledger_entries SET amount_toman=7000 WHERE id=%s", (row["id"],))



def test_wallet_pays_subscription_gift_first_and_is_idempotent(client, db):
    client.get("/wallet", headers=auth_headers())
    from commerce_service.wallet.wallet_repository import post_ledger_entry
    post_ledger_entry(
        db, user_id=USER_ID, bucket="cash", entry_type="topup",
        amount_toman=850000, idempotency_key="wallet-pay-seed-cash-001",
    )
    post_ledger_entry(
        db, user_id=USER_ID, bucket="gift", entry_type="referral_reward",
        amount_toman=100000, idempotency_key="wallet-pay-seed-gift-001",
    )
    db.commit()

    created = client.post(
        "/payments/orders",
        headers={**auth_headers(), "Idempotency-Key": "wallet-pay-order-001"},
        json={"plan_code": "premium_monthly"},
    )
    assert created.status_code == 201, created.text
    order_id = created.json()["order"]["id"]
    # Catalogue edits after order creation must not mutate the purchased term.
    db.execute("UPDATE plans SET duration_days=120 WHERE code='premium_monthly'")
    db.commit()

    headers = {**auth_headers(), "Idempotency-Key": "wallet-pay-once-001"}
    paid = client.post(f"/payments/orders/{order_id}/wallet-pay", headers=headers)
    assert paid.status_code == 200, paid.text
    payment = paid.json()["payment"]
    assert payment["status"] == "paid"
    assert payment["amount_toman"] == 899000
    assert payment["gift_debit_toman"] == 100000
    assert payment["cash_debit_toman"] == 799000
    assert payment["already_processed"] is False

    replay = client.post(f"/payments/orders/{order_id}/wallet-pay", headers=headers)
    assert replay.status_code == 200
    assert replay.json()["payment"]["already_processed"] is True
    changed_key = client.post(
        f"/payments/orders/{order_id}/wallet-pay",
        headers={**auth_headers(), "Idempotency-Key": "wallet-pay-second-key"},
    )
    assert changed_key.status_code == 409

    wallet = client.get("/wallet", headers=auth_headers()).json()
    assert wallet["gift_toman"] == 0
    assert wallet["cash_toman"] == 51000
    subscription = db.execute(
        "SELECT status FROM subscriptions WHERE payment_order_id=%s", (order_id,)
    ).fetchone()
    assert subscription["status"] == "active"
    duration_days = db.execute(
        """SELECT EXTRACT(EPOCH FROM (ends_at-starts_at))/86400 AS days
           FROM subscriptions WHERE payment_order_id=%s""", (order_id,)
    ).fetchone()["days"]
    assert duration_days == 30
    with pytest.raises(psycopg.errors.RaiseException):
        db.execute(
            "UPDATE wallet_order_payments SET amount_toman=1 WHERE order_id=%s",
            (order_id,),
        )


def test_wallet_payment_cannot_spend_cash_reserved_for_withdrawal(client, db):
    client.get("/wallet", headers=auth_headers())
    from commerce_service.wallet.wallet_repository import post_ledger_entry
    post_ledger_entry(
        db, user_id=USER_ID, bucket="cash", entry_type="topup",
        amount_toman=899000, idempotency_key="wallet-reserve-seed-cash-001",
    )
    db.execute(
        """INSERT INTO withdrawal_requests
           (user_id,amount_toman,status,bank_account_token,idempotency_key)
           VALUES (%s,200000,'requested','vault-reference:test','wallet-reserve-001')""",
        (USER_ID,),
    )
    db.commit()
    created = client.post(
        "/payments/orders",
        headers={**auth_headers(), "Idempotency-Key": "wallet-reserve-order-001"},
        json={"plan_code": "premium_monthly"},
    )
    order_id = created.json()["order"]["id"]
    response = client.post(
        f"/payments/orders/{order_id}/wallet-pay",
        headers={**auth_headers(), "Idempotency-Key": "wallet-reserve-pay-001"},
    )
    assert response.status_code == 409
    assert client.get("/wallet", headers=auth_headers()).json()["cash_toman"] == 899000
    assert db.execute(
        "SELECT status FROM payment_orders WHERE id=%s", (order_id,)
    ).fetchone()["status"] == "created"


def test_wallet_payment_rejects_order_with_inflight_gateway_attempt(client, db):
    client.get("/wallet", headers=auth_headers())
    created = client.post(
        "/payments/orders",
        headers={**auth_headers(), "Idempotency-Key": "wallet-race-order-001"},
        json={"plan_code": "premium_monthly"},
    )
    order_id = created.json()["order"]["id"]
    from commerce_service.payments.repository import create_payment_attempt, mark_attempt_failed
    attempt = create_payment_attempt(db, order_id=UUID(order_id), provider="snappay")
    mark_attempt_failed(
        db, attempt_id=attempt["id"], failure_code="TimeoutError",
        failure_message="provider outcome is unknown",
    )
    db.commit()
    response = client.post(
        f"/payments/orders/{order_id}/wallet-pay",
        headers={**auth_headers(), "Idempotency-Key": "wallet-race-pay-001"},
    )
    assert response.status_code == 409
