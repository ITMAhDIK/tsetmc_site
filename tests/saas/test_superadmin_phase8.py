import pytest
from uuid import uuid4
from saas_core.admin.service import (
    change_user_role, update_plan, list_users, list_audit_logs,
)


def test_role_console_never_manages_superadmin_role():
    with pytest.raises(ValueError):
        change_user_role(None,actor_id=uuid4(),user_id=uuid4(),
                         role_code="superadmin",grant=True)


def test_plan_update_rejects_boolean_integer_fields():
    with pytest.raises(ValueError):
        update_plan(None,actor_id=uuid4(),plan_id=uuid4(),
                    changes={"price_toman":True})


def test_plan_update_requires_allowlisted_fields():
    with pytest.raises(ValueError):
        update_plan(None,actor_id=uuid4(),plan_id=uuid4(),
                    changes={"created_at":"2026-01-01"})


@pytest.mark.parametrize("limit,offset",[(0,0),(101,0),(10,-1),(10,10001)])
def test_user_list_pagination_is_bounded(limit,offset):
    with pytest.raises(ValueError):
        list_users(None,query=None,status=None,limit=limit,offset=offset)


def test_audit_pagination_is_bounded():
    with pytest.raises(ValueError):
        list_audit_logs(None,actor_id=None,action=None,target_type=None,limit=101)


def test_admin_roles_cannot_be_changed_by_user_themselves():
    with pytest.raises(ValueError):
        change_user_role(None,actor_id=uuid4(),user_id=uuid4(),
                         role_code="support",grant=True)
