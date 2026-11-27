"""Integration tests for the extracted AnalyticsService."""
from __future__ import annotations

import io
import sqlite3
from zipfile import ZipFile
from fastapi.testclient import TestClient

import pytest

import server
from server import ApiError, SiteApi
from site_api.analytics_service import AnalyticsService
from screener_service.api import app as screener_app, get_runtime
from screener_service.runtime import ReadOnlyScreenerRuntime
from site_api.dispatch import dispatch_api


@pytest.fixture
def analytics_api(tmp_path):
    source_path = tmp_path / "source.sqlite"
    sidecar_path = tmp_path / "site_data.sqlite"
    api = SiteApi(str(source_path), str(sidecar_path), "", "")
    side = api.sidecar()
    try:
        side.execute(
            "CREATE TABLE IF NOT EXISTS site_holder_history (id INTEGER PRIMARY KEY)"
        )
        side.executemany(
            "INSERT OR REPLACE INTO site_meta(key,value) VALUES (?,?)",
            [
                ("last_refresh", "2026-11-26T18:00:00"),
                ("ownership_resolver_schema", "resolved-v1-source-exclusions"),
                ("sidecar_version", str(server.SIDECAR_VERSION)),
            ],
        )
        side.executemany(
            """
            INSERT INTO site_watch (
                ins_code, symbol, company_name, market,
                resolved_shareholder_count, resolved_ownership_percent_sum,
                data_completeness
            ) VALUES (?,?,?,?,?,?,?)
            """,
            [
                ("1001", "فولاد", "فولاد مبارکه", "TSE", 3, 65.0, "0.95"),
                ("1002", "فملی", "ملی صنایع مس ایران", "IFB", 5, 48.0, "1.0"),
            ],
        )
        side.executemany(
            """
            INSERT INTO site_holder_latest (
                ins_code, symbol, id_scheme, shareholder_id, identity_status,
                latest_date, shares, ownership_percent_calc, shares_change,
                change_type, entry_status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                ("1001", "فولاد", "share_id", 123, "verified_same_source_row",
                 "2026-11-18", 250, 25.0, 20, "increase", "existing"),
                ("1002", "فملی", "share_id", 456, "inferred_exact_match",
                 "2026-11-18", 800, 40.0, -10, "decrease", "existing"),
            ],
        )
        side.executemany(
            """
            INSERT INTO site_shareholder_names (
                id_scheme, shareholder_id, name, shareholder_type
            ) VALUES (?,?,?,?)
            """,
            [
                ("share_id", 123, "سرمایه‌گذار فولاد", "legal"),
                ("share_id", 456, "سرمایه‌گذار مس", "individual"),
            ],
        )
        side.execute(
            "INSERT INTO site_corp_action_counts(ins_code,n) VALUES (?,?)",
            ("1001", 1),
        )
        side.commit()
    finally:
        side.close()
    return api


def test_site_api_composes_analytics_service_and_preserves_legacy_adapter(
    analytics_api,
):
    assert isinstance(analytics_api.analytics_service, AnalyticsService)
    assert analytics_api.analytics_service.sidecar.__self__ is analytics_api
    assert analytics_api.analytics_service.require_ready.__self__ is analytics_api
    payload = analytics_api.screener_query({})
    assert len(payload["rows"]) == 2


def test_screener_and_condition_filters_numeric_values(analytics_api):
    payload = analytics_api.screener_query(
        {
            "logic": "AND",
            "conditions": [
                {"field": "market", "op": "=", "value": "TSE"},
                {"field": "ownership_percent_calculated", "op": ">=", "value": "20"},
                {"field": "has_corporate_action", "op": ">", "value": 0},
            ],
            "sort_by": "ownership_percent_calculated",
            "sort_dir": "DESC",
            "limit": 10,
        }
    )
    assert len(payload["rows"]) == 1
    assert payload["rows"][0] == {
        "symbol": "فولاد",
        "market": "TSE",
        "shareholder_name": "سرمایه‌گذار فولاد",
        "ownership_percent_calculated": 25.0,
        "shares_change": 20,
        "disclosed_ownership_percent_sum": 65.0,
    }


def test_screener_or_filters_and_date_normalization(analytics_api):
    payload = analytics_api.screener_query(
        {
            "logic": "OR",
            "conditions": [
                {"field": "market", "op": "=", "value": "TSE"},
                {"field": "date_gregorian", "op": ">=", "value": "20261118"},
            ],
        }
    )
    assert {row["symbol"] for row in payload["rows"]} == {"فولاد", "فملی"}


def test_invalid_conditions_are_ignored_and_invalid_sort_values_fall_back(
    analytics_api,
):
    payload = analytics_api.screener_query(
        {
            "conditions": [
                {"field": "not_a_field", "op": "=", "value": "x"},
                {"field": "market", "op": "LIKE", "value": "TSE"},
                {"field": "ownership_percent_calculated", "op": ">", "value": "not-number"},
                {"field": "date_gregorian", "op": ">=", "value": "not-a-date"},
            ],
            "sort_by": "symbol; DROP TABLE site_watch",
            "sort_dir": "DESC; DROP TABLE site_watch",
        }
    )
    assert len(payload["rows"]) == 2
    assert payload["rows"][0]["ownership_percent_calculated"] >= (
        payload["rows"][1]["ownership_percent_calculated"]
    )
    assert analytics_api.sidecar().execute(
        "SELECT COUNT(*) FROM site_watch"
    ).fetchone()[0] == 2


def test_screener_limit_is_clamped_and_export_remains_a_valid_xlsx(analytics_api):
    payload = analytics_api.screener_query({"limit": 10000})
    assert len(payload["rows"]) == 2

    exported = analytics_api.screener_export({})
    with ZipFile(io.BytesIO(exported)) as archive:
        assert archive.testzip() is None
        content = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "فولاد" in content
    assert "فملی" in content
    assert "shareholder_id" not in content
    assert "identity_status" not in content


def test_dispatcher_routes_query_and_export_to_analytics_service(analytics_api):
    query, status, content_type = dispatch_api(
        analytics_api,
        "POST",
        "/api/v1/screener/query",
        {},
        {"conditions": [{"field": "market", "op": "=", "value": "TSE"}]},
    )
    assert status == 200
    assert content_type == "json"
    assert [row["symbol"] for row in query["rows"]] == ["فولاد"]

    export, status, content_type = dispatch_api(
        analytics_api, "POST", "/api/v1/screener/export", {}, {}
    )
    assert status == 200
    assert content_type == "xlsx"
    assert isinstance(export, bytes)


def test_analytics_service_enforces_sidecar_readiness(analytics_api):
    side = analytics_api.sidecar()
    try:
        side.execute("DELETE FROM site_meta WHERE key='last_refresh'")
        side.commit()
    finally:
        side.close()

    with pytest.raises(ApiError) as error:
        analytics_api.screener_query({})
    assert error.value.status == 503
    with pytest.raises(ApiError) as export_error:
        analytics_api.screener_export({})
    assert export_error.value.status == 503


@pytest.fixture
def read_only_screener_runtime(analytics_api):
    return ReadOnlyScreenerRuntime(analytics_api.sidecar_path)


def test_independent_screener_http_api_preserves_query_and_excel_contract(
    read_only_screener_runtime,
):
    previous = screener_app.dependency_overrides.get(get_runtime)
    screener_app.dependency_overrides[get_runtime] = lambda: read_only_screener_runtime
    try:
        with TestClient(screener_app) as client:
            assert client.get("/healthz").json() == {
                "status": "ok", "service": "screener"
            }
            ready = client.get("/readyz")
            assert ready.status_code == 200, ready.text
            assert ready.json()["service"] == "screener"

            query = client.post(
                "/api/v1/screener/query",
                json={
                    "logic": "AND",
                    "conditions": [
                        {"field": "market", "op": "=", "value": "TSE"},
                        {"field": "ownership_percent_calculated", "op": ">=", "value": 20},
                    ],
                    "sort_by": "ownership_percent_calculated",
                    "sort_dir": "DESC",
                    "limit": 20,
                },
            )
            assert query.status_code == 200, query.text
            assert query.json()["rows"] == [{
                "symbol": "فولاد",
                "market": "TSE",
                "shareholder_name": "سرمایه‌گذار فولاد",
                "ownership_percent_calculated": 25.0,
                "shares_change": 20,
                "disclosed_ownership_percent_sum": 65.0,
            }]

            exported = client.post(
                "/api/v1/screener/export",
                json={"conditions": [{"field": "market", "op": "=", "value": "TSE"}]},
            )
            assert exported.status_code == 200, exported.text
            assert exported.headers["content-type"].startswith(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
            assert "attachment" in exported.headers["content-disposition"]
            with ZipFile(io.BytesIO(exported.content)) as archive:
                assert archive.testzip() is None
                sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
            assert "فولاد" in sheet
            assert "shareholder_id" not in sheet
            assert "identity_status" not in sheet
    finally:
        if previous is None:
            screener_app.dependency_overrides.pop(get_runtime, None)
        else:
            screener_app.dependency_overrides[get_runtime] = previous


def test_screener_runtime_is_read_only_and_never_opens_market_source(
    read_only_screener_runtime,
):
    conn = read_only_screener_runtime.sidecar()
    try:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        databases = list(conn.execute("PRAGMA database_list"))
        assert [row[1] for row in databases] == ["main"]
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE site_watch SET market='MUTATED' WHERE ins_code='1001'")
    finally:
        conn.close()


def test_screener_readiness_rejects_incomplete_projection(analytics_api):
    side = analytics_api.sidecar()
    try:
        side.execute("DROP TABLE site_corp_action_counts")
        side.commit()
    finally:
        side.close()
    runtime = ReadOnlyScreenerRuntime(analytics_api.sidecar_path)
    ready, reason = runtime.readiness()
    assert ready is False
    assert reason == "screener projection schema is incomplete"


def test_screener_api_bounds_filter_ast_and_validates_query_limit(
    read_only_screener_runtime,
):
    previous = screener_app.dependency_overrides.get(get_runtime)
    screener_app.dependency_overrides[get_runtime] = lambda: read_only_screener_runtime
    try:
        with TestClient(screener_app) as client:
            too_many = client.post(
                "/api/v1/screener/query",
                json={"conditions": [{"field": "market", "op": "=", "value": "TSE"}] * 51},
            )
            assert too_many.status_code == 422

            invalid_limit = client.post(
                "/api/v1/screener/query",
                json={"limit": "not-a-number"},
            )
            assert invalid_limit.status_code == 422
    finally:
        if previous is None:
            screener_app.dependency_overrides.pop(get_runtime, None)
        else:
            screener_app.dependency_overrides[get_runtime] = previous
