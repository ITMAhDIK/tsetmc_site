"""Integration tests for the extracted AnalyticsService."""
from __future__ import annotations

import io
from zipfile import ZipFile

import pytest

from server import ApiError, SiteApi
from site_api.analytics_service import AnalyticsService
from site_api.dispatch import dispatch_api


@pytest.fixture
def analytics_api(tmp_path):
    source_path = tmp_path / "source.sqlite"
    sidecar_path = tmp_path / "site_data.sqlite"
    api = SiteApi(str(source_path), str(sidecar_path), "", "")
    side = api.sidecar()
    try:
        side.executemany(
            "INSERT OR REPLACE INTO site_meta(key,value) VALUES (?,?)",
            [
                ("last_refresh", "2026-11-26T18:00:00"),
                ("ownership_resolver_schema", "resolved-v1-source-exclusions"),
                ("sidecar_version", "10"),
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
