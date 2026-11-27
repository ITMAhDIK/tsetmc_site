from pathlib import Path
from uuid import uuid4
import os
import pytest
import psycopg
from psycopg.rows import dict_row
from fastapi import HTTPException
DSN=os.getenv("NOTIFICATION_TEST_DATABASE_URL","").strip()
EXPECTED=os.getenv("NOTIFICATION_EXPECTED_DATABASE_NAME","").strip()
pytestmark=pytest.mark.skipif(not DSN or not EXPECTED,reason="requires dedicated disposable Notification PostgreSQL database")

def test_notification_enqueue_is_idempotent_and_creates_inbox_and_queue():
 from notification_service.service import enqueue,claim,deliver_one
 user=uuid4(); key="integration-"+uuid4().hex
 with psycopg.connect(DSN,row_factory=dict_row) as conn:
  assert conn.execute("SELECT current_database() AS n").fetchone()["n"]==EXPECTED
  result=enqueue(conn,[user],"welcome",{"name":"Test"},["in_app","email"],key)
  assert result["duplicate"] is False and len(result["notification_ids"])==1
  repeated=enqueue(conn,[user],"welcome",{"name":"Test"},["in_app","email"],key)
  assert repeated["duplicate"] is True and repeated["notification_ids"]==result["notification_ids"]
  with pytest.raises(HTTPException) as err:
   enqueue(conn,[user],"security_alert",{},["in_app"],key)
  assert err.value.status_code==409
  nid=result["notification_ids"][0]
  assert conn.execute("SELECT count(*) FROM notification_deliveries WHERE notification_id=%s",(nid,)).fetchone()["count"]==2
  jobs=claim(conn,10)
  assert len(jobs)==2
  inbox=conn.execute("SELECT id FROM notifications WHERE id=%s AND in_app_visible=TRUE",(nid,)).fetchone()
  assert inbox is not None
  for job in jobs:
   if job["channel"]=="in_app": deliver_one(conn,job)
  assert conn.execute("SELECT status FROM notification_deliveries WHERE notification_id=%s AND channel='in_app'",(nid,)).fetchone()["status"]=="sent"
  conn.rollback()

def test_user_opt_out_suppresses_channel_delivery():
 from notification_service.service import enqueue
 user=uuid4(); key="integration-"+uuid4().hex
 with psycopg.connect(DSN,row_factory=dict_row) as conn:
  conn.execute("INSERT INTO notification_preferences(user_id,email_enabled,sms_enabled,in_app_enabled) VALUES(%s,FALSE,FALSE,FALSE)",(user,))
  result=enqueue(conn,[user],"welcome",{},["email","sms","in_app"],key)
  nid=result["notification_ids"][0]
  assert conn.execute("SELECT count(*) FROM notification_deliveries WHERE notification_id=%s",(nid,)).fetchone()["count"]==0
  assert conn.execute("SELECT in_app_visible FROM notifications WHERE id=%s",(nid,)).fetchone()["in_app_visible"] is False
  conn.rollback()

def test_missing_verified_contact_is_permanent_delivery_failure(monkeypatch):
 from notification_service.service import enqueue,claim,deliver_one
 import notification_service.service as service
 user=uuid4(); key="integration-"+uuid4().hex
 with psycopg.connect(DSN,row_factory=dict_row) as conn:
  result=enqueue(conn,[user],"welcome",{},["email"],key)
  job=claim(conn,10)[0]
  monkeypatch.setattr(service,"verified_contact",lambda _user:{})
  deliver_one(conn,job)
  state=conn.execute("SELECT status,last_error_code FROM notification_deliveries WHERE id=%s",(job["id"],)).fetchone()
  assert state["status"]=="dead"
  assert state["last_error_code"]=="verified_email_unavailable"
  conn.rollback()
