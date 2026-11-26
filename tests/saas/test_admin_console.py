from __future__ import annotations
import uuid
import pytest
from saas_core.domain.admin_console import (
    AdminConflict, AdminConsoleError, AdminConsoleService, AdminPermissionDenied
)

ACTOR=uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
TARGET=uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
RESOURCE=uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")

class Cursor:
    def __init__(self, one=(), many=(), rowcount=1):
        self.one=list(one); self.many=list(many); self.rowcount=rowcount; self.calls=[]
    def execute(self, sql, params=None):
        self.calls.append((sql,params))
        if sql.lstrip().upper().startswith("DELETE "): self.rowcount=1
        return self
    def fetchone(self): return self.one.pop(0) if self.one else None
    def fetchall(self): return self.many.pop(0) if self.many else []
    def close(self): pass

class Connection:
    def __init__(self,cursor): self._cursor=cursor; self.commits=0; self.rollbacks=0
    def cursor(self): return self._cursor
    def commit(self): self.commits+=1
    def rollback(self): self.rollbacks+=1
    def close(self): pass

DEFAULT_PERMS={
 "admin.console.read","users.read","users.manage","roles.manage","subscriptions.manage",
 "payments.read","payments.reconcile","wallet.read","wallet.adjust","withdrawals.review",
 "referrals.read","tickets.manage","faq.manage","audit.read"
}
def service(cursor, permissions=None, calls=None):
    conn=Connection(cursor)
    def factory():
        if calls is not None: calls.append(True)
        return conn
    allowed=set(permissions or DEFAULT_PERMS)
    return AdminConsoleService(connection_factory=factory,
        permission_checker=lambda actor,permission: permission in allowed),conn

def test_overview_counts_and_server_permission():
    c=Cursor(one=[{"users_total":100,"users_active":90,"subscriptions_active":20,
        "payments_pending":3,"withdrawals_pending":2,"tickets_open":6,"referrals_pending":4}])
    svc,conn=service(c)
    data=svc.overview(actor_user_id=ACTOR)
    assert data["users_total"]==100 and data["withdrawals_pending"]==2
    assert "FROM payment_orders" in c.calls[0][0] and conn.commits==1

def test_forbidden_permission_fails_before_database_connection():
    calls=[]; c=Cursor()
    svc,_=service(c,permissions={"users.read"},calls=calls)
    with pytest.raises(AdminPermissionDenied): svc.overview(actor_user_id=ACTOR)
    assert calls==[] and c.calls==[]

def test_user_list_is_parameterized_and_omits_password_hash():
    c=Cursor(many=[[
      {"id":TARGET,"public_id":"TL-1","username":"u","display_name":"U","account_type":"standard",
       "status":"active","phone_e164":None,"email":None,"created_at":"t0","last_login_at":None,
       "roles":["user"],"total_count":1}
    ]])
    svc,_=service(c)
    result=svc.list_users(actor_user_id=ACTOR,query="'; DROP TABLE users",limit=20,offset=10)
    assert result["total"]==1 and "password_hash" not in result["users"][0]
    sql,params=c.calls[0]
    assert "ILIKE %s" in sql and "DROP TABLE" not in sql and params[-2:]==(20,10)

def test_pagination_is_bounded_before_database_access():
    c=Cursor(); svc,_=service(c)
    with pytest.raises(AdminConsoleError): svc.list_users(actor_user_id=ACTOR,limit=99999)
    assert not c.calls

def test_last_active_superadmin_cannot_be_suspended():
    c=Cursor(one=[("active",),(True,),(1,),(0,)])
    svc,conn=service(c)
    with pytest.raises(AdminConflict): svc.set_user_status(
        actor_user_id=ACTOR,target_user_id=TARGET,status="suspended",request_id="req-last")
    assert not any("UPDATE users SET status" in sql for sql,_ in c.calls)
    assert conn.rollbacks==1 and conn.commits==0

def test_role_escalation_requires_a_real_superadmin():
    c=Cursor(one=[(TARGET,),(False,)])
    svc,conn=service(c)
    with pytest.raises(AdminPermissionDenied): svc.assign_role(
        actor_user_id=ACTOR,target_user_id=TARGET,role_code="admin",request_id="req-esc")
    assert not any("INSERT INTO user_roles" in sql for sql,_ in c.calls)
    assert conn.rollbacks==1

def test_payment_review_is_audited_but_does_not_settle_order():
    c=Cursor(one=[("pending",),(RESOURCE,)])
    svc,conn=service(c)
    result=svc.review_payment(actor_user_id=ACTOR,order_id=TARGET,outcome="provider_followup",
       note="Needs provider evidence.",request_id="req-pay")
    sql=[s for s,_ in c.calls]
    assert result["order_status_unchanged"]=="pending"
    assert any("admin_payment_reviews" in s for s in sql)
    assert any("admin_audit_logs" in s for s in sql)
    assert not any("UPDATE payment_orders" in s or "INSERT INTO subscriptions" in s for s in sql)
    assert conn.commits==1

