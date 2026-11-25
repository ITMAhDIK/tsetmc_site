import pytest
from saas_core.support.service import clean_text, TICKET_STATUSES, TICKET_PRIORITIES


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
