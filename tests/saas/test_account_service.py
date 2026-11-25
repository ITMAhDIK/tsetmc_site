import pytest

from saas_core.account.service import (
    ALLOWED_PREFERENCES,
    list_sessions,
    revoke_one_session,
    update_preferences,
)
from saas_core.auth.api import app


class _Cursor:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.statement = None
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, statement, params=None):
        self.statement = statement
        self.params = params

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def transaction(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_preference_allow_list_is_explicit():
    assert ALLOWED_PREFERENCES == {
        "theme": {"light", "dark", "system"},
        "calendar": {"jalali", "gregorian"},
        "currency": {"toman", "rial"},
        "locale": {"fa-IR", "en-US"},
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"theme": "neon"},
        {"calendar": "unknown"},
        {"currency": "usd"},
        {"locale": "xx-XX"},
        {"is_admin": True},
        {"theme": None},
    ],
)
def test_preferences_reject_unknown_or_invalid_values_before_database_access(changes):
    with pytest.raises(ValueError):
        update_preferences(None, None, changes)


def test_current_session_cannot_be_revoked_through_other_session_endpoint():
    session_id = "00000000-0000-0000-0000-000000000001"
    with pytest.raises(ValueError, match="logout endpoint"):
        revoke_one_session(None, None, session_id, session_id)


def test_session_listing_is_scoped_to_authenticated_user_and_marks_current_session():
    cursor = _Cursor([{"is_current": True}])
    result = list_sessions(_Connection(cursor), "user-123", "session-456")
    assert result == [{"is_current": True}]
    assert "WHERE user_id=%s" in cursor.statement
    assert cursor.params == ("session-456", "user-123")


def test_single_session_revocation_is_scoped_to_user_and_session():
    cursor = _Cursor([{"id": "other-session"}])
    result = revoke_one_session(
        _Connection(cursor), "owner-user", "other-session", "current-session"
    )
    assert result is True
    assert "WHERE id=%s AND user_id=%s" in cursor.statement
    assert cursor.params == ("other-session", "owner-user")


def test_account_routes_are_registered_on_saas_api():
    routes = {
        (route.path, tuple(sorted(route.methods or [])))
        for route in app.routes
    }
    expected = {
        ("/account/profile", ("GET",)),
        ("/account/profile", ("PATCH",)),
        ("/account/preferences", ("GET",)),
        ("/account/preferences", ("PATCH",)),
        ("/account/sessions", ("GET",)),
        ("/account/sessions/{session_id}", ("DELETE",)),
        ("/account/sessions/revoke-others", ("POST",)),
        ("/account/subscriptions", ("GET",)),
        ("/account/features", ("GET",)),
    }
    assert expected <= routes
