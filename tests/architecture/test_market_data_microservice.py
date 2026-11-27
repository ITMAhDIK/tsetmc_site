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


def test_dispatch_uses_market_data_service_client_when_configured(monkeypatch):
    import site_api.dispatch as dispatch
    calls = []
    monkeypatch.setenv("MARKET_DATA_SERVICE_URL", "http://market-data-api:8000")
    monkeypatch.setattr(
        dispatch, "proxy_market_data_request",
        lambda path, params: (calls.append((path, params)) or
                              ({"service": "market-data"}, 200, "json")),
    )
    monkeypatch.setenv("MARKET_PUBLIC_READ_ONLY", "true")
    api = RecordingApi()

    result = dispatch_api(
        api, "GET", "/api/v1/stocks/search", {"q": ["فولاد"], "limit": ["8"]}, {}
    )
    assert result == ({"service": "market-data"}, 200, "json")
    assert calls == [(
        "/api/v1/stocks/search", {"q": ["فولاد"], "limit": ["8"]}
    )]
    assert api.calls == []


def test_dispatch_keeps_legacy_fallback_when_market_data_url_is_not_configured(monkeypatch):
    import site_api.dispatch as dispatch
    monkeypatch.delenv("MARKET_DATA_SERVICE_URL", raising=False)
    monkeypatch.setenv("MARKET_PUBLIC_READ_ONLY", "false")
    monkeypatch.setattr(dispatch, "proxy_market_data_request", lambda *_args: None)
    api = RecordingApi()
    result = dispatch_api(api, "GET", "/api/v1/stocks/search", {"q": ["x"]}, {})
    assert result == ({"legacy": "stocks_search"}, 200, "json")
    assert api.calls == [("stocks_search", ({"q": ["x"]},))]


def test_configured_market_data_outage_does_not_fall_back_to_legacy(monkeypatch):
    import site_api.dispatch as dispatch
    monkeypatch.setenv("MARKET_DATA_SERVICE_URL", "http://market-data-api:8000")
    monkeypatch.setattr(
        dispatch, "proxy_market_data_request",
        lambda *_args: (_ for _ in ()).throw(ApiError(503, "market data offline")),
    )
    api = RecordingApi()
    try:
        dispatch_api(api, "GET", "/api/v1/stocks/search", {}, {})
    except ApiError as exc:
        assert exc.status == 503
    else:
        raise AssertionError("a configured Market Data outage must fail closed")
    assert api.calls == []


def test_production_edge_routes_only_instrument_and_price_paths_to_new_service():
    caddy = (ROOT / "Caddyfile.market").read_text(encoding="utf-8")
    assert "@marketData path" in caddy
    assert "/api/v1/market/instruments" in caddy
    assert "/api/v1/stocks/search" in caddy
    assert "/api/v1/stocks/*/prices" in caddy
    assert "reverse_proxy market-data-api:8000" in caddy
    assert "reverse_proxy market-api:8000" in caddy


def test_market_data_container_mounts_source_readonly_and_sidecar_for_wal_only():
    compose = (ROOT / "docker-compose.market.production.yml").read_text(encoding="utf-8")
    start = compose.index("  market-data-api:")
    end = compose.index("\n  market-refresh-worker:", start)
    service = compose[start:end]
    assert "MARKET_DATA_API_IMAGE" in service
    assert 'user: "10001:10001"' in service
    assert ":/data/source/market.sqlite:ro" in service
    assert "market_sidecar:/var/lib/tloomberg" in service
    assert "market_sidecar:/var/lib/tloomberg:ro" not in service
    assert "condition: service_completed_successfully" in service
    assert "MARKET_DATA_SOURCE_DB_PATH" in service
    assert "MARKET_DATA_SIDECAR_PATH" in service
    assert "IDENTITY_DATABASE_URL" not in service
    assert "COMMERCE_DATABASE_URL" not in service


def test_market_data_image_is_non_root_and_does_not_bundle_legacy_site_server():
    dockerfile = (ROOT / "Dockerfile.market-data").read_text(encoding="utf-8")
    assert "USER 10004:10004" in dockerfile
    assert "market_data_service" in dockerfile
    assert "server.py" not in dockerfile



def test_market_data_http_client_url_encodes_persian_paths_and_query(monkeypatch):
    import site_api.market_data_client as client_module

    monkeypatch.setenv("MARKET_DATA_SERVICE_URL", "http://market-data-api:8000")
    captured = []

    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def read(self, _limit):
            return b'{"prices":[]}'

    def fake_urlopen(request, timeout):
        captured.append((request.full_url, timeout))
        return Response()

    monkeypatch.setattr(client_module, "urlopen", fake_urlopen)
    result = client_module.proxy_market_data_request(
        "/api/v1/stocks/فولاد/prices",
        {"start_date": ["2026-11-17"], "limit": ["5"]},
    )
    assert result == ({"prices": []}, 200, "json")
    url, timeout = captured[0]
    assert "%D9%81%D9%88%D9%84%D8%A7%D8%AF" in url.upper()
    assert "start_date=2026-11-17" in url
    assert "limit=5" in url
    assert timeout == 3.0


def test_market_data_http_client_fails_closed_when_service_is_unavailable(monkeypatch):
    from urllib.error import URLError
    import site_api.market_data_client as client_module

    monkeypatch.setenv("MARKET_DATA_SERVICE_URL", "http://market-data-api:8000")
    monkeypatch.setattr(
        client_module, "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("offline")),
    )
    try:
        client_module.proxy_market_data_request("/api/v1/stocks/search", {})
    except ApiError as exc:
        assert exc.status == 503
    else:
        raise AssertionError("a configured Market Data outage must fail closed")
