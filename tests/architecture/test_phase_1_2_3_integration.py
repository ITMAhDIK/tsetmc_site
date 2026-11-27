from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

IDENTITY_DSN = os.getenv("IDENTITY_TEST_DATABASE_URL", "").strip()
IDENTITY_EXPECTED = os.getenv("IDENTITY_TEST_DATABASE_NAME", "").strip()
COMMERCE_DSN = os.getenv("COMMERCE_TEST_DATABASE_URL", "").strip()
COMMERCE_EXPECTED = os.getenv("COMMERCE_TEST_DATABASE_NAME", "").strip()
pytestmark = pytest.mark.skipif(
    not all((IDENTITY_DSN, IDENTITY_EXPECTED, COMMERCE_DSN, COMMERCE_EXPECTED)),
    reason="requires separate disposable Identity and Commerce PostgreSQL databases",
)

IDENTITY_SERVICE_TOKEN = "phase123-identity-service-token-" + "i" * 24
COMMERCE_SERVICE_TOKEN = "phase123-commerce-service-token-" + "c" * 24


def _create_market_files(tmp_path: Path):
    source_path = tmp_path / "market-source.sqlite"
    sidecar_path = tmp_path / "market-sidecar.sqlite"

    source = sqlite3.connect(source_path)
    source.executescript(
        """
        CREATE TABLE companies (
            ins_code TEXT PRIMARY KEY, cisin TEXT, total_shares_current INTEGER
        );
        CREATE TABLE trading_days (
            ins_code TEXT, date TEXT, p_first REAL, p_max REAL, p_min REAL,
            p_closing REAL, volume INTEGER
        );
        """
    )
    source.execute(
        "INSERT INTO companies VALUES ('mkt-1','IR-MARKET-001',1000000)"
    )
    source.execute(
        """INSERT INTO trading_days VALUES
           ('mkt-1','2026-11-18',100,110,95,105,9000)"""
    )
    source.commit()
    source.close()

    stat = os.stat(source_path)
    fingerprint = f"{stat.st_size}:{int(stat.st_mtime)}"
    sidecar = sqlite3.connect(sidecar_path)
    sidecar.executescript(
        """
        CREATE TABLE site_meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE site_watch (
            ins_code TEXT PRIMARY KEY, symbol TEXT NOT NULL,
            company_name TEXT, market TEXT NOT NULL,
            total_company_shares INTEGER, latest_date TEXT,
            latest_date_jalali TEXT, latest_price REAL, latest_price_date TEXT
        );
        """
    )
    sidecar.executemany(
        "INSERT INTO site_meta(key,value) VALUES (?,?)",
        [
            ("last_refresh", "2026-11-26T18:00:00"),
            ("sidecar_version", "10"),
            ("db_fingerprint", fingerprint),
        ],
    )
    sidecar.execute(
        """INSERT INTO site_watch
           (ins_code,symbol,company_name,market,total_company_shares,
            latest_date,latest_date_jalali,latest_price,latest_price_date)
           VALUES ('mkt-1','آزمایشی','شرکت آزمایشی','TSE',1000000,
                   '2026-11-18','1405-08-27',105,'2026-11-18')"""
    )
    sidecar.commit()
    sidecar.close()
    return source_path, sidecar_path


@pytest.fixture
def identity_db():
    conn = psycopg.connect(IDENTITY_DSN, row_factory=dict_row)
    actual = conn.execute("SELECT current_database() AS name").fetchone()["name"]
    if actual != IDENTITY_EXPECTED:
        conn.close()
        pytest.fail("three-service integration refuses an unexpected Identity database")
    conn.execute("TRUNCATE TABLE users CASCADE")
    conn.execute("TRUNCATE TABLE phone_verifications,notification_outbox")
    conn.commit()
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture
def commerce_db():
    conn = psycopg.connect(COMMERCE_DSN, row_factory=dict_row)
    actual = conn.execute("SELECT current_database() AS name").fetchone()["name"]
    if actual != COMMERCE_EXPECTED:
        conn.close()
        pytest.fail("three-service integration refuses an unexpected Commerce database")
    conn.execute("TRUNCATE TABLE users CASCADE")
    conn.commit()
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture
def identity_client(monkeypatch):
    from identity_service.api import app, get_conn

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("IDENTITY_SERVICE_TOKEN", IDENTITY_SERVICE_TOKEN)
    monkeypatch.setenv("IDENTITY_OTP_HMAC_SECRET", "test-otp-hmac-secret-" + "o" * 32)
    from cryptography.fernet import Fernet
    monkeypatch.setenv("IDENTITY_OUTBOX_FERNET_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("IDENTITY_ADMIN_MFA_FERNET_KEY", Fernet.generate_key().decode("ascii"))

    def override():
        with psycopg.connect(IDENTITY_DSN, row_factory=dict_row) as conn:
            yield conn

    app.dependency_overrides[get_conn] = override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_conn, None)


