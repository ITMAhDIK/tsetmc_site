"""PostgreSQL/API integration tests for the Phase 8 administrative console."""
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

DSN=os.getenv("SAAS_TEST_DATABASE_URL")
pytestmark=pytest.mark.skipif(not DSN,reason="SAAS_TEST_DATABASE_URL is not configured")

@pytest.fixture(scope="session",autouse=True)
def migrated_admin_database():
    root=os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    schema=os.path.join(root,"saas_core","schema")
    with psycopg.connect(DSN,autocommit=True,row_factory=dict_row) as conn:
        for name in ("001_initial.sql","002_payment_lifecycle.sql","003_wallet_referrals.sql",
                     "004_support.sql","005_analytics.sql","006_order_terms_snapshot.sql","007_admin_console.sql"):
            with open(os.path.join(schema,name),encoding="utf-8") as stream:
                conn.execute(stream.read())
    yield

def connect():
    return psycopg.connect(DSN,row_factory=dict_row)

def make_user(conn,code="console"):
    uid=uuid4()
    conn.execute("INSERT INTO users(id,public_id,username,status,password_hash) VALUES (%s,%s,%s,'active','never-return-this')",
                 (uid,str(uid)[:32],f"{code}-{uid.hex[:12]}"))
    return uid

def grant(conn,user_id,role):
    rid=conn.execute("SELECT id FROM roles WHERE code=%s",(role,)).fetchone()["id"]
    conn.execute("INSERT INTO user_roles(user_id,role_id) VALUES (%s,%s)",(user_id,rid))

def session(conn,user_id):
    from saas_core.auth.repository import create_session
    token,_=create_session(conn,user_id)
    return {"Authorization":f"Bearer {token}"}

def test_console_permissions_user_management_and_session_revocation(migrated_admin_database):
    from saas_core.auth.api import app,get_conn
    from saas_core.auth.repository import authenticate_session
    with connect() as conn:
        root=make_user(conn,"root"); admin=make_user(conn,"admin")
        target=make_user(conn,"target"); protected=make_user(conn,"protected")
        grant(conn,root,"superadmin");grant(conn,admin,"admin");grant(conn,protected,"superadmin")
        root_headers=session(conn,root);admin_headers=session(conn,admin);target_headers=session(conn,target)
        target_token=target_headers["Authorization"].split(" ",1)[1]
        conn.commit()
        def database_connection():
            yield conn
        app.dependency_overrides[get_conn]=database_connection
        try:
            with TestClient(app) as client:
                assert client.get("/admin/console/overview",headers=root_headers).status_code==200
                ui=client.get("/admin/console/ui",headers=root_headers)
                assert ui.status_code==200 and "Tloomberg" in ui.text and "/admin/console/users" in ui.text
                assert client.get("/admin/console/ui",headers=admin_headers).status_code==403
                assert client.get("/admin/console/overview",headers=admin_headers).status_code==403
                cookie_token=root_headers["Authorization"].split(" ",1)[1]
                client.cookies.set("tloomberg_session",cookie_token,domain="testserver",path="/")
                csrf_blocked=client.patch(f"/admin/console/users/{target}/status",
                    json={"status":"suspended","reason":"missing origin"})
                assert csrf_blocked.status_code==403
                client.cookies.delete("tloomberg_session",domain="testserver",path="/")
                listing=client.get("/admin/console/users?q=target",headers=admin_headers)
                assert listing.status_code==200
                assert any(row["id"]==str(target) for row in listing.json()["users"])
                detail=client.get(f"/admin/console/users/{target}",headers=admin_headers)
                assert detail.status_code==200
                assert "password_hash" not in detail.text and "never-return-this" not in detail.text
                assert client.get("/admin/console/roles",headers=admin_headers).status_code==403
                assert client.get("/admin/console/roles",headers=root_headers).status_code==200

                granted=client.put(f"/admin/console/users/{target}/roles/support",headers=root_headers)
                assert granted.status_code==200 and granted.json()["role"]["changed"] is True
                assert client.delete(f"/admin/console/users/{target}/roles/support",headers=root_headers).status_code==200
                assert client.put(f"/admin/console/users/{target}/roles/superadmin",headers=root_headers).status_code==422
                assert client.put(f"/admin/console/users/{protected}/roles/admin",headers=root_headers).status_code==422

                self_suspend=client.patch(f"/admin/console/users/{root}/status",headers=root_headers,
                    json={"status":"suspended","reason":"self test"})
                assert self_suspend.status_code==422
                protect_suspend=client.patch(f"/admin/console/users/{protected}/status",headers=root_headers,
                    json={"status":"suspended","reason":"protected superadmin"})
                assert protect_suspend.status_code==422

                suspended=client.patch(f"/admin/console/users/{target}/status",headers=root_headers,
                    json={"status":"suspended","reason":"policy violation"})
                assert suspended.status_code==200 and suspended.json()["user"]["status"]=="suspended"
                assert authenticate_session(conn,target_token) is None
                assert client.get("/auth/me",headers=target_headers).status_code==401
                logs=client.get("/admin/console/audit?action=admin.user.status_changed",headers=root_headers)
                assert logs.status_code==200 and logs.json()["logs"]
                assert client.patch(f"/admin/console/users/{target}/status",headers=root_headers,
                    json={"status":"deleted","reason":"not allowed"}).status_code==422
        finally:
            app.dependency_overrides.pop(get_conn,None)


