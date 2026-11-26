
"""Cross-phase integration regressions for authentication, account and purchases."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from saas_core.auth.service import AuthenticationError, hash_session_token
from saas_core.auth.repository import (
    authenticate_session, consume_phone_otp, request_phone_otp,
)
from saas_core.auth.service import hash_otp
from saas_core.payments.repository import create_subscription_order, create_payment_attempt, attach_provider_checkout
from saas_core.payments.domain import VerifiedPayment
from saas_core.payments.settlement import settle_provider_callback
from saas_core.account.service import subscription_history

DSN=os.getenv("SAAS_TEST_DATABASE_URL")
pytestmark=pytest.mark.skipif(not DSN,reason="SAAS_TEST_DATABASE_URL is not configured")


@pytest.fixture(scope="session",autouse=True)
def migrated_cross_phase_database():
    root=os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    schema=os.path.join(root,"saas_core","schema")
    with psycopg.connect(DSN,autocommit=True,row_factory=dict_row) as conn:
        for name in ("001_initial.sql","002_payment_lifecycle.sql","003_wallet_referrals.sql",
                     "004_support.sql","005_analytics.sql","006_order_terms_snapshot.sql","005_analytics.sql","006_order_terms_snapshot.sql"):
            with open(os.path.join(schema,name),encoding="utf-8") as stream:
                conn.execute(stream.read())
    yield


def connect():
    return psycopg.connect(DSN,row_factory=dict_row)


def _configure_otp(monkeypatch):
    key=Fernet.generate_key()
    monkeypatch.setenv("SAAS_OTP_HMAC_SECRET","test-otp-hmac-secret-with-at-least-32-bytes")
    monkeypatch.setenv("SAAS_OUTBOX_FERNET_KEY",key.decode("ascii"))
    return Fernet(key)


def _latest_otp(conn,cipher,phone,purpose="login"):
    challenge=conn.execute(
        """SELECT otp_hash FROM phone_verifications
           WHERE phone_e164=%s AND purpose=%s
           ORDER BY created_at DESC,id DESC LIMIT 1""",(phone,purpose)
    ).fetchone()
    if not challenge:
        raise AssertionError("OTP challenge not found")
    rows=conn.execute(
        """SELECT destination_token,payload->>'encrypted_payload' AS encrypted_payload
           FROM notification_outbox
           WHERE template_code='phone_otp' AND idempotency_key LIKE %s
           ORDER BY created_at DESC,id DESC""",(f"otp:{phone}:{purpose}:%",)
    ).fetchall()
    for row in rows:
        if cipher.decrypt(row["destination_token"].encode()).decode()!=phone:
            continue
        payload=json.loads(cipher.decrypt(row["encrypted_payload"].encode()).decode())
        otp=payload["otp"]
        if hash_otp(phone,purpose,otp)==challenge["otp_hash"]:
            return otp
    raise AssertionError("encrypted OTP for the latest challenge not found")


def _grant_role(conn,user_id,role):
    role_id=conn.execute("SELECT id FROM roles WHERE code=%s",(role,)).fetchone()["id"]
    conn.execute("INSERT INTO user_roles(user_id,role_id) VALUES (%s,%s)",(user_id,role_id))


def test_password_account_can_link_verified_phone_then_create_subscription(migrated_cross_phase_database,monkeypatch):
    cipher=_configure_otp(monkeypatch)
    from saas_core.auth.api import app,get_conn
    import saas_core.auth.api as auth_api
    phone="+989121234567"

    with connect() as conn:
        conn.commit()
        def database_connection():
            yield conn
        app.dependency_overrides[get_conn]=database_connection
        monkeypatch.setattr(auth_api,"COOKIE_SECURE",False)
        try:
            with TestClient(app) as client:
                created=client.post("/auth/register",json={
                    "username":f"phase_link_{uuid4().hex[:10]}",
                    "password":"a-long-secure-test-password",
                    "display_name":"Cross phase test",
                })
                assert created.status_code==200
                assert client.get("/auth/me").status_code==200
                requested=client.post("/auth/phone/link/request",json={"phone":phone})
                assert requested.status_code==200
                otp=_latest_otp(conn,cipher,phone,"link_identity")
                verified=client.post("/auth/phone/link/verify",json={"phone":phone,"otp":otp})
                assert verified.status_code==200
                assert verified.json()["phone"]["phone_verified"] is True
                account=client.get("/auth/me")
                assert account.status_code==200
                uid=account.json()["user"]["id"]
                user_uuid=UUID(uid)
                stored=conn.execute("SELECT phone_e164,phone_verified_at FROM users WHERE id=%s",(user_uuid,)).fetchone()
                assert stored["phone_e164"]==phone and stored["phone_verified_at"] is not None
                identity=conn.execute(
                    "SELECT user_id FROM auth_identities WHERE provider='phone_otp' AND provider_subject=%s",
                    (phone,),
                ).fetchone()
                assert str(identity["user_id"])==uid
                order=create_subscription_order(
                    conn,user_id=user_uuid,plan_code="premium_monthly",
                    idempotency_key=f"phase-link-order-{uuid4()}",
                )
                assert order["amount_toman"]==899000
        finally:
            app.dependency_overrides.pop(get_conn,None)


def test_linked_phone_cannot_be_claimed_by_a_second_password_account(migrated_cross_phase_database,monkeypatch):
    cipher=_configure_otp(monkeypatch)
    from saas_core.auth.api import app,get_conn
    import saas_core.auth.api as auth_api
    phone="+989121234568"
    with connect() as conn:
        conn.commit()
        def database_connection():
            yield conn
        app.dependency_overrides[get_conn]=database_connection
        monkeypatch.setattr(auth_api,"COOKIE_SECURE",False)
        try:
            with TestClient(app) as first:
                r=first.post("/auth/register",json={"username":f"owner_{uuid4().hex[:8]}","password":"a-long-secure-test-password"})
                assert r.status_code==200
                assert first.post("/auth/phone/link/request",json={"phone":phone}).status_code==200
                otp=_latest_otp(conn,cipher,phone)
                assert first.post("/auth/phone/link/verify",json={"phone":phone,"otp":otp}).status_code==200
            with TestClient(app) as second:
                r=second.post("/auth/register",json={"username":f"claim_{uuid4().hex[:8]}","password":"a-long-secure-test-password"})
                assert r.status_code==200
                assert second.post("/auth/phone/link/request",json={"phone":phone}).status_code==200
                second_otp=_latest_otp(conn,cipher,phone,"link_identity")
                rejected=second.post("/auth/phone/link/verify",json={"phone":phone,"otp":second_otp})
                assert rejected.status_code==401
                assert conn.execute("SELECT count(*) AS n FROM users WHERE phone_e164=%s",(phone,)).fetchone()["n"]==1
        finally:
            app.dependency_overrides.pop(get_conn,None)


def test_phone_otp_rate_limit_is_atomic_under_concurrent_requests(migrated_cross_phase_database,monkeypatch):
    _configure_otp(monkeypatch)
    phone="+989121234569"
    def request_one(_):
        try:
            with connect() as conn:
                request_phone_otp(conn,phone,"signup")
            return "accepted"
        except ValueError:
            return "limited"
    with ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(request_one,range(8)))
    assert results.count("accepted")==3
    assert results.count("limited")==5
    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM phone_verifications WHERE phone_e164=%s AND created_at>now()-interval '15 minutes'",
            (phone,),
        ).fetchone()["n"]==3


def test_otp_failed_attempt_limit_replay_protection_and_hashed_session(migrated_cross_phase_database,monkeypatch):
    cipher=_configure_otp(monkeypatch)
    phone="+989121234570"
    with connect() as conn:
        user_id=uuid4()
        conn.execute(
            """INSERT INTO users(id,public_id,username,status,phone_e164,phone_verified_at)
               VALUES (%s,%s,%s,'active',%s,now())""",
            (user_id,str(user_id)[:32],f"otp-{user_id.hex[:12]}",phone),
        )
        request_phone_otp(conn,phone,"login")
        for _ in range(5):
            with pytest.raises(AuthenticationError):
                consume_phone_otp(conn,phone,"000000","login")
        assert conn.execute(
            "SELECT attempts FROM phone_verifications WHERE phone_e164=%s AND purpose='login' ORDER BY created_at DESC LIMIT 1",
            (phone,),
        ).fetchone()["attempts"]==5
        with pytest.raises(AuthenticationError):
            consume_phone_otp(conn,phone,_latest_otp(conn,cipher,phone),"login")
        request_phone_otp(conn,phone,"login")
        otp=_latest_otp(conn,cipher,phone)
        token,_=consume_phone_otp(conn,phone,otp,"login")
        with pytest.raises(AuthenticationError):
            consume_phone_otp(conn,phone,otp,"login")
        row=conn.execute("SELECT token_hash FROM sessions WHERE user_id=%s ORDER BY created_at DESC LIMIT 1",(user_id,)).fetchone()
        assert row["token_hash"]==hash_session_token(token)
        assert row["token_hash"]!=token
        assert authenticate_session(conn,token)["id"]==user_id


def test_phone_normalization_rejects_unicode_digits(migrated_cross_phase_database):
    from saas_core.auth.service import normalize_phone
    with pytest.raises(ValueError):
        normalize_phone("+۹۸۹۱۲۳۴۵۶۷۸")


def test_subscription_history_and_settlement_use_terms_captured_when_order_was_created(migrated_cross_phase_database):
    with connect() as conn:
        user_id=uuid4()
        conn.execute(
            """INSERT INTO users(id,public_id,username,status,phone_e164,phone_verified_at)
               VALUES (%s,%s,%s,'active','+989121234571',now())""",
            (user_id,str(user_id)[:32],f"terms-{user_id.hex[:12]}"),
        )
        plan_code=f"terms_{uuid4().hex[:10]}"
        conn.execute("INSERT INTO plans(code,title,duration_days,price_toman,active) VALUES (%s,'Snapshot test plan',30,899000,TRUE)",(plan_code,))
        order=create_subscription_order(conn,user_id=user_id,plan_code=plan_code,
                                        idempotency_key=f"terms-order-{uuid4()}")
        assert order["amount_toman"]==899000 and order["duration_days"]==30
        conn.execute("UPDATE plans SET price_toman=999000,duration_days=7 WHERE code=%s",(plan_code,))
        attempt=create_payment_attempt(conn,order_id=order["id"],provider="snappay")
        provider_ref=f"terms-provider-{uuid4()}"
        attach_provider_checkout(conn,attempt_id=attempt["id"],provider_order_id=provider_ref,
            redirect_url="https://sandbox.invalid/checkout",request_ref="terms-test")
        verified=VerifiedPayment(provider_event_id=f"terms-event-{uuid4()}",
            provider_order_id=provider_ref,amount_toman=899000,success=True,reference="terms-reference")
        settle_provider_callback(conn,order_id=order["id"],attempt_id=attempt["id"],provider="snappay",
            verified=verified,payload_sha256="9"*64)
        subscription=conn.execute(
            """SELECT EXTRACT(EPOCH FROM (ends_at-starts_at))/86400 AS days
               FROM subscriptions WHERE payment_order_id=%s""",(order["id"],)
        ).fetchone()
        assert float(subscription["days"])==30.0
        history=subscription_history(conn,user_id)
        assert history[0]["price_toman"]==899000
        assert history[0]["duration_days"]==30


def test_account_api_profile_preferences_and_session_ownership(migrated_cross_phase_database):
    from saas_core.auth.api import app,get_conn
    from saas_core.auth.repository import create_session
    from fastapi.testclient import TestClient
    with connect() as conn:
        user_id=uuid4();other_id=uuid4()
        conn.execute(
            """INSERT INTO users(id,public_id,username,status,display_name)
               VALUES (%s,%s,%s,'active','Original Name'),(%s,%s,%s,'active','Other User')""",
            (user_id,str(user_id)[:32],f"account_{user_id.hex[:10]}",other_id,str(other_id)[:32],f"other_{other_id.hex[:10]}"),
        )
        conn.execute("INSERT INTO user_preferences(user_id) VALUES (%s),(%s)",(user_id,other_id))
        current_token,_=create_session(conn,user_id)
        secondary_token,_=create_session(conn,user_id)
        third_token,_=create_session(conn,user_id)
        other_token,_=create_session(conn,other_id)
        conn.commit()
        foreign_session=conn.execute("SELECT id FROM sessions WHERE token_hash=%s",(hash_session_token(other_token),)).fetchone()["id"]
        def database_connection():
            yield conn
        app.dependency_overrides[get_conn]=database_connection
        try:
            with TestClient(app) as client:
                headers={"Authorization":f"Bearer {current_token}"}
                profile=client.get("/account/profile",headers=headers)
                assert profile.status_code==200 and profile.json()["profile"]["display_name"]=="Original Name"
                updated=client.patch("/account/profile",headers=headers,json={"display_name":"Updated Name"})
                assert updated.status_code==200
                assert client.get("/account/profile",headers=headers).json()["profile"]["display_name"]=="Updated Name"
                assert client.patch("/account/profile",headers=headers,json={"display_name":"Bad","status":"deleted"}).status_code==422
                preferences=client.patch("/account/preferences",headers=headers,json={"theme":"dark","currency":"rial"})
                assert preferences.status_code==200
                assert preferences.json()["preferences"]["theme"]=="dark"
                assert client.patch("/account/preferences",headers=headers,json={"is_admin":True}).status_code==422
                sessions=client.get("/account/sessions",headers=headers)
                assert sessions.status_code==200 and len(sessions.json()["sessions"])==3
                own_ids={row["id"] for row in sessions.json()["sessions"]}
                current_id=next(row["id"] for row in sessions.json()["sessions"] if row["is_current"])
                assert client.delete(f"/account/sessions/{current_id}",headers=headers).status_code==409
                assert client.delete(f"/account/sessions/{foreign_session}",headers=headers).status_code==404
                secondary_id=str(conn.execute("SELECT id FROM sessions WHERE token_hash=%s",(hash_session_token(secondary_token),)).fetchone()["id"])
                assert secondary_id in own_ids
                assert client.delete(f"/account/sessions/{secondary_id}",headers=headers).status_code==200
                revoked=client.post("/account/sessions/revoke-others",headers=headers)
                assert revoked.status_code==200 and revoked.json()["revoked_count"]==1
                assert authenticate_session(conn,third_token) is None
                assert authenticate_session(conn,other_token)["id"]==other_id
                assert authenticate_session(conn,current_token)["id"]==user_id
        finally:
            app.dependency_overrides.pop(get_conn,None)
