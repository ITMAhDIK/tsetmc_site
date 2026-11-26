import pytest
from uuid import uuid4

from site_accounts import AccountError, AccountService


@pytest.fixture
def service(tmp_path):
    return AccountService(str(tmp_path / "site_accounts.sqlite"))


def signed_in(service, username, role=None):
    user_id = service.register({
        "username": username,
        "password": "analytics-test-password-2026",
    })
    if role:
        con = service._connect()
        try:
            with con:
                con.execute("UPDATE site_users SET role=? WHERE id=?", (role, user_id))
        finally:
            con.close()
    raw_token, _payload, _ttl = service._new_session(user_id, remember=True)
    return service.authenticate(raw_token)


def dispatch_consent(service, user, enabled):
    return service.dispatch(
        "PUT", "/api/v1/analytics/consent", {},
        {"product_analytics": enabled, "_csrf": user["csrf_token"]},
        user, "http://localhost:8000",
    )["payload"]["consent"]


def post_events(service, user, events):
    return service.dispatch(
        "POST", "/api/v1/analytics/events", {},
        {"events": events, "_csrf": user["csrf_token"]},
        user, "http://localhost:8000",
    )["payload"]


def page_views(service, user, paths):
    return post_events(service, user, [
        {"client_event_id": str(uuid4()), "event_name": "page_view", "page_path": path}
        for path in paths
    ])


def test_analytics_is_off_by_default_and_requires_explicit_consent(service):
    user = signed_in(service, "analytics_off_user")
    response = service.dispatch(
        "GET", "/api/v1/analytics/consent", {}, {}, user, "http://localhost:8000"
    )["payload"]["consent"]
    assert response["product_analytics"] is False
    assert response["has_choice"] is False
    with pytest.raises(AccountError) as exc:
        page_views(service, user, ["/market"])
    assert exc.value.status == 403


def test_event_ingestion_is_session_bound_deduplicated_and_minimized(service):
    user = signed_in(service, "analytics_event_user")
    dispatch_consent(service, user, True)
    client_id = str(uuid4())
    event = {
        "client_event_id": client_id,
        "event_name": "click",
        "page_path": "/market?phone=%2B989121234567#private",
        "component_id": "nav-market",
        "element_role": "tab",
        "typed_text": "must never be stored",
        "password": "must never be stored",
        "raw_click_text": "must never be stored",
    }
    with pytest.raises(AccountError):
        post_events(service, user, [event])

    event["page_path"] = "/market"
    assert post_events(service, user, [event]) == {
        "received": 1, "inserted": 1, "duplicates": 0
    }
    assert post_events(service, user, [event]) == {
        "received": 1, "inserted": 0, "duplicates": 1
    }
    with pytest.raises(AccountError) as exc:
        post_events(service, user, [dict(event, page_path="/stock")])
    assert exc.value.status == 409

    con = service._connect()
    try:
        row = dict(con.execute(
            """SELECT user_id,session_hash,page_path,component_id,element_role,
                      payload_sha256 FROM site_analytics_events WHERE user_id=?""",
            (user["id"],),
        ).fetchone())
        assert row["user_id"] == user["id"]
        assert row["session_hash"] == user["session_hash"]
        assert row["page_path"] == "/market"
        assert row["component_id"] == "nav-market"
        assert row["element_role"] == "tab"
        assert "typed_text" not in row and "password" not in row
        assert "raw_click_text" not in row
    finally:
        con.close()


def test_revoking_consent_deletes_existing_events_and_blocks_future_ingestion(service):
    user = signed_in(service, "analytics_revoke_user")
    dispatch_consent(service, user, True)
    page_views(service, user, ["/market", "/stock"])
    disabled = dispatch_consent(service, user, False)
    assert disabled["product_analytics"] is False
    assert disabled["deleted_events"] == 2
    con = service._connect()
    try:
        assert con.execute(
            "SELECT COUNT(*) FROM site_analytics_events WHERE user_id=?",
            (user["id"],),
        ).fetchone()[0] == 0
    finally:
        con.close()
    with pytest.raises(AccountError) as exc:
        page_views(service, user, ["/market"])
    assert exc.value.status == 403


