"""PostgreSQL-backed API integration tests for Phase 10 CRM.

Only an explicitly named disposable SaaS test database is accepted. No market
or production database is opened by these tests.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

_DSN = os.environ.get("SAAS_TEST_DATABASE_URL", "").strip()
_EXPECTED_DB = os.environ.get("SAAS_TEST_DATABASE_NAME", "").strip()

pytestmark = pytest.mark.skipif(
    not _DSN or not _EXPECTED_DB,
    reason="CRM integration tests require an explicitly named disposable PostgreSQL database",
)


@pytest.fixture(scope="module", autouse=True)
def verify_crm_test_database():
    if not _DSN or not _EXPECTED_DB:
        pytest.skip("disposable SaaS test database is not configured")
    required_tables = (
        "users", "roles", "permissions", "role_permissions", "sessions",
        "admin_audit_logs", "user_admin_mfa", "crm_customer_profiles",
        "crm_interactions", "crm_followups",
    )
    with psycopg.connect(_DSN, row_factory=dict_row) as conn:
        actual = conn.execute("SELECT current_database() AS name").fetchone()["name"]
        if actual != _EXPECTED_DB:
            pytest.fail("Refusing CRM tests: database identity does not match")
        row = conn.execute(
            "SELECT " + ", ".join(
                f"to_regclass('public.{name}') IS NOT NULL AS {name}"
                for name in required_tables
            )
        ).fetchone()
        missing = [name for name in required_tables if not row[name]]
        if missing:
            pytest.fail("CRM migration is incomplete: " + ", ".join(missing))


@pytest.fixture
def crm_context(monkeypatch):
    from saas_core.auth.api import app, get_conn
    from saas_core.auth.mfa import totp_code
    from saas_core.auth.repository import create_session, register_password_user

    monkeypatch.setenv(
        "SAAS_ADMIN_MFA_FERNET_KEY",
        Fernet.generate_key().decode("ascii"),
    )

    with psycopg.connect(_DSN, row_factory=dict_row) as setup:
        actual = setup.execute("SELECT current_database() AS name").fetchone()["name"]
        if actual != _EXPECTED_DB:
            pytest.fail("Refusing CRM fixture setup against an unexpected database")

        admin_username = f"crm_admin_{uuid4().hex[:12]}"
        admin = register_password_user(
            setup,
            admin_username,
            "CRM-integration-password-2026",
            display_name="CRM Integration Admin",
        )
        customer = register_password_user(
            setup,
            f"crm_customer_{uuid4().hex[:12]}",
            "CRM-customer-password-2026",
            display_name="CRM Test Customer",
        )
        ordinary = register_password_user(
            setup,
            f"crm_ordinary_{uuid4().hex[:12]}",
            "CRM-ordinary-password-2026",
            display_name="CRM Ordinary User",
        )
        setup.execute(
            """INSERT INTO user_roles(user_id,role_id)
               SELECT %s,id FROM roles WHERE code='admin'
               ON CONFLICT DO NOTHING""",
            (admin["id"],),
        )
        unique_phone = f"+989{int(uuid4().hex[:10], 16) % 10**10:010d}"
        setup.execute(
            """UPDATE users
               SET phone_e164=%s,phone_verified_at=now(),
                   email=%s,email_verified_at=now()
               WHERE id=%s""",
            (unique_phone, f"{admin_username}@example.test", customer["id"]),
        )
        admin_token, _ = create_session(setup, admin["id"])
        customer_token, _ = create_session(setup, customer["id"])
        ordinary_token, _ = create_session(setup, ordinary["id"])
        setup.commit()

    def test_connection():
        with psycopg.connect(_DSN, row_factory=dict_row) as conn:
            yield conn

    previous = app.dependency_overrides.get(get_conn)
    app.dependency_overrides[get_conn] = test_connection
    try:
        with TestClient(app) as client:
            headers = {"Authorization": f"Bearer {admin_token}"}
            setup_response = client.post("/auth/mfa/setup", headers=headers)
            assert setup_response.status_code == 200, setup_response.text
            confirm_response = client.post(
                "/auth/mfa/confirm",
                headers=headers,
                json={"code": totp_code(setup_response.json()["secret"])},
            )
            assert confirm_response.status_code == 200, confirm_response.text
            yield {
                "client": client,
                "admin": admin,
                "admin_headers": headers,
                "customer": customer,
                "customer_headers": {"Authorization": f"Bearer {customer_token}"},
                "ordinary_headers": {"Authorization": f"Bearer {ordinary_token}"},
            }
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_conn, None)
        else:
            app.dependency_overrides[get_conn] = previous


def test_crm_customer_interaction_followup_and_audit_lifecycle(crm_context):
    context = crm_context
    client = context["client"]
    admin = context["admin"]
    admin_headers = context["admin_headers"]
    customer = context["customer"]
    customer_id = str(customer["id"])

    # CRM has a separate staff boundary and requires an MFA-verified session.
    assert client.get("/crm/customers").status_code == 401
    assert client.get("/crm/customers", headers=context["ordinary_headers"]).status_code == 403

    # The same admin MFA does not grant the Superadmin-only operations console.
    assert client.get("/admin/dashboard", headers=admin_headers).status_code == 403

    listed = client.get(
        "/crm/customers?q=CRM+Test+Customer&lifecycle_stage=new",
        headers=admin_headers,
    )
    assert listed.status_code == 200, listed.text
    match = next(
        item for item in listed.json()["customers"]
        if str(item["id"]) == customer_id
    )
    assert match["lifecycle_stage"] == "new"
    assert match["phone_verified"] is True
    assert match["email_verified"] is True
    assert "phone_e164" not in match
    assert "email" not in match
    assert listed.json()["total"] >= 1

    detail = client.get(f"/crm/customers/{customer_id}", headers=admin_headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["customer"]["id"] == str(customer["id"])
    assert "phone_e164" not in detail.json()["customer"]
    assert "email" not in detail.json()["customer"]

    profile = client.patch(
        f"/crm/customers/{customer_id}/profile",
        headers=admin_headers,
        json={"lifecycle_stage": "at_risk", "assigned_to": str(admin["id"])},
    )
    assert profile.status_code == 200, profile.text
    assert profile.json()["profile"]["lifecycle_stage"] == "at_risk"
    assert str(profile.json()["profile"]["assigned_to"]) == str(admin["id"])

    invalid_assignee = client.patch(
        f"/crm/customers/{customer_id}/profile",
        headers=admin_headers,
        json={"lifecycle_stage": "active", "assigned_to": customer_id},
    )
    assert invalid_assignee.status_code == 422

    interaction = client.post(
        f"/crm/customers/{customer_id}/interactions",
        headers=admin_headers,
        json={
            "channel": "call",
            "subject": "Quarterly account check-in",
            "summary": "Discussed account usage and requested a follow-up.",
        },
    )
    assert interaction.status_code == 201, interaction.text
    interaction_id = interaction.json()["interaction"]["id"]
    assert interaction.json()["interaction"]["channel"] == "call"

    history = client.get(
        f"/crm/customers/{customer_id}/interactions",
        headers=admin_headers,
    )
    assert history.status_code == 200, history.text
    assert any(row["id"] == interaction_id for row in history.json()["interactions"])

    due_at = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    followup = client.post(
        "/crm/followups",
        headers=admin_headers,
        json={
            "customer_id": customer_id,
            "assigned_to": str(admin["id"]),
            "title": "Review customer feedback",
            "details": "Check progress after the next account review.",
            "due_at": due_at,
        },
    )
    assert followup.status_code == 201, followup.text
    followup_id = followup.json()["followup"]["id"]

    open_items = client.get("/crm/followups", headers=admin_headers)
    assert open_items.status_code == 200
    assert any(item["id"] == followup_id for item in open_items.json()["followups"])

    completed = client.patch(
        f"/crm/followups/{followup_id}/status",
        headers=admin_headers,
        json={"status": "completed"},
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["followup"]["status"] == "completed"
    assert completed.json()["followup"]["completed_at"] is not None

    open_after = client.get("/crm/followups", headers=admin_headers)
    assert all(item["id"] != followup_id for item in open_after.json()["followups"])

    # Interaction history cannot be rewritten or deleted, even by SQL.
    with psycopg.connect(_DSN, row_factory=dict_row) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute(
                "UPDATE crm_interactions SET summary='rewritten' WHERE id=%s",
                (interaction_id,),
            )
        conn.rollback()
        audit_count = conn.execute(
            """SELECT COUNT(*)::integer AS total FROM admin_audit_logs
               WHERE actor_user_id=%s AND action LIKE 'crm.%%'""",
            (admin["id"],),
        ).fetchone()["total"]
    assert audit_count >= 7


def test_crm_customer_and_followup_routes_validate_missing_records(crm_context):
    context = crm_context
    client = context["client"]
    headers = context["admin_headers"]
    missing_id = str(uuid4())

    assert client.get(f"/crm/customers/{missing_id}", headers=headers).status_code == 404
    assert client.get(
        f"/crm/customers/{missing_id}/interactions", headers=headers
    ).status_code == 404
    assert client.patch(
        f"/crm/followups/{missing_id}/status",
        headers=headers,
        json={"status": "completed"},
    ).status_code == 404


def test_crm_role_permissions_are_scoped_to_admin_and_superadmin():
    with psycopg.connect(_DSN, row_factory=dict_row) as conn:
        matrix = conn.execute(
            """SELECT r.code AS role_code, p.code AS permission_code
               FROM role_permissions rp
               JOIN roles r ON r.id=rp.role_id
               JOIN permissions p ON p.id=rp.permission_id
               WHERE p.code IN ('crm.read','crm.manage')"""
        ).fetchall()
    mapping = {}
    for row in matrix:
        mapping.setdefault(row["role_code"], set()).add(row["permission_code"])
    # Keep role names explicit in the assertion; support/user roles must not
    # inherit CRM access through the support-ticket permission.
    assert mapping == {
        "admin": {"crm.read", "crm.manage"},
        "superadmin": {"crm.read", "crm.manage"},
    }
