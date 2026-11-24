import io

import pytest
"""Privacy contract tests for public market-facing API projections."""
import server


def site_api():
    # The wrappers under test are pure projections; no database is opened.
    return object.__new__(server.SiteApi)


def test_public_market_watch_removes_ownership_and_raw_holder_fields():
    api = site_api()
    captured = {}

    def market_watch(params):
        captured.update(params)
        return {
            "total": 1,
            "rows": [{
                "symbol": "نماد",
                "company_name": "شرکت نمونه",
                "market": "TSE",
                "total_company_shares": 10_000,
                "disclosed_shareholder_count": 4,
                "resolved_shareholder_count": 3,
                "resolved_ownership_percent_sum": 12.5,
                "raw_ownership_percent_sum": 16.2,
                "latest_price": 100,
                "latest_price_date": "20261120",
                "market_value": 1_000_000,
                "latest_date_jalali": "1405/08/29",
                "shareholder_names": ["نام محرمانه"],
                "shareholder_ids": ["123"],
            }],
        }

    api.market_watch = market_watch
    result = api.market_watch_public({
        "sort_by": "disclosed_pct", "sort_dir": "DESC",
    })

    assert captured["sort_by"] == ["market_value"]
    row = result["rows"][0]
    assert row["symbol"] == "نماد"
    assert row["market_value"] == 1_000_000
    assert not ({"total_company_shares", "disclosed_shareholder_count",
                 "resolved_ownership_percent_sum", "raw_ownership_percent_sum",
                 "shareholder_names", "shareholder_ids"} & set(row))


def test_public_market_overview_does_not_leak_collection_or_holder_summaries():
    api = site_api()
    api.market_overview = lambda: {
        "summary": {
            "total_companies": 500, "tse_companies": 300,
            "ifb_companies": 190, "unknown_market_companies": 10,
            "total_holdings_rows": 99_999, "earliest_date_jalali": "1400/01/01",
        },
        "top_ownership_changes": [{"shareholder_name": "نام محرمانه"}],
        "recently_observed_shareholders": [{"shareholder_id": "123"}],
    }

    result = api.market_overview_public()
    assert result == {"summary": {
        "total_companies": 500, "tse_companies": 300,
        "ifb_companies": 190, "unknown_market_companies": 10,
    }}


def test_public_stock_detail_contains_no_company_history_or_identifiers():
    api = site_api()
    api.stock_detail = lambda symbol: {
        "instrument": {
            "symbol": symbol, "company_name": "شرکت نمونه", "market": "TSE",
            "ins_code": "secret-code", "cisin": "secret-cisin",
            "current_issued_shares": 10_000,
        },
        "latest_snapshot": {
            "latest_price": 100, "latest_price_date": "20261120",
            "market_value": 1_000_000, "data_completeness": 0.9,
            "raw_ownership_percent_sum": 105.2, "total_company_shares": 10_000,
        },
        "company_history": [{"raw_shareholder_count": 22}],
        "available_dates": ["1405/08/29"],
    }

    result = api.stock_detail_public("نماد")
    assert result["instrument"] == {
        "symbol": "نماد", "company_name": "شرکت نمونه", "market": "TSE",
    }
    assert result["latest_snapshot"]["latest_price"] == 100
    assert "company_history" not in result
    assert "available_dates" not in result
    assert "ins_code" not in result["instrument"]
    assert "cisin" not in result["instrument"]


def test_public_corporate_actions_are_chart_markers_only():
    api = site_api()
    api.stock_corporate_actions = lambda symbol: {
        "corporate_actions": [{
            "event_date_gregorian": "2026-02-01",
            "event_date_jalali": "1404/11/12",
            "action_type": "افزایش سرمایه",
            "old_total_shares": 1_000,
            "new_total_shares": 2_000,
            "capital_increase_ratio": 2,
            "verification_status": "verified",
        }],
    }

    event = api.stock_corporate_actions_public("نماد")["events"][0]
    assert event["is_capital_increase"] is True
    assert event["date"] == "2026-02-01"
    assert "old_total_shares" not in event
    assert "new_total_shares" not in event
    assert "capital_increase_ratio" not in event


def test_request_body_must_be_a_json_object():
    class Request:
        def __init__(self, body):
            self.headers = {"Content-Length": str(len(body))}
            self.rfile = io.BytesIO(body)

    with pytest.raises(server.ApiError) as exc:
        server._read_json_body(Request(b'["unexpected","array"]'))
    assert exc.value.status == 400

    assert server._read_json_body(Request(b'{"safe":true}')) == {"safe": True}
    assert server._read_json_body(Request(b'')) == {}


class _NoAccountRoutes:
    def handles(self, path):
        return False


class _NoAccountApi:
    accounts = _NoAccountRoutes()


class _RecordingHandler(server.Handler):
    def __init__(self, path, method="GET", body=b"{}"):
        self.path = path
        self.headers = {"Content-Length": str(len(body))}
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.api = _NoAccountApi()
        self.status = None
        self.headers_out = {}

    def send_response(self, status, message=None):
        self.status = status

    def send_header(self, name, value):
        self.headers_out[name.lower()] = value

    def end_headers(self):
        pass


@pytest.mark.parametrize("path,method", [
    ("/api/v1/shareholders/search", "GET"),
    ("/api/v1/shareholders/observation-history", "GET"),
    ("/api/v1/portfolios/investors", "GET"),
    ("/api/v1/portfolios/123/history", "GET"),
    ("/api/v1/stocks/ABC/shareholders", "GET"),
    ("/api/v1/stocks/ABC/ownership-series", "GET"),
    ("/api/v1/stocks/ABC/data-quality", "GET"),
    ("/api/v1/screener/query", "POST"),
    ("/api/v1/screener/export", "POST"),
])
def test_legacy_raw_data_routes_are_blocked_at_http_layer(path, method):
    handler = _RecordingHandler(path, method)
    handler._route(method)
    assert handler.status == 410


def test_database_files_are_not_served_as_static_assets():
    handler = _RecordingHandler("/site_accounts.sqlite")
    handler._route("GET")
    assert handler.status == 404


def test_admin_api_requires_authenticated_superadmin():
    handler = _RecordingHandler("/api/v1/admin/overview")
    handler._route("GET")
    assert handler.status == 401
