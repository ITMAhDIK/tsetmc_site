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


def test_dispatch_proxies_all_portfolio_read_routes_when_configured(monkeypatch):
    import site_api.dispatch as dispatch

    monkeypatch.setenv("PORTFOLIO_SERVICE_URL", "http://portfolio-api:8000")
    monkeypatch.setenv("MARKET_PUBLIC_READ_ONLY", "true")
    calls = []
    monkeypatch.setattr(
        dispatch, "proxy_portfolio_request",
        lambda path, params: (
            calls.append((path, params)) or ({"service": "portfolio"}, 200, "json")
        ),
    )
    api = RecordingApi()
    requests = [
        ("/api/v1/portfolios/investors", {"q": ["سرمایه‌گذار"]}),
        ("/api/v1/portfolios/share_id:123", {"as_of_date": ["2026-11-18"]}),
        ("/api/v1/portfolios/share_id:123/history", {"ins_code": ["1001"]}),
    ]
    for path, params in requests:
        assert dispatch_api(api, "GET", path, params, {}) == (
            {"service": "portfolio"}, 200, "json"
        )
    assert calls == requests
    assert api.calls == []


def test_dispatch_falls_back_only_when_portfolio_service_url_is_unset(monkeypatch):
    import site_api.dispatch as dispatch

    monkeypatch.delenv("PORTFOLIO_SERVICE_URL", raising=False)
    monkeypatch.setenv("MARKET_PUBLIC_READ_ONLY", "false")
    monkeypatch.setattr(dispatch, "proxy_portfolio_request", lambda *_args: None)
    api = RecordingApi()
    result = dispatch_api(api, "GET", "/api/v1/portfolios/share_id:123", {}, {})
    assert result == ({"legacy": "portfolio_detail"}, 200, "json")
    assert api.calls == [("portfolio_detail", ("share_id:123", {}))]


def test_configured_portfolio_outage_fails_closed_without_legacy_fallback(monkeypatch):
    import site_api.dispatch as dispatch

    monkeypatch.setenv("PORTFOLIO_SERVICE_URL", "http://portfolio-api:8000")
    monkeypatch.setattr(
        dispatch, "proxy_portfolio_request",
        lambda *_args: (_ for _ in ()).throw(ApiError(503, "portfolio offline")),
    )
    api = RecordingApi()
    try:
        dispatch_api(api, "GET", "/api/v1/portfolios/investors", {}, {})
    except ApiError as exc:
        assert exc.status == 503
    else:
        raise AssertionError("configured Portfolio outage must fail closed")
    assert api.calls == []


def test_portfolio_client_preserves_composite_id_and_query_parameters(monkeypatch):
    import site_api.portfolio_client as client_module

    monkeypatch.setenv("PORTFOLIO_SERVICE_URL", "http://portfolio-api:8000")
    monkeypatch.setenv("PORTFOLIO_HTTP_TIMEOUT_SECONDS", "45")
    captured = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b'{"investor":{"id":"share_id:123"}}'

    def fake_urlopen(request, timeout):
        captured.append((request.full_url, timeout))
        return Response()

    monkeypatch.setattr(client_module, "urlopen", fake_urlopen)
    result = client_module.proxy_portfolio_request(
        "/api/v1/portfolios/share_id:123/history",
        {"ins_code": ["1001"], "as_of_date": ["2026-11-18"]},
    )
    assert result == ({"investor": {"id": "share_id:123"}}, 200, "json")
    url, timeout = captured[0]
    assert "share_id:123/history" in url
    assert "ins_code=1001" in url
    assert "as_of_date=2026-11-18" in url
    assert timeout == 45.0


def test_portfolio_client_fails_closed_when_service_is_unavailable(monkeypatch):
    from urllib.error import URLError
    import site_api.portfolio_client as client_module

    monkeypatch.setenv("PORTFOLIO_SERVICE_URL", "http://portfolio-api:8000")
    monkeypatch.setattr(
        client_module, "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("offline")),
    )
    try:
        client_module.proxy_portfolio_request("/api/v1/portfolios/investors", {})
    except ApiError as exc:
        assert exc.status == 503
    else:
        raise AssertionError("configured Portfolio outage must fail closed")


def test_portfolio_client_rejects_unbounded_timeout(monkeypatch):
    import site_api.portfolio_client as client_module

    monkeypatch.setenv("PORTFOLIO_SERVICE_URL", "http://portfolio-api:8000")
    monkeypatch.setenv("PORTFOLIO_HTTP_TIMEOUT_SECONDS", "999")
    try:
        client_module.proxy_portfolio_request("/api/v1/portfolios/investors", {})
    except ApiError as exc:
        assert exc.status == 503
    else:
        raise AssertionError("unbounded Portfolio timeout must be rejected")


def test_production_edge_routes_portfolios_to_dedicated_service():
    caddy = (ROOT / "Caddyfile.market").read_text(encoding="utf-8")
    assert "@portfolio path" in caddy
    assert "/api/v1/portfolios/investors" in caddy
    assert "/api/v1/portfolios/*" in caddy
    assert "reverse_proxy portfolio-api:8000" in caddy
    assert "reverse_proxy market-api:8000" in caddy


def test_portfolio_compose_service_is_read_only_and_shares_sidecar_for_wal():
    compose = (ROOT / "docker-compose.market.production.yml").read_text(
        encoding="utf-8"
    )
    start = compose.index("  portfolio-api:")
    end = compose.index("\n  market-refresh-worker:", start)
    service = compose[start:end]
    assert "PORTFOLIO_API_IMAGE" in service
    assert 'user: "10001:10001"' in service
    assert ":/data/source/market.sqlite:ro" in service
    assert "market_sidecar:/var/lib/tloomberg" in service
    assert "PORTFOLIO_SOURCE_DB_PATH" in service
    assert "PORTFOLIO_SIDECAR_PATH" in service
    assert "IDENTITY_DATABASE_URL" not in service
    assert "COMMERCE_DATABASE_URL" not in service
    assert "condition: service_completed_successfully" in service


def test_portfolio_image_is_non_root_and_does_not_bundle_refresh_worker():
    dockerfile = (ROOT / "Dockerfile.portfolio").read_text(encoding="utf-8")
    assert "USER 10001:10001" in dockerfile
    assert "COPY portfolio_service" in dockerfile
    assert "COPY site_api" in dockerfile
    assert "site_api.refresh_worker" not in dockerfile