def test_wallet_adjustment_uses_append_only_ledger_and_audit():
    c=Cursor(one=[None,(TARGET,),(TARGET,),(100000,50000),(25000,),(RESOURCE,)])
    svc,conn=service(c)
    result=svc.adjust_wallet(actor_user_id=ACTOR,target_user_id=TARGET,bucket="cash",
       direction="debit",amount_toman=50000,idempotency_key="adj-001",
       reason="Correct duplicate credit",request_id="req-wallet")
    sql=[s for s,_ in c.calls]
    assert result["amount_toman"]==-50000
    assert any("INSERT INTO wallet_ledger_entries" in s for s in sql)
    assert any("admin_audit_logs" in s for s in sql)
    assert not any("UPDATE wallet_ledger_entries" in s for s in sql)
    assert conn.commits==1

def test_wallet_debit_cannot_consume_reserved_withdrawal_cash():
    c=Cursor(one=[None,(TARGET,),(TARGET,),(100000,0),(80000,)])
    svc,conn=service(c)
    with pytest.raises(AdminConflict): svc.adjust_wallet(actor_user_id=ACTOR,target_user_id=TARGET,
       bucket="cash",direction="debit",amount_toman=30000,idempotency_key="adj-002",
       reason="Reserved cash protection",request_id="req-wallet-2")
    assert not any("INSERT INTO wallet_ledger_entries" in s for s,_ in c.calls)
    assert conn.rollbacks==1

def test_paid_withdrawal_requires_reference_and_is_audited():
    c=Cursor(one=[(TARGET,"approved",120000),("paid",),(TARGET,)])
    svc,conn=service(c)
    result=svc.review_withdrawal(actor_user_id=ACTOR,withdrawal_id=RESOURCE,decision="paid",
       payout_reference="BANK-REF-1",request_id="req-paid")
    update=next(params for sql,params in c.calls if "UPDATE withdrawal_requests" in sql)
    assert result["status"]=="paid" and update[3]=="BANK-REF-1"
    assert any("admin_audit_logs" in sql for sql,_ in c.calls) and conn.commits==1

def test_paid_withdrawal_without_reference_is_rejected_pre_db():
    calls=[]; c=Cursor(); svc,_=service(c,calls=calls)
    with pytest.raises(AdminConsoleError):
        svc.review_withdrawal(actor_user_id=ACTOR,withdrawal_id=RESOURCE,decision="paid",request_id="req-no-ref")
    assert calls==[]

def test_internal_ticket_notes_are_private_and_audited():
    c=Cursor(one=[(RESOURCE,),(RESOURCE,)])
    svc,conn=service(c)
    result=svc.add_internal_ticket_note(actor_user_id=ACTOR,ticket_id=TARGET,
       body="Internal provider follow-up.",request_id="req-note")
    insert=next((sql,params) for sql,params in c.calls if "INSERT INTO ticket_messages" in sql)
    assert result["internal"] is True and "TRUE" in insert[0]
    assert insert[1]==(TARGET,ACTOR,"Internal provider follow-up.")
    assert any("admin_audit_logs" in sql for sql,_ in c.calls) and conn.commits==1

def test_faq_is_validated_and_audited():
    c=Cursor(one=[None,{"slug":"help-start","question":"Help","published":True,"sort_order":1}])
    svc,conn=service(c)
    result=svc.upsert_faq(actor_user_id=ACTOR,slug="help-start",question="Help",
       answer="Read our guide.",published=True,sort_order=1,request_id="req-faq")
    assert result["published"] is True
    assert any("INSERT INTO faq_articles" in sql for sql,_ in c.calls)
    assert any("admin_audit_logs" in sql for sql,_ in c.calls) and conn.commits==1

def test_invalid_faq_slug_is_rejected_before_connection():
    calls=[]; c=Cursor(); svc,_=service(c,calls=calls)
    with pytest.raises(AdminConsoleError):
        svc.upsert_faq(actor_user_id=ACTOR,slug="../private",question="Q",answer="A",
            published=True,request_id="req-faq-bad")
    assert calls==[]

def test_audit_log_query_is_permission_gated_and_parameterized():
    c=Cursor(many=[[
      {"id":RESOURCE,"actor_user_id":ACTOR,"action":"users.status.change","target_type":"user",
       "target_id":str(TARGET),"request_id":"req-1","before_state":{"status":"active"},
       "after_state":{"status":"suspended"},"created_at":"t0"}
    ]])
    svc,_=service(c)
    result=svc.audit_logs(actor_user_id=ACTOR,action="users.status.change",target_id=str(TARGET),limit=10,offset=20)
    assert result["entries"][0]["action"]=="users.status.change"
    assert c.calls[0][1]==("users.status.change","users.status.change",str(TARGET),str(TARGET),10,20)

def test_user_detail_redacts_password_hash_and_returns_roles_and_subscriptions():
    c=Cursor(one=[{
      "id":TARGET,"public_id":"TL-1","username":"u","display_name":"U","account_type":"standard",
      "status":"active","phone_e164":None,"phone_verified_at":None,"email":None,
      "email_verified_at":None,"created_at":"t0","updated_at":"t1","last_login_at":None
    }],many=[
      [{"code":"user","title":"User","granted_at":"t0","granted_by":None}],
      [{"id":RESOURCE,"plan_code":"premium_monthly","plan_title":"Monthly","status":"active",
        "starts_at":"t0","ends_at":"t1","created_at":"t0"}]
    ])
    svc,_=service(c)
    data=svc.user_detail(actor_user_id=ACTOR,target_user_id=TARGET)
    assert "password_hash" not in data["user"]
    assert data["roles"][0]["code"]=="user"
    assert data["subscriptions"][0]["plan_code"]=="premium_monthly"