def test_console_financial_support_referral_and_analytics_views_are_permission_scoped(migrated_admin_database):
    from saas_core.auth.api import app,get_conn
    with connect() as conn:
        root=make_user(conn,"rootview"); admin=make_user(conn,"adminview")
        customer=make_user(conn,"customer"); invited=make_user(conn,"invited")
        grant(conn,root,"superadmin");grant(conn,admin,"admin")
        root_headers=session(conn,root);admin_headers=session(conn,admin)
        plan=conn.execute("SELECT id FROM plans WHERE code='premium_monthly'").fetchone()["id"]
        order=conn.execute("""INSERT INTO payment_orders(user_id,plan_id,amount_toman,duration_days,status,
            idempotency_key,phone_e164,expires_at) VALUES (%s,%s,899000,30,'pending',%s,'+989121234501',now()+interval '1 hour')
            RETURNING id""",(customer,plan,f"console-order-{uuid4()}")).fetchone()["id"]
        conn.execute("""INSERT INTO subscriptions(user_id,plan_id,status,starts_at,ends_at,payment_order_id)
            VALUES (%s,%s,'active',now(),now()+interval '30 days',%s)""",(customer,plan,order))
        attempt=conn.execute("""INSERT INTO payment_attempts(order_id,provider,status,amount_toman)
            VALUES (%s,'snappay','pending',899000) RETURNING id""",(order,)).fetchone()["id"]
        case=conn.execute("""INSERT INTO payment_reconciliation_cases(order_id,payment_attempt_id,reason)
            VALUES (%s,%s,'test_case') RETURNING id""",(order,attempt)).fetchone()["id"]
        conn.execute("""INSERT INTO referrals(inviter_user_id,referred_user_id,status)
            VALUES (%s,%s,'pending')""",(customer,invited))
        conn.execute("""INSERT INTO withdrawal_requests(user_id,amount_toman,status,bank_account_token)
            VALUES (%s,100000,'requested','vault-reference:test')""",(customer,))
        conn.execute("""INSERT INTO tickets(user_id,subject,status) VALUES (%s,'Console support test','open')""",(customer,))
        conn.execute("INSERT INTO wallet_accounts(user_id) VALUES (%s)",(customer,))
        conn.execute("""INSERT INTO wallet_ledger_entries(user_id,bucket,entry_type,amount_toman,idempotency_key,description)
            VALUES (%s,'cash','topup',150000,%s,'fixture verified user-funded balance')""",
            (customer,f"console-fixture-topup-{uuid4()}"))
        conn.commit()
        def database_connection():
            yield conn
        app.dependency_overrides[get_conn]=database_connection
        try:
            with TestClient(app) as client:
                assert client.get("/admin/console/subscriptions",headers=admin_headers).status_code==200
                assert client.get("/admin/console/payments",headers=admin_headers).status_code==200
                assert client.get("/admin/console/withdrawals",headers=admin_headers).status_code==200
                assert client.get("/admin/console/support",headers=admin_headers).status_code==200
                assert client.get("/admin/console/audit",headers=admin_headers).status_code==200
                assert client.get(f"/admin/console/wallets/{customer}",headers=admin_headers).status_code==200
                assert client.get("/admin/console/referrals",headers=admin_headers).status_code==403
                assert client.get("/admin/console/referrals",headers=root_headers).status_code==200
                assert client.post(f"/admin/console/payments/reconciliation/{case}/resolve",
                    headers=admin_headers,json={"note":"Verified against provider"}).status_code==200
                assert client.get("/admin/console/payments/reconciliation",headers=admin_headers).status_code==200

                denied=client.post(f"/admin/console/wallets/{customer}/adjust",headers=admin_headers,
                    json={"bucket":"gift","amount_toman":50000,"reason":"test credit","idempotency_key":f"console-adjust-{uuid4()}"})
                assert denied.status_code==403
                key=f"console-adjust-{uuid4()}"
                credit=client.post(f"/admin/console/wallets/{customer}/adjust",headers=root_headers,
                    json={"bucket":"gift","amount_toman":50000,"reason":"approved goodwill","idempotency_key":key})
                assert credit.status_code==200 and credit.json()["entry"]["amount_toman"]==50000
                replay=client.post(f"/admin/console/wallets/{customer}/adjust",headers=root_headers,
                    json={"bucket":"gift","amount_toman":50000,"reason":"approved goodwill","idempotency_key":key})
                assert replay.status_code==200
                assert conn.execute("SELECT count(*) AS n FROM wallet_ledger_entries WHERE idempotency_key=%s",(key,)).fetchone()["n"]==1
                assert client.get(f"/admin/console/wallets/{customer}",headers=root_headers).json()["gift_toman"]==50000
                assert client.post(f"/admin/console/wallets/{customer}/adjust",headers=root_headers,
                    json={"bucket":"gift","amount_toman":-60000,"reason":"too much debit","idempotency_key":f"console-debit-{uuid4()}"}).status_code==422
                assert client.get("/admin/console/overview",headers=root_headers).json()["total_users"]>=4
        finally:
            app.dependency_overrides.pop(get_conn,None)


def test_admin_console_services_enforce_permissions_without_api(migrated_admin_database):
    from saas_core.admin.service import console_overview,list_users,adjust_wallet
    from saas_core.auth.authorization import AuthorizationDenied
    with connect() as conn:
        ordinary=make_user(conn)
        with pytest.raises(AuthorizationDenied):
            console_overview(conn,actor_id=ordinary)
        with pytest.raises(AuthorizationDenied):
            list_users(conn,actor_id=ordinary)
        with pytest.raises(AuthorizationDenied):
            adjust_wallet(conn,actor_id=ordinary,user_id=ordinary,bucket="cash",
                          amount_toman=1000,reason="no",idempotency_key="direct-call-denied")
