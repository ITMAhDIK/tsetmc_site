from __future__ import annotations
import os
from uuid import uuid4
from datetime import datetime, timezone, timedelta
import psycopg
import pytest

DSN=os.getenv("CRM_TEST_DATABASE_URL","").strip()
EXPECTED=os.getenv("CRM_EXPECTED_DATABASE_NAME","").strip()
pytestmark=pytest.mark.skipif(not DSN or not EXPECTED,reason="requires explicitly named disposable CRM PostgreSQL database")

def test_crm_postgres_schema_constraints_audit_and_immutable_interactions():
    with psycopg.connect(DSN) as conn:
        actual=conn.execute("SELECT current_database()").fetchone()[0]
        assert actual==EXPECTED
        customer=uuid4(); actor=uuid4()
        conn.execute("""INSERT INTO crm_customers(user_id,username,display_name,account_status,created_at)
            VALUES(%s,%s,%s,'active',now())""",(customer,"test_"+customer.hex[:12],"CRM test"))
        interaction=conn.execute("""INSERT INTO crm_interactions(customer_id,actor_user_id,channel,subject,summary)
            VALUES(%s,%s,'note','Integration test','Append-only interaction') RETURNING id""",(customer,actor)).fetchone()[0]
        with pytest.raises(psycopg.errors.RaiseException):
            with conn.transaction():
                conn.execute("UPDATE crm_interactions SET summary='tampered' WHERE id=%s",(interaction,))
        conn.execute("""INSERT INTO crm_audit_log(actor_user_id,action,target_type,target_id,metadata)
            VALUES(%s,'crm.test','customer',%s,'{"source":"integration"}'::jsonb)""",(actor,str(customer)))
        followup=conn.execute("""INSERT INTO crm_followups(customer_id,assigned_to,created_by,title,due_at)
            VALUES(%s,%s,%s,'Integration follow-up',%s) RETURNING id""",(customer,actor,actor,datetime.now(timezone.utc)+timedelta(days=1))).fetchone()[0]
        assert conn.execute("SELECT status FROM crm_followups WHERE id=%s",(followup,)).fetchone()[0]=="open"
        conn.rollback()

def test_crm_requires_permissions_and_recent_mfa():
    from crm_service.api import _permission
    from fastapi import HTTPException
    recent=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()
    principal={"permissions":["crm.read","crm.manage"],"mfa_enabled":True,"mfa_verified_at":recent}
    _permission(principal,"crm.read")
    with pytest.raises(HTTPException) as denied:
        _permission({"permissions":[],"mfa_enabled":True,"mfa_verified_at":recent},"crm.read")
    assert denied.value.status_code==403
    stale=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
    with pytest.raises(HTTPException) as denied_mfa:
        _permission({"permissions":["crm.read"],"mfa_enabled":True,"mfa_verified_at":stale},"crm.read")
    assert denied_mfa.value.status_code==403
