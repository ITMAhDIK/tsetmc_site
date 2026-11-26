"""PostgreSQL/API integration coverage for the Phase 8 superadmin console."""
import os
import time
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row
from fastapi.testclient import TestClient

DSN=os.getenv("SAAS_TEST_DATABASE_URL")
pytestmark=pytest.mark.skipif(not DSN,reason="SAAS_TEST_DATABASE_URL is not configured")


@pytest.fixture(scope="session")
def phase8_schema():
    root=os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    schema=f"phase8_{uuid4().hex}"
    with psycopg.connect(DSN,autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        with psycopg.connect(DSN,options=f"-c search_path={schema},public",row_factory=dict_row) as conn:
            for name in ("001_initial.sql","002_payment_lifecycle.sql",
                         "003_wallet_referrals.sql","004_support_indexes.sql",
                         "005_product_analytics.sql","006_superadmin_console.sql",
                         "007_immutable_admin_audit.sql","008_analytics_path_sequences.sql",
                         "009_analytics_event_order.sql","010_sms_outbox_worker.sql",
                         "011_privileged_mfa_and_legal_acceptance.sql"):
                with open(os.path.join(root,"saas_core","schema",name),encoding="utf-8") as stream:
                    conn.execute(stream.read())
        yield schema
    finally:
        with psycopg.connect(DSN,autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def connect(schema):
    return psycopg.connect(DSN,options=f"-c search_path={schema},public",row_factory=dict_row)


def make_user(conn, label):
    user_id=uuid4()
    conn.execute(
        """INSERT INTO users(id,public_id,username,status,password_hash)
           VALUES (%s,%s,%s,'active','test-hash')""",
        (user_id,user_id.hex[:32],f"{label}_{user_id.hex[:12]}"),
    )
    return user_id


def grant_role(conn,user_id,role_code):
    role_id=conn.execute("SELECT id FROM roles WHERE code=%s",(role_code,)).fetchone()["id"]
    conn.execute("INSERT INTO user_roles(user_id,role_id) VALUES (%s,%s)",(user_id,role_id))


def auth_headers(conn,user_id):
    from saas_core.auth.repository import create_session
    token,_=create_session(conn,user_id)
    return {"Authorization":f"Bearer {token}"}


@pytest.fixture
def admin_context(phase8_schema, monkeypatch):
    from saas_core.auth.api import app,get_conn
    from saas_core.auth.mfa import totp_code
    monkeypatch.setenv("SAAS_ADMIN_MFA_FERNET_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    conn=connect(phase8_schema)
    root=make_user(conn,"root")
    admin=make_user(conn,"admin")
    customer=make_user(conn,"customer")
    grant_role(conn,root,"superadmin")
    grant_role(conn,admin,"admin")
    root_headers=auth_headers(conn,root)
    admin_headers=auth_headers(conn,admin)
    customer_headers=auth_headers(conn,customer)
    conn.commit()

    def database_connection():
        yield conn

    app.dependency_overrides[get_conn]=database_connection
    try:
        with TestClient(app) as client:
            setup=client.post("/auth/mfa/setup",headers=root_headers)
            assert setup.status_code==200,setup.text
            confirmed=client.post("/auth/mfa/confirm",headers=root_headers,
                json={"code":totp_code(setup.json()["secret"])})
            assert confirmed.status_code==200,confirmed.text
            yield {"conn":conn,"client":client,"root":root,"admin":admin,"customer":customer,
                   "root_headers":root_headers,"admin_headers":admin_headers,
                   "customer_headers":customer_headers}
    finally:
        app.dependency_overrides.pop(get_conn,None)
        conn.close()


def test_superadmin_gate_and_account_status_lifecycle(admin_context):
    c=admin_context
    client,conn=c["client"],c["conn"]
    assert client.get("/admin/dashboard").status_code==401
    assert client.get("/admin/dashboard",headers=c["admin_headers"]).status_code==403
    assert client.get("/admin/dashboard",headers=c["root_headers"]).status_code==200

    # A non-superadmin cannot remove the final active superadmin, even if an
    # operator accidentally grants the two relevant capabilities.
    admin_role=conn.execute("SELECT id FROM roles WHERE code='admin'").fetchone()["id"]
    elevated=conn.execute(
        "SELECT id FROM permissions WHERE code IN ('superadmin.console.access','users.manage')"
    ).fetchall()
    for permission in elevated:
        conn.execute("INSERT INTO role_permissions(role_id,permission_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                     (admin_role,permission["id"]))
    conn.commit()
    last_root=client.patch(f"/admin/users/{c['root']}/status",headers=c["admin_headers"],
        json={"status":"suspended","reason":"last superadmin protection"})
    assert last_root.status_code==409
    assert conn.execute("SELECT status FROM users WHERE id=%s",(c["root"],)).fetchone()["status"]=="active"
    for permission in elevated:
        conn.execute("DELETE FROM role_permissions WHERE role_id=%s AND permission_id=%s",
                     (admin_role,permission["id"]))
    conn.commit()

    suspended=client.patch(f"/admin/users/{c['customer']}/status",headers=c["root_headers"],
        json={"status":"suspended","reason":"policy violation"})
    assert suspended.status_code==200
    assert client.get("/auth/me",headers=c["customer_headers"]).status_code==401

    resumed=client.patch(f"/admin/users/{c['customer']}/status",headers=c["root_headers"],
        json={"status":"active","reason":"review completed"})
    assert resumed.status_code==200
    assert resumed.json()["user"]["status"]=="active"

    self_change=client.patch(f"/admin/users/{c['root']}/status",headers=c["root_headers"],
        json={"status":"suspended","reason":"self test"})
    assert self_change.status_code==409

    deleted=make_user(conn,"deleted")
    pending=make_user(conn,"pending")
    conn.execute("UPDATE users SET status='deleted' WHERE id=%s",(deleted,))
    conn.execute("UPDATE users SET status='pending' WHERE id=%s",(pending,))
    conn.commit()
    for target in (deleted,pending):
        response=client.patch(f"/admin/users/{target}/status",headers=c["root_headers"],
            json={"status":"active","reason":"reactivation attempt"})
        assert response.status_code==409
    assert conn.execute("SELECT status FROM users WHERE id=%s",(deleted,)).fetchone()["status"]=="deleted"
    assert conn.execute("SELECT status FROM users WHERE id=%s",(pending,)).fetchone()["status"]=="pending"


def test_role_and_plan_controls_are_audited_and_do_not_rewrite_existing_orders(admin_context):
    c=admin_context
    client,conn=c["client"],c["conn"]
    target=c["customer"]
    grant=client.put(f"/admin/users/{target}/roles/support",headers=c["root_headers"],json={"grant":True})
    assert grant.status_code==200
    assert conn.execute("""SELECT count(*) AS n FROM user_roles ur JOIN roles r ON r.id=ur.role_id
                           WHERE ur.user_id=%s AND r.code='support'""",(target,)).fetchone()["n"]==1

    self_role=client.put(f"/admin/users/{c['root']}/roles/admin",headers=c["root_headers"],json={"grant":True})
    assert self_role.status_code==409
    protected=client.put(f"/admin/users/{target}/roles/superadmin",headers=c["root_headers"],json={"grant":True})
    assert protected.status_code==409

    plan=conn.execute("SELECT id,price_toman FROM plans WHERE code='premium_monthly'").fetchone()
    order=conn.execute(
        """INSERT INTO payment_orders(user_id,plan_id,amount_toman,status,idempotency_key,phone_e164)
           VALUES (%s,%s,%s,'pending',%s,'+989121234501') RETURNING id""",
        (target,plan["id"],plan["price_toman"],f"phase8-order-{uuid4()}"),
    ).fetchone()["id"]
    update=client.patch(f"/admin/plans/{plan['id']}",headers=c["root_headers"],
        json={"title":"Premium monthly revised","price_toman":990000})
    assert update.status_code==200
    assert update.json()["plan"]["price_toman"]==990000
    assert conn.execute("SELECT amount_toman FROM payment_orders WHERE id=%s",(order,)).fetchone()["amount_toman"]==plan["price_toman"]
    assert conn.execute("""SELECT count(*) AS n FROM admin_audit_logs
                           WHERE action='superadmin.plan.update' AND target_id=%s""",
                        (str(plan["id"]),)).fetchone()["n"]==1

    subscription=conn.execute(
        """INSERT INTO subscriptions(user_id,plan_id,status,starts_at,ends_at,payment_order_id)
           VALUES (%s,%s,'active',now(),now()+interval '30 days',%s) RETURNING id""",
        (target,plan["id"],order),
    ).fetchone()["id"]
    cancelled=client.post(f"/admin/subscriptions/{subscription}/cancel",headers=c["root_headers"],
        json={"reason":"customer requested cancellation"})
    assert cancelled.status_code==200
    assert conn.execute("SELECT status FROM subscriptions WHERE id=%s",(subscription,)).fetchone()["status"]=="cancelled"
    assert conn.execute("""SELECT count(*) AS n FROM admin_audit_logs
                           WHERE action='superadmin.subscription.cancel' AND target_id=%s""",
                        (str(subscription),)).fetchone()["n"]==1


def test_reconciliation_notes_are_idempotent_append_only_and_do_not_change_payment_state(admin_context):
    c=admin_context
    client,conn=c["client"],c["conn"]
    plan=conn.execute("SELECT id,price_toman FROM plans WHERE code='premium_monthly'").fetchone()
    order=conn.execute(
        """INSERT INTO payment_orders(user_id,plan_id,amount_toman,status,idempotency_key,phone_e164)
           VALUES (%s,%s,%s,'pending',%s,'+989121234502') RETURNING id""",
        (c["customer"],plan["id"],plan["price_toman"],f"reconcile-order-{uuid4()}"),
    ).fetchone()["id"]
    key=f"phase8-reconcile-{uuid4()}"
    url=f"/admin/payments/orders/{order}/reconciliation-notes"
    headers={**c["root_headers"],"Idempotency-Key":key}
    payload={"note":"Provider reference is under review"}
    first=client.post(url,headers=headers,json=payload)
    replay=client.post(url,headers=headers,json=payload)
    assert first.status_code==201 and replay.status_code==201
    assert first.json()["note"]["id"]==replay.json()["note"]["id"]
    conflict=client.post(url,headers=headers,json={"note":"Different review result"})
    assert conflict.status_code==409
    assert conn.execute("SELECT status FROM payment_orders WHERE id=%s",(order,)).fetchone()["status"]=="pending"
    assert conn.execute("SELECT count(*) AS n FROM payment_reconciliation_notes WHERE idempotency_key=%s",
                        (key,)).fetchone()["n"]==1
    conn.commit()
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("UPDATE payment_reconciliation_notes SET note='tampered' WHERE idempotency_key=%s",(key,))
    conn.rollback()
    assert conn.execute("SELECT note FROM payment_reconciliation_notes WHERE idempotency_key=%s",
                        (key,)).fetchone()["note"]==payload["note"]
    assert conn.execute("""SELECT count(*) AS n FROM admin_audit_logs
                           WHERE action='superadmin.payment.reconcile_note' AND target_id=%s""",
                        (str(order),)).fetchone()["n"]==1
    history=client.get(url,headers=c["root_headers"])
    assert history.status_code==200 and len(history.json()["notes"])==1


def test_wallet_adjustment_is_exactly_idempotent_and_rejects_invalid_money(admin_context):
    c=admin_context
    client,conn=c["client"],c["conn"]
    url=f"/admin/users/{c['customer']}/wallet/adjustments"
    key=f"phase8-wallet-{uuid4()}"
    headers={**c["root_headers"],"Idempotency-Key":key}
    payload={"bucket":"gift","amount_toman":50000,"reason":"approved goodwill credit"}
    first=client.post(url,headers=headers,json=payload)
    replay=client.post(url,headers=headers,json=payload)
    assert first.status_code==201 and replay.status_code==201
    assert first.json()["entry"]["id"]==replay.json()["entry"]["id"]
    assert conn.execute("SELECT count(*) AS n FROM wallet_ledger_entries WHERE idempotency_key=%s",
                        (key,)).fetchone()["n"]==1
    assert conn.execute("""SELECT count(*) AS n FROM admin_audit_logs
                           WHERE action='superadmin.wallet.adjust' AND target_id=%s""",
                        (str(c["customer"]),)).fetchone()["n"]==1
    conn.commit()
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("UPDATE wallet_ledger_entries SET description='tampered' WHERE idempotency_key=%s",(key,))
    conn.rollback()
    assert conn.execute("SELECT description FROM wallet_ledger_entries WHERE idempotency_key=%s",
                        (key,)).fetchone()["description"]==payload["reason"]

    changed=client.post(url,headers=headers,json={**payload,"reason":"different justification"})
    assert changed.status_code==409
    for amount in (True,1.5,"50000",9223372036854775808):
        invalid=client.post(url,headers={**c["root_headers"],"Idempotency-Key":f"invalid-{uuid4()}"},
            json={**payload,"amount_toman":amount})
        assert invalid.status_code==422
    debit=client.post(url,headers={**c["root_headers"],"Idempotency-Key":f"debit-{uuid4()}"},
        json={"bucket":"cash","amount_toman":-1,"reason":"cannot overdraw"})
    assert debit.status_code==409
    wallet=client.get(f"/admin/users/{c['customer']}/wallet",headers=c["root_headers"])
    assert wallet.status_code==200
    assert wallet.json()["balances"]["gift_toman"]==50000
    assert wallet.json()["balances"]["cash_toman"]==0


def test_withdrawal_review_referrals_and_audit_are_readable_without_payout_endpoint(admin_context):
    c=admin_context
    client,conn=c["client"],c["conn"]
    withdrawal=conn.execute(
        """INSERT INTO withdrawal_requests(user_id,amount_toman,status,bank_account_token)
           VALUES (%s,25000,'requested','vault-reference-do-not-expose') RETURNING id""",
        (c["customer"],),
    ).fetchone()["id"]
    inviter=make_user(conn,"inviter")
    referred=make_user(conn,"referred")
    conn.execute("INSERT INTO referrals(inviter_user_id,referred_user_id,status) VALUES (%s,%s,'pending')",
                 (inviter,referred))
    conn.commit()

    queue=client.get("/admin/withdrawals",headers=c["root_headers"])
    assert queue.status_code==200
    assert "vault-reference-do-not-expose" not in queue.text
    review=client.post(f"/admin/withdrawals/{withdrawal}/review",headers=c["root_headers"],
        json={"decision":"approved","note":"bank details reviewed"})
    assert review.status_code==200
    duplicate=client.post(f"/admin/withdrawals/{withdrawal}/review",headers=c["root_headers"],
        json={"decision":"rejected","note":"second decision"})
    assert duplicate.status_code==409
    assert conn.execute("SELECT status FROM withdrawal_requests WHERE id=%s",(withdrawal,)).fetchone()["status"]=="approved"

    referrals=client.get("/admin/referrals",headers=c["root_headers"])
    assert referrals.status_code==200 and referrals.json()["referrals"]
    audit=client.get("/admin/audit-logs?action=superadmin.withdrawal.review",headers=c["root_headers"])
    assert audit.status_code==200 and audit.json()["logs"]
    paths=set(client.get("/openapi.json").json()["paths"])
    assert f"/admin/withdrawals/{{withdrawal_id}}/pay" not in paths
