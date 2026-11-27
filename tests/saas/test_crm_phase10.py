from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest


def test_crm_routes_are_mounted_with_expected_contract():
    from saas_core.auth.api import app

    paths = set(app.openapi()["paths"])
    assert {
        "/crm/customers",
        "/crm/customers/{customer_id}",
        "/crm/customers/{customer_id}/profile",
        "/crm/customers/{customer_id}/interactions",
        "/crm/followups",
        "/crm/followups/{followup_id}/status",
    } <= paths


def test_crm_service_rejects_invalid_lifecycle_stage_before_database_access():
    from saas_core.crm.service import update_customer_profile

    with pytest.raises(ValueError, match="invalid lifecycle stage"):
        update_customer_profile(
            None,
            actor_user_id=uuid4(),
            customer_id=uuid4(),
            lifecycle_stage="vip",
            assigned_to=None,
        )


@pytest.mark.parametrize("limit,offset", [(0, 0), (101, 0), (20, -1), (20, 10001)])
def test_crm_customer_pagination_is_bounded(limit, offset):
    from saas_core.crm.service import list_customers

    with pytest.raises(ValueError, match="invalid pagination"):
        list_customers(
            None,
            actor_user_id=uuid4(),
            limit=limit,
            offset=offset,
        )


def test_crm_customer_query_is_bounded():
    from saas_core.crm.service import list_customers

    with pytest.raises(ValueError, match="query must contain"):
        list_customers(
            None,
            actor_user_id=uuid4(),
            query="x" * 81,
        )


@pytest.mark.parametrize("status", ["pending", "paid", "deleted", "unknown"])
def test_crm_followup_filter_rejects_unknown_status(status):
    from saas_core.crm.service import list_followups

    with pytest.raises(ValueError, match="invalid follow-up status"):
        list_followups(
            None,
            actor_user_id=uuid4(),
            status=status,
        )


def test_crm_interaction_rejects_unknown_channels_before_database_access():
    from saas_core.crm.service import create_interaction

    with pytest.raises(ValueError, match="invalid interaction channel"):
        create_interaction(
            None,
            actor_user_id=uuid4(),
            customer_id=uuid4(),
            channel="whatsapp",
            subject="Customer follow-up",
            summary="A short interaction summary.",
        )


def test_crm_interaction_rejects_empty_or_oversized_summaries():
    from saas_core.crm.service import create_interaction

    for summary in (" ", "x" * 4001):
        with pytest.raises(ValueError, match="summary must contain"):
            create_interaction(
                None,
                actor_user_id=uuid4(),
                customer_id=uuid4(),
                channel="note",
                subject="Customer note",
                summary=summary,
            )


def test_crm_followup_requires_timezone_aware_due_date_before_database_access():
    from saas_core.crm.service import create_followup

    with pytest.raises(ValueError, match="due_at must include a timezone"):
        create_followup(
            None,
            actor_user_id=uuid4(),
            customer_id=uuid4(),
            assigned_to=None,
            title="Call customer",
            due_at=datetime(2026, 12, 1, 10, 0),
        )


def test_crm_migration_defines_staff_permissions_and_immutable_interactions():
    migration = (
        Path(__file__).resolve().parents[2]
        / "saas_core"
        / "schema"
        / "014_customer_relationship_management.sql"
    ).read_text(encoding="utf-8")

    assert "crm.read" in migration
    assert "crm.manage" in migration
    assert "crm_customer_profiles" in migration
    assert "crm_interactions" in migration
    assert "crm_followups" in migration
    assert "BEFORE UPDATE OR DELETE ON crm_interactions" in migration
    assert "r.code IN ('superadmin','admin')" in migration
    assert "r.code='support'" not in migration


def test_crm_is_in_saas_readiness_schema_requirements():
    from saas_core.auth.api import _REQUIRED_SCHEMA_TABLES

    assert {
        "crm_customer_profiles",
        "crm_interactions",
        "crm_followups",
    } <= set(_REQUIRED_SCHEMA_TABLES)
