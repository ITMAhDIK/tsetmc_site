from pathlib import Path
from uuid import uuid4
from datetime import datetime, timezone
from fastapi.testclient import TestClient
import pytest

ROOT=Path(__file__).resolve().parents[2]

def test_crm_is_an_independent_fastapi_service():
    from crm_service.api import app
    paths=set(app.openapi()["paths"])
    assert {"/health","/ready","/customers","/customers/{customer_id}","/customers/{customer_id}/interactions","/followups"} <= paths

def test_crm_schema_is_independent_and_interactions_are_immutable():
    sql=(ROOT/"crm_service/schema/001_crm_core.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS crm_customers" in sql
    assert "REFERENCES users" not in sql
    assert "BEFORE UPDATE OR DELETE ON crm_interactions" in sql
    assert "crm_audit_log" in sql

def test_identity_adapter_does_not_query_identity_database():
    source=(ROOT/"crm_service/identity.py").read_text()
    assert "/internal/introspect" in source
    assert "IDENTITY_SERVICE_TOKEN" in source
    assert "SELECT " not in source

def test_crm_projection_requires_service_token(monkeypatch):
    from crm_service.api import _projection_auth
    monkeypatch.setenv("CRM_PROJECTION_TOKEN","a"*40)
    with pytest.raises(Exception):
        _projection_auth(None)

def test_crm_runtime_refuses_shared_database_names(monkeypatch):
    from crm_service.db import validate_production_configuration
    monkeypatch.setenv("APP_ENV","production")
    monkeypatch.setenv("CRM_DATABASE_URL","postgresql://x:y@localhost/db?sslmode=verify-full")
    monkeypatch.setenv("CRM_EXPECTED_DATABASE_NAME","tloomberg_saas")
    monkeypatch.setenv("SAAS_EXPECTED_DATABASE_NAME","tloomberg_saas")
    monkeypatch.setenv("CRM_PROJECTION_TOKEN","x"*40)
    monkeypatch.setenv("IDENTITY_SERVICE_URL","http://identity:8000")
    monkeypatch.setenv("IDENTITY_SERVICE_TOKEN","y"*40)
    monkeypatch.setenv("CRM_TRUSTED_HOSTS","crm.example.com")
    monkeypatch.setenv("CRM_ALLOWED_ORIGINS","https://crm.example.com")
    with pytest.raises(RuntimeError,match="dedicated"):
        validate_production_configuration()

def test_migration_import_is_dry_run_first_and_source_read_only():
    source=(ROOT/"scripts/migrate_legacy_crm.py").read_text()
    assert "required=True" in source
    assert "SET TRANSACTION READ ONLY" in source
    assert "ON CONFLICT(id) DO NOTHING" in source
