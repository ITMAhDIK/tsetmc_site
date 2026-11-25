import pytest

from saas_core.account.service import (
    ALLOWED_PREFERENCES,
    revoke_one_session,
    update_preferences,
)

def test_preference_allow_list_is_explicit():
    assert ALLOWED_PREFERENCES["theme"] == {"light", "dark", "system"}
    assert ALLOWED_PREFERENCES["calendar"] == {"jalali", "gregorian"}
    assert ALLOWED_PREFERENCES["currency"] == {"toman", "rial"}

@pytest.mark.parametrize(
    "changes",
    [
        {"theme": "neon"},
        {"calendar": "unknown"},
        {"currency": "usd"},
        {"locale": "xx-XX"},
        {"is_admin": True},
    ],
)
def test_preferences_reject_unknown_or_invalid_values_before_database_access(changes):
    with pytest.raises(ValueError):
        update_preferences(None, None, changes)

def test_current_session_cannot_be_revoked_through_other_session_endpoint():
    session_id = "00000000-0000-0000-0000-000000000001"
    with pytest.raises(ValueError, match="logout endpoint"):
        revoke_one_session(None, None, session_id, session_id)
