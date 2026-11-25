import pytest
from saas_core.support.service import (
    clean_text, TICKET_STATUSES, TICKET_PRIORITIES, list_ticket_messages,
)


@pytest.mark.parametrize(
    "value,minimum,maximum",
    [
        ("  valid subject  ", 3, 180),
        ("first message", 2, 10_000),
    ],
)
def test_clean_text_trims_valid_text(value,minimum,maximum):
    assert clean_text(value,field="text",minimum=minimum,maximum=maximum) == value.strip()


@pytest.mark.parametrize("value",["", "  ", "x"*181])
def test_clean_text_rejects_empty_or_oversized_subject(value):
    with pytest.raises(ValueError):
        clean_text(value,field="subject",minimum=3,maximum=180)


def test_ticket_status_and_priority_values_are_closed_allow_lists():
    assert TICKET_STATUSES == {"open","waiting","answered","closed"}
    assert TICKET_PRIORITIES == {"low","normal","high","urgent"}


class _FakeCursor:
    def __init__(self):
        self.query = ""
        self.params = None
    def __enter__(self):
        return self
    def __exit__(self,*args):
        return False
    def execute(self,query,params=None):
        self.query = query
        self.params = params
    def fetchall(self):
        return []


class _FakeConnection:
    def __init__(self):
        self.last_cursor = _FakeCursor()
    def cursor(self):
        return self.last_cursor


def test_customer_message_query_excludes_internal_notes():
    conn = _FakeConnection()
    list_ticket_messages(conn,ticket_id="ticket-id",include_internal=False)
    assert "is_internal_note=FALSE" in conn.last_cursor.query


def test_staff_message_query_can_include_internal_notes():
    conn = _FakeConnection()
    list_ticket_messages(conn,ticket_id="ticket-id",include_internal=True)
    assert "is_internal_note=FALSE" not in conn.last_cursor.query
