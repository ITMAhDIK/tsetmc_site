import io
import pytest
import server


class _NoAccountRoutes:
    def handles(self, path):
        return False


class _NoAccountApi:
    accounts = _NoAccountRoutes()


class _RecordingHandler(server.Handler):
    def __init__(self, path, method="GET", body=b"{}"):
        self.path = path
        self.headers = {"Content-Length": str(len(body)), "Cookie": ""}
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

    def _dispatch(self, method, path, params, body, user=None):
        return {"path": path, "public": True}, 200, "json"


@pytest.mark.parametrize("path,method", [
    ("/api/v1/shareholders/search", "GET"),
    ("/api/v1/shareholders/observation-history", "GET"),
    ("/api/v1/portfolios/investors", "GET"),
    ("/api/v1/portfolios/123/history", "GET"),
    ("/api/v1/stocks/ABC/shareholders", "GET"),
    ("/api/v1/stocks/ABC/ownership-series", "GET"),
    ("/api/v1/stocks/ABC/ownership-series?shareholder_id=123&id_scheme=holder_id", "GET"),
    ("/api/v1/stocks/ABC/data-quality", "GET"),
    ("/api/v1/screener/query", "POST"),
    ("/api/v1/screener/export", "POST"),
])
def test_market_and_ownership_source_routes_are_public(path, method):
    handler = _RecordingHandler(path, method)
    handler._route(method)
    assert handler.status == 200


def test_database_and_workbook_files_are_not_served_as_static_assets():
    for path in ("/site_accounts.sqlite", "/site_data.sqlite", "/tsetmc_holdings.sqlite", "/secret.xlsx"):
        handler = _RecordingHandler(path)
        handler._route("GET")
        assert handler.status == 404


def test_admin_api_requires_authenticated_superadmin():
    handler = _RecordingHandler("/api/v1/admin/overview")
    handler._route("GET")
    assert handler.status == 401


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
