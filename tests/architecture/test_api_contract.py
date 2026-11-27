"""Executable compatibility checks for the baseline HTTP API contract."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import server as server_module
from server import ApiError, Handler
from site_api.dispatch import dispatch_api
from site_api.errors import ApiError as ModuleApiError


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = ROOT / "docs" / "architecture" / "api-contract.json"
PARAMS = {"probe": ["query-value"]}
BODY = {"probe": "body-value"}


class RecordingApi:
    """Return the invoked method and retain arguments without using a DB."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args):
            self.calls.append((name, args))
            return {"called": name}
        return record


def load_contract():
    with CONTRACT_PATH.open(encoding="utf-8") as source:
        return json.load(source)


def invoke_current_dispatcher(method, path):
    api = RecordingApi()
    handler = Handler.__new__(Handler)
    handler.api = api
    try:
        result = handler._dispatch(method, path, PARAMS, BODY)
    except ApiError as exc:
        return api, exc
    return api, result


def invoke_extracted_dispatcher(method, path):
    api = RecordingApi()
    try:
        result = dispatch_api(api, method, path, PARAMS, BODY)
    except ModuleApiError as exc:
        return api, exc
    return api, result


@pytest.fixture(scope="module")
def contract():
    return load_contract()


def test_contract_manifest_has_unique_method_and_path_pairs(contract):
    routes = contract["routes"]
    keys = [(item["method"], item["path"]) for item in routes]
    assert contract["contract_version"] == 1
    assert contract["base_path"] == "/api/v1"
    assert contract["baseline_commit"] == "ff062cf18fa4953ddd9bebeb9cbbb776741a8258"
    assert len(routes) == 27
    assert len(keys) == len(set(keys))


@pytest.mark.parametrize("route", load_contract()["routes"],
                         ids=lambda route: f'{route["method"]} {route["path"]}')
def test_manifest_route_matches_current_dispatcher(route):
    api, result = invoke_current_dispatcher(route["method"], route["test_path"])

    if route["status"] != 200:
        assert isinstance(result, ApiError)
        assert result.status == route["status"]
        assert str(result)
        assert api.calls == []
        return

    assert not isinstance(result, ApiError)
    payload, status, content_type = result
    assert status == route["status"]
    assert content_type == route["response"]
    assert payload == {"called": route["handler"]}
    assert len(api.calls) == 1
    called_name, args = api.calls[0]
    assert called_name == route["handler"]

    expected = {
        "none": [],
        "params": [PARAMS],
        "body": [BODY],
        "symbol": ["TEST_SYMBOL"],
        "symbol_params": ["TEST_SYMBOL", PARAMS],
        "investor_params": [route["test_path"].split("/")[4], PARAMS],
    }[route["argument"]]
    assert args == tuple(expected)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("TRACE", "/api/v1/market/overview"),
        ("POST", "/api/v1/market/overview"),
        ("GET", "/api/v1/does-not-exist"),
        ("DELETE", "/api/v1/screener/query"),
    ],
)
def test_unknown_method_or_path_remains_a_404(method, path):
    api, result = invoke_current_dispatcher(method, path)
    assert isinstance(result, ApiError)
    assert result.status == 404
    assert api.calls == []


def test_screener_export_keeps_xlsx_content_type():
    api, result = invoke_current_dispatcher("POST", "/api/v1/screener/export")
    assert not isinstance(result, ApiError)
    assert result[1:] == (200, "xlsx")
    assert api.calls == [("screener_export", (BODY,))]


def test_admin_mutation_placeholders_remain_unimplemented():
    for path in (
        "/api/v1/admin/ingestion/trigger",
        "/api/v1/admin/identity/resolve",
    ):
        api, result = invoke_current_dispatcher("POST", path)
        assert isinstance(result, ApiError)
        assert result.status == 501
        assert api.calls == []



@pytest.mark.parametrize("route", load_contract()["routes"],
                         ids=lambda route: f'{route["method"]} {route["path"]}')
def test_manifest_route_matches_extracted_dispatcher(route):
    api, result = invoke_extracted_dispatcher(route["method"], route["test_path"])

    if route["status"] != 200:
        assert isinstance(result, ModuleApiError)
        assert result.status == route["status"]
        assert str(result)
        assert api.calls == []
        return

    assert not isinstance(result, ModuleApiError)
    payload, status, content_type = result
    assert status == route["status"]
    assert content_type == route["response"]
    assert payload == {"called": route["handler"]}
    assert len(api.calls) == 1
    called_name, args = api.calls[0]
    assert called_name == route["handler"]

    expected = {
        "none": [],
        "params": [PARAMS],
        "body": [BODY],
        "symbol": ["TEST_SYMBOL"],
        "symbol_params": ["TEST_SYMBOL", PARAMS],
        "investor_params": [route["test_path"].split("/")[4], PARAMS],
    }[route["argument"]]
    assert args == tuple(expected)


def test_handler_keeps_the_legacy_dispatch_entry_point(monkeypatch):
    calls = []

    def fake_dispatch(api, method, path, params, body):
        calls.append((api, method, path, params, body))
        return {"delegated": True}, 202, "json"

    monkeypatch.setattr(server_module, "dispatch_api", fake_dispatch)
    api = RecordingApi()
    handler = Handler.__new__(Handler)
    handler.api = api

    result = handler._dispatch("GET", "/api/v1/market/overview", PARAMS, BODY)

    assert result == ({"delegated": True}, 202, "json")
    assert calls == [(api, "GET", "/api/v1/market/overview", PARAMS, BODY)]


def test_server_and_dispatcher_share_the_same_api_error_type():
    assert ApiError is ModuleApiError
