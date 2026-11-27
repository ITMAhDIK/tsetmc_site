from __future__ import annotations

from pathlib import Path

from site_api.dispatch import dispatch_api
from site_api.errors import ApiError


ROOT = Path(__file__).resolve().parents[2]


class RecordingApi:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(*args):
            self.calls.append((name, args))
            return {"legacy": name}
        return call


def test_dispatch_proxies_query_and_export_to_screener_service(monkeypatch):
    import site_api.dispatch as dispatch

    monkeypatch.setenv("SCREENER_SERVICE_URL", "http://screener-api:8000")
    monkeypatch.setenv("MARKET_PUBLIC_READ_ONLY", "true")
    calls = []
    monkeypatch.setattr(
        dispatch,
        "proxy_screener_request",
        lambda path, body: (
            calls.append((path, body))
            or (
                (b"xlsx-bytes", 200, "xlsx")
                if path.endswith("/export")
                else ({"rows": []}, 200, "json")
            )
        ),
    )
    api = RecordingApi()
    query = {"conditions": [{"field": "market", "op": "=", "value": "TSE"}]}
    assert dispatch_api(api, "POST", "/api/v1/screener/query", {}, query) == (
        {"rows": []}, 200, "json"
    )
    assert dispatch_api(api, "POST", "/api/v1/screener/export", {}, query) == (
        b"xlsx-bytes", 200, "xlsx"
    )
    assert calls == [
        ("/api/v1/screener/query", query),
        ("/api/v1/screener/export", query),
    ]
    assert api.calls == []


def test_dispatch_uses_legacy_screener_only_when_service_is_not_configured(monkeypatch):
    import site_api.dispatch as dispatch

    monkeypatch.delenv("SCREENER_SERVICE_URL", raising=False)
    monkeypatch.setenv("MARKET_PUBLIC_READ_ONLY", "false")
    monkeypatch.setattr(dispatch, "proxy_screener_request", lambda *_args: None)
    api = RecordingApi()
    result = dispatch_api(
        api,
        "POST",
        "/api/v1/screener/query",
        {},
        {"conditions": []},
    )
    assert result == ({"legacy": "screener_query"}, 200, "json")
    assert api.calls == [("screener_query", ({"conditions": []},))]


def test_configured_screener_outage_fails_closed_without_legacy_fallback(monkeypatch):
    import site_api.dispatch as dispatch

    monkeypatch.setenv("SCREENER_SERVICE_URL", "http://screener-api:8000")
    monkeypatch.setenv("MARKET_PUBLIC_READ_ONLY", "true")
    monkeypatch.setattr(
        dispatch,
        "proxy_screener_request",
        lambda *_args: (_ for _ in ()).throw(ApiError(503, "screener offline")),
    )
    api = RecordingApi()
    try:
        dispatch_api(api, "POST", "/api/v1/screener/query", {}, {})
    except ApiError as exc:
        assert exc.status == 503
    else:
        raise AssertionError("configured Screener outage must fail closed")
    assert api.calls == []


def test_screener_client_transmits_json_and_accepts_safe_xlsx_response(monkeypatch):
    import site_api.screener_client as client

    monkeypatch.setenv("SCREENER_SERVICE_URL", "http://screener-api:8000")
    captured = []

    class Headers(dict):
        pass

    class Response:
        status = 200

        def __init__(self, content, content_type):
            self.content = content
            self.headers = Headers({"Content-Type": content_type})

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return self.content

    def fake_urlopen(request, timeout):
        captured.append((request.full_url, request.method, request.data, request.headers, timeout))
        if request.full_url.endswith("/export"):
            return Response(b"zip-data", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        return Response(b'{"rows":[]}', "application/json; charset=utf-8")

    monkeypatch.setattr(client, "urlopen", fake_urlopen)
    body = {"logic": "AND", "conditions": [{"field": "market", "op": "=", "value": "TSE"}]}
    assert client.proxy_screener_request("/api/v1/screener/query", body) == (
        {"rows": []}, 200, "json"
    )
    assert client.proxy_screener_request("/api/v1/screener/export", body) == (
        b"zip-data", 200, "xlsx"
    )
    assert all(item[1] == "POST" for item in captured)
    assert all(item[2] == b'{"logic":"AND","conditions":[{"field":"market","op":"=","value":"TSE"}]}' for item in captured)
    assert all(item[4] == 20.0 for item in captured)


def test_screener_client_rejects_unknown_route_and_fails_closed_on_outage(monkeypatch):
    from urllib.error import URLError
    import site_api.screener_client as client

    monkeypatch.setenv("SCREENER_SERVICE_URL", "http://screener-api:8000")
    try:
        client.proxy_screener_request("/api/v1/screener/saved-filters", {})
    except ApiError as exc:
        assert exc.status == 404
    else:
        raise AssertionError("saved-filter routes must not be forwarded to Screener")

    monkeypatch.setattr(
        client, "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("offline")),
    )
    try:
        client.proxy_screener_request("/api/v1/screener/query", {})
    except ApiError as exc:
        assert exc.status == 503
    else:
        raise AssertionError("configured Screener outage must fail closed")


def test_production_edge_routes_only_screener_query_and_export():
    caddy = (ROOT / "Caddyfile.market").read_text(encoding="utf-8")
    assert "@screener path" in caddy
    assert "/api/v1/screener/query" in caddy
    assert "/api/v1/screener/export" in caddy
    assert "reverse_proxy screener-api:8000" in caddy
    # Saved-filter persistence remains on Market API until it has a
    # per-account ownership model; do not route it to the query-only service.
    assert "/api/v1/screener/saved-filters" not in caddy


def test_screener_compose_service_is_sidecar_read_only_and_has_no_market_source_mount():
    compose = (ROOT / "docker-compose.market.production.yml").read_text(
        encoding="utf-8"
    )
    start = compose.index("  screener-api:")
    end = compose.index("\n  market-refresh-worker:", start)
    service = compose[start:end]
    assert "SCREENER_API_IMAGE" in service
    assert 'user: "10001:10001"' in service
    assert "market_sidecar:/var/lib/tloomberg" in service
    assert "SCREENER_SIDECAR_PATH" in service
    assert "SCREENER_SOURCE_DB_PATH" not in service
    assert "/data/source/market.sqlite" not in service
    assert "IDENTITY_DATABASE_URL" not in service
    assert "COMMERCE_DATABASE_URL" not in service
    assert "condition: service_completed_successfully" in service
    assert "read_only: true" in service


def test_screener_image_is_non_root_and_does_not_bundle_a_refresh_worker():
    dockerfile = (ROOT / "Dockerfile.screener").read_text(encoding="utf-8")
    assert "USER 10001:10001" in dockerfile
    assert "COPY screener_service" in dockerfile
    assert "COPY site_api" in dockerfile
    assert "site_api.refresh_worker" not in dockerfile