def test_repeated_paths_are_counted_per_session_and_personal_views_are_superadmin_only(service):
    users = []
    for index in range(5):
        user = signed_in(service, f"analytics_path_user_{index}")
        dispatch_consent(service, user, True)
        page_views(service, user, [
            "/market", "/stock/فملی", "/shareholders",
            "/market", "/stock/فملی", "/shareholders",
        ])
        users.append(user)

    admin = signed_in(service, "analytics_route_admin", role="admin")
    aggregate = service.dispatch(
        "GET", "/api/v1/analytics/admin/paths",
        {"days": ["30"], "max_steps": ["3"]}, {}, admin, "http://localhost:8000",
    )["payload"]
    assert aggregate["suppressed"] is False
    target = next(x for x in aggregate["paths"]
                  if x["path"] == ["/market", "/stock/فملی", "/shareholders"])
    assert target["repetitions"] == 10
    assert target["sessions"] == 5
    assert target["users"] == 5

    ordinary = users[0]
    with pytest.raises(AccountError) as exc:
        service.dispatch(
            "GET", f"/api/v1/analytics/admin/users/{ordinary['id']}/paths",
            {}, {}, admin, "http://localhost:8000",
        )
    assert exc.value.status == 403

    superadmin = signed_in(service, "analytics_super_admin", role="super_admin")
    personal = service.dispatch(
        "GET", f"/api/v1/analytics/admin/users/{ordinary['id']}/paths",
        {"days": ["30"], "max_steps": ["3"]}, {}, superadmin, "http://localhost:8000",
    )["payload"]
    personal_target = next(x for x in personal["paths"]
                           if x["path"] == ["/market", "/stock/فملی", "/shareholders"])
    assert personal_target["repetitions"] == 2
    assert personal_target["sessions"] == 1
    assert personal_target["users"] == 1

    con = service._connect()
    try:
        count = con.execute(
            """SELECT COUNT(*) FROM site_analytics_audit
               WHERE actor_user_id=? AND target_user_id=? AND action='user_paths.read'""",
            (superadmin["id"], ordinary["id"]),
        ).fetchone()[0]
        assert count == 1
    finally:
        con.close()


def test_aggregate_path_reports_are_suppressed_below_five_users(service):
    for index in range(4):
        user = signed_in(service, f"analytics_small_cohort_{index}")
        dispatch_consent(service, user, True)
        page_views(service, user, ["/market", "/stock", "/market", "/stock"])
    admin = signed_in(service, "analytics_small_cohort_admin", role="admin")
    result = service.dispatch(
        "GET", "/api/v1/analytics/admin/paths", {}, {}, admin, "http://localhost:8000"
    )["payload"]
    assert result == {"suppressed": True, "minimum_cohort": 5, "paths": []}


@pytest.mark.parametrize("path", [
    "https://example.com/market", "/market?query=private",
    "/market#fragment", "/../private",
])
def test_analytics_page_path_rejects_full_urls_queries_fragments_and_traversal(path):
    with pytest.raises(AccountError):
        AccountService._analytics_page_path(path)


def test_analytics_path_normalization_hides_numeric_ids_but_preserves_persian_symbols():
    assert AccountService._analytics_page_path("/company/123456") == "/company/:id"
    assert AccountService._analytics_page_path("/stock/فملی") == "/stock/فملی"


def test_event_batch_is_bounded_to_fifty_items(service):
    user = signed_in(service, "analytics_batch_bound_user")
    dispatch_consent(service, user, True)
    events = [{
        "client_event_id": str(uuid4()), "event_name": "page_view", "page_path": "/market"
    } for _ in range(51)]
    with pytest.raises(AccountError) as exc:
        post_events(service, user, events)
    assert exc.value.status == 400


def test_analytics_endpoints_do_not_intercept_sector_affinity():
    assert AccountService.handles("/api/v1/analytics/consent")
    assert AccountService.handles("/api/v1/analytics/events")
    assert AccountService.handles("/api/v1/analytics/admin/paths")
    assert AccountService.handles("/api/v1/analytics/admin/users/12/events")
    assert not AccountService.handles("/api/v1/analytics/sector-affinity")
