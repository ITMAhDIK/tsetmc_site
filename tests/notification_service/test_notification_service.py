from pathlib import Path
from uuid import uuid4
from fastapi.testclient import TestClient
from fastapi import HTTPException
import pytest
ROOT=Path(__file__).resolve().parents[2]

def test_notification_service_exposes_public_and_private_contracts():
 from notification_service.api import app
 paths=set(app.openapi()["paths"])
 assert {"/health","/ready","/notifications","/notifications/{notification_id}/read","/notifications/preferences"} <= paths
 assert "/internal/notifications" not in paths

def test_notification_schema_has_independent_store_idempotency_and_queue():
 sql=(ROOT/"notification_service/schema/001_notification_core.sql").read_text()
 assert "CREATE TABLE IF NOT EXISTS notifications" in sql
 assert "CREATE TABLE IF NOT EXISTS notification_deliveries" in sql
 assert "CREATE TABLE IF NOT EXISTS notification_idempotency" in sql
 assert "FOR UPDATE" not in sql
 assert "REFERENCES users" not in sql
 assert "FOR UPDATE" not in sql

def test_notification_queue_uses_skip_locked_and_leases():
 source=(ROOT/"notification_service/service.py").read_text()
 assert "FOR UPDATE SKIP LOCKED" in source
 assert "locked_at<now()-interval '5 minutes'" in source
 assert "lock_token" in source

def test_template_allowlist_and_safe_variable_rendering():
 from notification_service.service import _render,TEMPLATES
 assert _render("welcome",{"name":"Mahdi"})[0]=="Welcome Mahdi to Tloomberg"
 assert "<script>" in _render("welcome",{"name":"<script>"})[0]\n assert "&lt;script&gt;" in _render("welcome",{"name":"<script>"})[2]
 assert "arbitrary" not in TEMPLATES

def test_unknown_template_is_rejected():
 from notification_service.service import enqueue
 class Conn: pass
 with pytest.raises(HTTPException) as e:
  enqueue(Conn(),[uuid4()],"custom_html",{},["email"],"idempotency-123")
 assert e.value.status_code==422

def test_idempotency_key_format_is_bounded():
 from notification_service.service import enqueue
 class Conn: pass
 with pytest.raises(HTTPException) as e:
  enqueue(Conn(),[uuid4()],"welcome",{},["in_app"],"x")
 assert e.value.status_code==422

def test_worker_uses_autocommit_for_polling_loop():
 source=(ROOT/"notification_service/worker.py").read_text()
 assert "autocommit=True" in source

def test_identity_contact_endpoint_returns_only_verified_channels():
 source=(ROOT/"identity_service/api.py").read_text()
 assert '"/internal/notification-contact/{user_id}"' in source
 assert 'row["phone_verified_at"]' in source and 'row["email_verified_at"]' in source
 assert "phone_e164" in source and "email" in source

def test_delivery_provider_requires_https_for_sms_and_starttls_for_email():
 source=(ROOT/"notification_service/providers.py").read_text()
 assert 'url.startswith("https://")' in source
 assert "smtp.starttls()" in source
 assert "Authorization" in source

def test_notification_api_requires_private_ingest_token():
 source=(ROOT/"notification_service/api.py").read_text()
 assert "NOTIFICATION_INGEST_TOKEN" in source
 assert "hmac.compare_digest" in source
 assert 'include_in_schema=False' in source

def test_notification_cannot_access_arbitrary_user_inbox():
 source=(ROOT/"notification_service/api.py").read_text()
 assert 'user["id"]' in source
 assert "WHERE id=%s AND user_id=%s AND in_app_visible=TRUE" in source

def test_production_configuration_requires_dedicated_database_and_identity_token():
 source=(ROOT/"notification_service/db.py").read_text()
 assert "NOTIFICATION_EXPECTED_DATABASE_NAME" in source
 assert "IDENTITY_SERVICE_TOKEN" in source
 assert "sslmode" in source and "verify-full" in source
