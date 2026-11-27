"""Production boundary tests for the public market API.

The read-only switch is intentionally tested at the route dispatcher so no
database fixture or live source file is required.
"""
from __future__ import annotations

import pytest

from site_api.dispatch import dispatch_api
from site_api.errors import ApiError


class PublicApi:
    def market_overview(self):
        return {"ok": True}

    def screener_query(self, body):
        return {"query": body}

    def screener_export(self, body):
        return b"xlsx"

    def watchlist_get(self):
        return {"items": []}

    def saved_filters_list(self):
        return {"filters": []}

    def admin_overview(self):
        return {"admin": True}

    def admin_manifests(self, params):
        return {"rows": []}


def test_public_mode_allows_market_read_endpoints(monkeypatch):
    monkeypatch.setenv("MARKET_PUBLIC_READ_ONLY", "true")
    result, status, content_type = dispatch_api(
        PublicApi(), "GET", "/api/v1/market/overview", {}, {}
    )
    assert result == {"ok": True}
    assert status == 200
    assert content_type == "json"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/v1/watchlists/default", {}),
        ("POST", "/api/v1/watchlists/default/symbols", {"symbol": "فولاد"}),
        ("DELETE", "/api/v1/watchlists/default/symbols/فولاد", {}),
        ("GET", "/api/v1/screener/saved-filters", {}),
        ("POST", "/api/v1/screener/saved-filters", {"name": "private"}),
        ("GET", "/api/v1/admin/overview", {}),
        ("GET", "/api/v1/admin/manifests", {}),
        ("POST", "/api/v1/admin/ingestion/trigger", {}),
    ],
)
def test_public_mode_rejects_user_state_and_legacy_admin_routes(monkeypatch, method, path, body):
    monkeypatch.setenv("MARKET_PUBLIC_READ_ONLY", "true")
    expected = 404 if "/admin/" in path else (403 if method != "GET" else 403)
    with pytest.raises(ApiError) as exc:
        dispatch_api(PublicApi(), method, path, {}, body)
    assert exc.value.status == expected


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/v1/screener/query"),
    ("POST", "/api/v1/screener/export"),
])
def test_public_mode_allows_only_side_effect_free_screener_post_operations(monkeypatch, method, path):
    monkeypatch.setenv("MARKET_PUBLIC_READ_ONLY", "true")
    result, status, kind = dispatch_api(PublicApi(), method, path, {}, {"filters": []})
    assert status == 200
    assert kind in {"json", "xlsx"}
    assert result in ({"query": {"filters": []}}, b"xlsx")


def test_local_mode_preserves_existing_watchlist_and_admin_route_contracts(monkeypatch):
    monkeypatch.delenv("MARKET_PUBLIC_READ_ONLY", raising=False)
    assert dispatch_api(PublicApi(), "GET", "/api/v1/watchlists/default", {}, {})[0] == {"items": []}
    assert dispatch_api(PublicApi(), "GET", "/api/v1/admin/overview", {}, {})[0] == {"admin": True}
