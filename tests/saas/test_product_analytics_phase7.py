import pytest
from uuid import uuid4
from saas_core.analytics.service import (
    ALLOWED_EVENT_NAMES, normalize_page_path, validate_event,
)


def test_event_names_are_closed_allow_list():
    assert ALLOWED_EVENT_NAMES == {
        "page_view","click","navigation","scroll","active_time","feature_view"
    }


@pytest.mark.parametrize("path",[
    "/dashboard","/analytics/overview","/stocks/12345/details",
])
def test_page_path_is_normalized_and_dynamic_numeric_ids_are_removed(path):
    normalized=normalize_page_path(path)
    assert normalized.startswith("/")
    assert "? " not in normalized
    if "12345" in path:
        assert ":id" in normalized


@pytest.mark.parametrize("path",[
    "https://example.com/home","/account?token=secret","/account#private",
    "/profile/user@example.com","/bad path",
])
def test_page_path_rejects_origins_queries_fragments_and_free_text(path):
    with pytest.raises(ValueError):
        normalize_page_path(path)


def _event(**overrides):
    data={
        "client_event_id":uuid4(),"event_name":"page_view","page_path":"/dashboard",
        "component_id":None,"element_role":None,"viewport_width":1440,
        "viewport_height":900,"scroll_depth_pct":None,"active_duration_ms":None,
        "x_pct":None,"y_pct":None,
    }
    data.update(overrides)
    return data


def test_event_validator_keeps_only_allowlisted_fields():
    event=validate_event({**_event(),"password":"must-not-be-stored","text":"private text"})
    assert "password" not in event
    assert "text" not in event


def test_event_validator_requires_coordinate_pair():
    with pytest.raises(ValueError,match="supplied together"):
        validate_event(_event(x_pct=25,y_pct=None))


@pytest.mark.parametrize("duration",[180_001,-1,True])
def test_active_time_is_bounded(duration):
    with pytest.raises(ValueError):
        validate_event(_event(event_name="active_time",active_duration_ms=duration))


def test_path_query_is_not_collected():
    with pytest.raises(ValueError):
        validate_event(_event(page_path="/billing?email=private@example.com"))


def test_only_low_risk_event_fields_are_kept():
    event=validate_event(_event(event_name="click",x_pct=25,y_pct=60))
    assert event["x_pct"] == 25
    assert event["y_pct"] == 60


def test_scroll_and_active_duration_are_bound_to_matching_event_types():
    with pytest.raises(ValueError,match="only valid for scroll"):
        validate_event(_event(scroll_depth_pct=40))
    with pytest.raises(ValueError,match="only valid for active_time"):
        validate_event(_event(active_duration_ms=1000))


def test_click_coordinates_are_not_accepted_for_page_view():
    with pytest.raises(ValueError,match="only valid for click"):
        validate_event(_event(x_pct=10,y_pct=20))


def test_path_supports_html_routes_but_rejects_traversal():
    assert normalize_page_path("/help/article.html") == "/help/article.html"
    with pytest.raises(ValueError):
        normalize_page_path("/../../secret")
