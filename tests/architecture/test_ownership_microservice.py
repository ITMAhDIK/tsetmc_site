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


def test_dispatch_proxies_ownership_routes_when_configured(monkeypatch):
    import site_api.dispatch as dispatch

    monkeypatch.setenv("OWNERSHIP_SERVICE_URL", "http://ownership-api:8000")
    monkeypatch.setenv("MARKET_PUBLIC_READ_ONLY", "true")
    calls = []
    monkeypatch.setattr(
        dispatch,
        "proxy_ownership_request",
        lambda path, params: (
            calls.append((path, params)) or ({"service": "ownership"}, 200, "json")
        ),
    )
    api = RecordingApi()
    result = dispatch_api(
        api, "GET", "/api/v1/stocks/فولاد/shareholders",
        {"date_jalali": ["1405-08-27"]}, {}
    )
    assert result == ({"service": "ownership"}, 200, "json")
    assert calls == [(
        "/api/v1/stocks/فولاد/shareholders",
        {"date_jalali": ["1405-08-27"]},
    )]
    assert api.calls == []


def test_dispatch_falls_back_only_when_ownership_url_is_not_configured(monkeypatch):
    import site_api.dispatch as dispatch

    monkeypatch.delenv("OWNERSHIP_SERVICE_URL", raising=False)
    monkeypatch.setenv("MARKET_PUBLIC_READ_ONLY", "false")
    monkeypatch.setattr(dispatch, "proxy_ownership_request", lambda *_args: None)
    api = RecordingApi()
    result = dispatch_api(
        api, "GET", "/api/v1/shareholders/search", {"q": ["share_id:12"]}, {}
    )
    assert result == ({"legacy": "shareholders_search"}, 200, "json")
    assert api.calls == [("shareholders_search", ({"q": ["share_id:12"]},))]


def test_configured_ownership_outage_fails_closed(monkeypatch):
    import site_api.dispatch as dispatch

    monkeypatch.setenv("OWNERSHIP_SERVICE_URL", "http://ownership-api:8000")
    monkeypatch.setattr(
        dispatch,
        "proxy_ownership_request",
        lambda *_args: (_ for _ in ()).throw(ApiError(503, "ownership offline")),
    )
    api = RecordingApi()
    try:
        dispatch_api(api, "GET", "/api/v1/shareholders/search", {}, {})
    except ApiError as exc:
        assert exc.status == 503
    else:
        raise AssertionError("configured Ownership outage must fail closed")
    assert api.calls == []


def test_ownership_client_url_encodes_persian_path_and_preserves_query(monkeypatch):
    import site_api.ownership_client as client_module

    monkeypatch.setenv("OWNERSHIP_SERVICE_URL", "http://ownership-api:8000")
    captured = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b'{"groups":[]}'

    def fake_urlopen(request, timeout):
        captured.append((request.full_url, timeout))
        return Response()

    monkeypatch.setattr(client_module, "urlopen", fake_urlopen)
    result = client_module.proxy_ownership_request(
        "/api/v1/stocks/فولاد/shareholders",
        {"date_jalali": ["1405-08-27"], "limit": ["50"]},
    )
    assert result == ({"groups": []}, 200, "json")
    url, timeout = captured[0]
    assert "%D9%81%D9%88%D9%84%D8%A7%D8%AF" in url.upper()
    assert "date_jalali=1405-08-27" in url
    assert "limit=50" in url
    assert timeout == 3.0


def test_ownership_client_fails_closed_when_service_is_unavailable(monkeypatch):
    from urllib.error import URLError
    import site_api.ownership_client as client_module

    monkeypatch.setenv("OWNERSHIP_SERVICE_URL", "http://ownership-api:8000")
    monkeypatch.setattr(
        client_module, "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("offline")),
    )
    try:
        client_module.proxy_ownership_request("/api/v1/shareholders/search", {})
    except ApiError as exc:
        assert exc.status == 503
    else:
        raise AssertionError("configured Ownership outage must fail closed")


def test_production_edge_routes_ownership_paths_to_dedicated_service():
    caddy = (ROOT / "Caddyfile.market").read_text(encoding="utf-8")
    assert "@ownership path" in caddy
    assert "/api/v1/stocks/*/shareholders" in caddy
    assert "/api/v1/stocks/*/ownership-series" in caddy
    assert "/api/v1/shareholders/search" in caddy
    assert "/api/v1/shareholders/observation-history" in caddy
    assert "reverse_proxy ownership-api:8000" in caddy
    assert "reverse_proxy market-api:8000" in caddy


def test_ownership_compose_service_is_read_only_and_shares_sidecar_for_wal():
    compose = (ROOT / "docker-compose.market.production.yml").read_text(
        encoding="utf-8"
    )
    start = compose.index("  ownership-api:")
    end = compose.index("\n  market-refresh-worker:", start)
    service = compose[start:end]
    assert "OWNERSHIP_API_IMAGE" in service
    assert 'user: "10001:10001"' in service
    assert ":/data/source/market.sqlite:ro" in service
    assert "market_sidecar:/var/lib/tloomberg" in service
    assert "OWNERSHIP_SOURCE_DB_PATH" in service
    assert "OWNERSHIP_SIDECAR_PATH" in service
    assert "IDENTITY_DATABASE_URL" not in service
    assert "COMMERCE_DATABASE_URL" not in service
    assert "condition: service_completed_successfully" in service


def test_ownership_docker_image_is_non_root_and_uses_current_domain_code():
    dockerfile = (ROOT / "Dockerfile.ownership").read_text(encoding="utf-8")
    assert "USER 10001:10001" in dockerfile
    assert "COPY server.py sector_affinity.py" in dockerfile
    assert "COPY site_api" in dockerfile
    assert "COPY ownership_service" in dockerfile
    assert "site_api.refresh_worker" not in dockerfile
