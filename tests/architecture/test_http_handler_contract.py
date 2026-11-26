"""End-to-end HTTP compatibility tests for the extracted API dispatcher."""
from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from server import Handler


class HttpRecordingApi:
    """A database-free API double used through the real HTTP handler."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(*args):
            self.calls.append((name, args))
            if name == "screener_export":
                return b"PK\\x03\\x04fake-xlsx"
            return {"called": name, "args": args}

        return call


@pytest.fixture
def http_api():
    api = HttpRecordingApi()

    class TestHandler(Handler):
        pass

    TestHandler.api = api
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield base_url, api
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def read_http_error(request):
    with pytest.raises(HTTPError) as captured:
        urlopen(request, timeout=3)
    body = captured.value.read().decode("utf-8")
    return captured.value.code, json.loads(body)


def test_real_http_get_routes_through_handler_adapter_and_preserves_query(http_api):
    base_url, api = http_api
    with urlopen(
        f"{base_url}/api/v1/market/watch?limit=7&q=alpha", timeout=3
    ) as response:
        payload = json.loads(response.read().decode("utf-8"))

    assert response.status == 200
    assert response.headers["Content-Type"].startswith("application/json")
    assert payload == {
        "called": "market_watch",
        "args": [{"limit": ["7"], "q": ["alpha"]}],
    }
    assert api.calls == [
        ("market_watch", ({"limit": ["7"], "q": ["alpha"]},))
    ]


def test_real_http_get_decodes_path_parameter_before_dispatch(http_api):
    base_url, api = http_api
    with urlopen(f"{base_url}/api/v1/stocks/TEST%20SYMBOL", timeout=3) as response:
        payload = json.loads(response.read().decode("utf-8"))

    assert response.status == 200
    assert payload == {"called": "stock_detail", "args": ["TEST SYMBOL"]}
    assert api.calls == [("stock_detail", ("TEST SYMBOL",))]


def test_real_http_post_passes_json_body_to_extracted_dispatcher(http_api):
    base_url, api = http_api
    body = {"conditions": [{"field": "ownership", "op": ">", "value": 1}]}
    request = Request(
        f"{base_url}/api/v1/screener/query",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=3) as response:
        payload = json.loads(response.read().decode("utf-8"))

    assert response.status == 200
    assert payload == {"called": "screener_query", "args": [body]}
    assert api.calls == [("screener_query", (body,))]


def test_real_http_xlsx_export_keeps_binary_response_contract(http_api):
    base_url, api = http_api
    body = {"conditions": []}
    request = Request(
        f"{base_url}/api/v1/screener/export",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=3) as response:
        payload = response.read()

    assert response.status == 200
    assert response.headers["Content-Type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert response.headers["Content-Disposition"].endswith(
        'filename="tloomberg_shareholder_screener.xlsx"'
    )
    assert payload == b"PK\\x03\\x04fake-xlsx"
    assert api.calls == [("screener_export", (body,))]


def test_real_http_api_error_is_serialized_with_original_status_and_detail(http_api):
    base_url, _api = http_api
    request = Request(
        f"{base_url}/api/v1/admin/ingestion/trigger",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    status, payload = read_http_error(request)

    assert status == 501
    assert "وصل نشده" in payload["detail"]


def test_real_http_unknown_route_remains_json_404(http_api):
    base_url, _api = http_api
    status, payload = read_http_error(
        Request(f"{base_url}/api/v1/not-a-real-route", method="GET")
    )

    assert status == 404
    assert "مسیر API یافت نشد" in payload["detail"]


def test_real_http_rejects_invalid_json_without_calling_api(http_api):
    base_url, api = http_api
    status, payload = read_http_error(
        Request(
            f"{base_url}/api/v1/screener/query",
            data=b"{not-json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
    )

    assert status == 400
    assert "JSON نامعتبر" in payload["detail"]
    assert api.calls == []
