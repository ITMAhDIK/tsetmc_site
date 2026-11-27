from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

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
              "wallet_ledger_entries", "withdrawal_requests", "referrals"],),
        ).fetchone()["n"]
        if missing:
            pytest.fail("Commerce migrations are incomplete")


@pytest.fixture()
def db(verify_commerce_database):
    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        conn.execute(
            """TRUNCATE TABLE users CASCADE"""
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