@pytest.fixture
def commerce_client(monkeypatch, identity_client):
    import commerce_service.identity as commerce_identity
    from commerce_service.api import app
    from commerce_service.db import get_conn

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("COMMERCE_SERVICE_TOKEN", COMMERCE_SERVICE_TOKEN)
    monkeypatch.setenv("IDENTITY_SERVICE_TOKEN", IDENTITY_SERVICE_TOKEN)

    def identity_http_introspection(token: str):
        response = identity_client.post(
            "/internal/introspect",
            headers={"X-Identity-Service-Token": IDENTITY_SERVICE_TOKEN},
            json={"token": token},
        )
        if response.status_code == 401:
            raise commerce_identity.IdentityUnauthorized()
        if response.status_code != 200:
            raise commerce_identity.IdentityUnavailable("Identity request failed")
        return response.json()["principal"]

    monkeypatch.setattr(
        commerce_identity, "_request_identity", identity_http_introspection
    )

    def override():
        with psycopg.connect(COMMERCE_DSN, row_factory=dict_row) as conn:
            yield conn

    app.dependency_overrides[get_conn] = override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_conn, None)


def test_identity_commerce_and_market_data_run_on_independent_stores(
    tmp_path, monkeypatch, identity_db, commerce_db,
    identity_client, commerce_client,
):
    assert IDENTITY_EXPECTED != COMMERCE_EXPECTED
    from saas_core.auth.service import hash_password, hash_session_token

    user_id = identity_db.execute(
        """INSERT INTO users(public_id,username,password_hash,phone_e164,phone_verified_at)
           VALUES (%s,%s,%s,%s,now()) RETURNING id""",
        (
            "usr_" + uuid4().hex[:24],
            "phase123_" + uuid4().hex[:16],
            hash_password("phase123-integration-password"),
            "+989121234567",
        ),
    ).fetchone()["id"]
    identity_db.execute(
        """INSERT INTO user_roles(user_id,role_id)
           SELECT %s,id FROM roles WHERE code='user'""",
        (user_id,),
    )
    session_token = "phase123-session-" + uuid4().hex
    identity_db.execute(
        """INSERT INTO sessions(user_id,token_hash,expires_at)
           VALUES (%s,%s,now()+interval '1 day')""",
        (user_id, hash_session_token(session_token)),
    )
    identity_db.commit()

    introspection = identity_client.post(
        "/internal/introspect",
        headers={"X-Identity-Service-Token": IDENTITY_SERVICE_TOKEN},
        json={"token": session_token},
    )
    assert introspection.status_code == 200, introspection.text
    assert introspection.json()["principal"]["user"]["id"] == str(user_id)

    # Commerce validates the real Identity service contract before projecting
    # only the minimal, same-UUID user record into its own database.
    wallet = commerce_client.get(
        "/wallet", headers={"Authorization": f"Bearer {session_token}"}
    )
    assert wallet.status_code == 200, wallet.text
    commerce_user = commerce_db.execute(
        "SELECT id,status,phone_verified_at FROM users WHERE id=%s", (user_id,)
    ).fetchone()
    assert commerce_user is not None
    assert commerce_user["status"] == "active"
    assert commerce_user["phone_verified_at"] is not None

    # A subscription is owned by Commerce, not Identity or Market Data.
    plan_id = commerce_db.execute(
        "SELECT id FROM plans WHERE code='premium_monthly'"
    ).fetchone()["id"]
    now = datetime.now(timezone.utc)
    commerce_db.execute(
        """INSERT INTO subscriptions(user_id,plan_id,status,starts_at,ends_at)
           VALUES (%s,%s,'active',%s,%s)""",
        (user_id, plan_id, now - timedelta(days=1), now + timedelta(days=29)),
    )
    commerce_db.commit()
    entitlements = commerce_client.get(
        f"/internal/users/{user_id}/entitlements",
        headers={"X-Commerce-Service-Token": COMMERCE_SERVICE_TOKEN},
    )
    assert entitlements.status_code == 200, entitlements.text
    assert all(entitlements.json()["entitlements"].values())

    # Market Data reads a separate SQLite source and a read-only Sidecar. It
    # does not need an Identity session or Commerce entitlement to serve
    # publicly available market reference data and daily prices.
    source_path, sidecar_path = _create_market_files(tmp_path)
    from market_data_service.api import app as market_app, get_repository
    from market_data_service.repository import MarketDataRepository

    repository = MarketDataRepository(str(source_path), str(sidecar_path))
    market_app.dependency_overrides[get_repository] = lambda: repository
    try:
        with TestClient(market_app) as market_client:
            instrument = market_client.get("/api/v1/market/instruments/آزمایشی")
            prices = market_client.get("/api/v1/stocks/آزمایشی/prices")
            assert instrument.status_code == 200, instrument.text
            assert instrument.json()["instrument"]["cisin"] == "IR-MARKET-001"
            assert prices.status_code == 200, prices.text
            assert prices.json()["prices"][0]["close"] == 105
    finally:
        market_app.dependency_overrides.pop(get_repository, None)

    assert commerce_db.execute(
        "SELECT COUNT(*) AS n FROM subscriptions WHERE user_id=%s", (user_id,)
    ).fetchone()["n"] == 1
    assert identity_db.execute(
        "SELECT COUNT(*) AS n FROM users WHERE id=%s", (user_id,)
    ).fetchone()["n"] == 1
