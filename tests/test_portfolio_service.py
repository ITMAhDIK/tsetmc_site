"""Integration tests for the extracted PortfolioService."""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

import server
from server import ApiError, SiteApi
from site_api.dispatch import dispatch_api
from site_api.portfolio_service import PortfolioService
from portfolio_service.api import app as portfolio_app, get_runtime
from portfolio_service.runtime import ReadOnlyPortfolioApi


@pytest.fixture
def portfolio_api(tmp_path):
    source_path = tmp_path / "source.sqlite"
    sidecar_path = tmp_path / "site_data.sqlite"
    site_dir = tmp_path / "docs"
    site_dir.mkdir()

    source = sqlite3.connect(source_path)
    source.executescript(
        """
        CREATE TABLE companies (
            ins_code TEXT PRIMARY KEY, cisin TEXT, total_shares_current INTEGER
        );
        CREATE TABLE company_daily (
            ins_code TEXT, date TEXT, total_company_shares INTEGER,
            disclosed_shareholder_count INTEGER,
            disclosed_ownership_percent_sum REAL
        );
        CREATE TABLE shareholders (
            id_scheme TEXT, shareholder_id INTEGER, name TEXT,
            name_normalized TEXT, shareholder_type TEXT,
            identity_status TEXT, first_seen_date TEXT, last_seen_date TEXT,
            raw_source_ids TEXT,
            PRIMARY KEY (id_scheme, shareholder_id)
        );
        CREATE TABLE shareholder_id_xref (
            ins_code TEXT, source_scheme TEXT, source_id INTEGER,
            canonical_scheme TEXT, canonical_id INTEGER, status TEXT
        );
        CREATE TABLE shareholder_holdings (
            ins_code TEXT, date TEXT, id_scheme TEXT, shareholder_id INTEGER,
            symbol TEXT, shares INTEGER, ownership_percent REAL,
            ownership_percent_calc REAL, shares_change INTEGER,
            market_value INTEGER, price INTEGER, price_date TEXT,
            valuation_status TEXT, identity_status TEXT, entry_status TEXT,
            change_type TEXT, jalali_date TEXT, total_company_shares INTEGER,
            previous_date TEXT, previous_shares INTEGER,
            change_calculation_status TEXT, shares_change_percent REAL,
            gap_trading_days INTEGER, corporate_action TEXT,
            corporate_action_date TEXT, corporate_action_ratio REAL,
            source_change_code INTEGER, source_change_amount REAL,
            anomaly_flags TEXT, data_available INTEGER, note TEXT,
            price_field TEXT, source_endpoint TEXT, prev_close_price REAL,
            prev_close_date TEXT
        );
        CREATE TABLE trading_days (
            ins_code TEXT, date TEXT, p_first REAL, p_max REAL,
            p_min REAL, p_closing REAL, volume INTEGER
        );
        """
    )
    source.execute(
        "INSERT INTO companies VALUES (?,?,?)",
        ("1001", "IR1234567890", 1000),
    )
    source.execute(
        "INSERT INTO company_daily VALUES (?,?,?,?,?)",
        ("1001", "2026-11-18", 1000, 1, 25.0),
    )
    source.execute(
        """
        INSERT INTO shareholders VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            "share_id", 123, "سرمایه‌گذار آزمایشی",
            "سرمایه‌گذار آزمایشی", "legal", "source_id",
            "2024-01-01", "2026-11-18", "share_id:123",
        ),
    )
    source.execute(
        """
        INSERT INTO shareholder_holdings (
            ins_code, date, id_scheme, shareholder_id, symbol, shares,
            ownership_percent, ownership_percent_calc, shares_change,
            market_value, price, price_date, valuation_status,
            identity_status, entry_status, change_type, jalali_date,
            total_company_shares, previous_date, previous_shares,
            change_calculation_status, shares_change_percent,
            gap_trading_days, corporate_action, corporate_action_date,
            corporate_action_ratio, source_change_code, source_change_amount,
            anomaly_flags, data_available, note, price_field,
            source_endpoint, prev_close_price, prev_close_date
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "1001", "2026-11-18", "share_id", 123, "فولاد", 250,
            25.0, 25.0, 50, None, None, "2026-11-18", "unavailable",
            "source_id", "existing", "increase", "1405-08-27",
            1000, "2026-11-17", 200, "consecutive_observations", 25.0,
            1, None, None, None, None, None, None, 1, None, None,
            "shareholders_on_date", 2500, "2026-11-17",
        ),
    )
    source.execute(
        "INSERT INTO trading_days VALUES (?,?,?,?,?,?,?)",
        ("1001", "2026-11-18", 2500, 2700, 2400, 2600, 15000),
    )
    source.commit()
    source.close()

    api = SiteApi(str(source_path), str(sidecar_path), str(site_dir), "")
    side = api.sidecar()
    try:
        side.execute(
            """
            CREATE TABLE IF NOT EXISTS site_holder_history (
                id INTEGER PRIMARY KEY,
                id_scheme TEXT NOT NULL, shareholder_id INTEGER NOT NULL,
                ins_code TEXT NOT NULL, symbol TEXT NOT NULL, identity_status TEXT,
                date TEXT NOT NULL, jalali_date TEXT, shares INTEGER,
                ownership_percent_reported REAL, total_company_shares INTEGER,
                price REAL, price_field TEXT, price_date TEXT, valuation_status TEXT,
                market_value REAL, previous_date TEXT, previous_shares INTEGER,
                shares_change INTEGER, shares_change_percent REAL, gap_trading_days INTEGER,
                change_type TEXT, change_calculation_status TEXT, corporate_action TEXT,
                corporate_action_date TEXT, corporate_action_ratio REAL,
                source_change_code INTEGER, source_change_amount REAL, anomaly_flags TEXT,
                data_available INTEGER, note TEXT, entry_status TEXT, source_endpoint TEXT,
                prev_close_price REAL, prev_close_date TEXT, cisin TEXT
            )
            """
        )
        side.executemany(
            "INSERT OR REPLACE INTO site_meta(key,value) VALUES (?,?)",
            [
                ("last_refresh", "2026-11-26T18:00:00"),
                ("ownership_resolver_schema", "resolved-v1-source-exclusions"),
                ("sidecar_version", str(server.SIDECAR_VERSION)),
                ("earliest_date", "2026-11-18"),
                ("latest_date", "2026-11-18"),
                ("holdings_count", "1"),
                ("last_fetch_ts", "2026-11-26T18:00:00"),
                ("top_changes", "[]"),
                ("recent_holders", "[]"),
                ("top_changes_date_jalali", "1405-08-27"),
            ],
        )
        side.execute(
            """
            INSERT INTO site_investors (
                id_scheme, shareholder_id, linked_records_count,
                verified_links_count, inferred_links_count
            ) VALUES (?,?,?,?,?)
            """,
            ("share_id", 123, 1, 0, 0),
        )
        side.execute(
            """
            INSERT INTO site_watch (
                ins_code, symbol, company_name, market, first_date,
                first_date_jalali, latest_date, latest_date_jalali,
                total_company_shares, disclosed_shareholder_count,
                total_shareholder_count, total_shareholder_count_note,
                disclosed_ownership_percent_sum, data_completeness,
                resolved_shareholder_count, resolved_ownership_percent_sum,
                resolved_unresolved_count, resolved_status, resolved_status_note,
                raw_ownership_percent_sum, latest_price, latest_price_date, market_value
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "1001", "فولاد", "فولاد مبارکه", "TSE",
                "2026-11-18", "1405-08-27", "2026-11-18", "1405-08-27",
                1000, 1, 1, None, 25.0, "1.0",
                1, 25.0, 0, "ok", None, 25.0, 2600,
                "2026-11-18", 2600000,
            ),
        )
        side.execute(
            """
            INSERT INTO site_company_daily_dedup (
                ins_code, date, resolved_count, resolved_shares, resolved_pct,
                raw_count, raw_pct, raw_shares, unquantified_count,
                unresolved_count, unresolved_shares, total_shares, status, status_note
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "1001", "2026-11-18", 1, 250, 25.0, 1, 25.0, 250,
                0, 0, 0, 1000, "ok", None,
            ),
        )
        side.execute(
            """
            INSERT INTO site_holder_latest (
                ins_code, symbol, id_scheme, shareholder_id, identity_status,
                latest_date, latest_date_jalali, shares, ownership_percent_calc,
                ownership_percent_reported, shares_change, change_type,
                entry_status, price, market_value
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "1001", "فولاد", "share_id", 123, "source_id",
                "2026-11-18", "1405-08-27", 250, 25.0, 25.0,
                50, "increase", "existing", None, None,
            ),
        )
        side.execute(
            """
            INSERT INTO site_shareholder_names (
                id_scheme, shareholder_id, name, shareholder_type,
                first_seen_date, last_seen_date
            ) VALUES (?,?,?,?,?,?)
            """,
            (
                "share_id", 123, "سرمایه‌گذار آزمایشی", "legal",
                "2024-01-01", "2026-11-18",
            ),
        )
        side.execute(
            """
            INSERT INTO site_holder_history (
                id, id_scheme, shareholder_id, ins_code, symbol, identity_status,
                date, jalali_date, shares, ownership_percent_reported,
                total_company_shares, price, price_field, price_date,
                valuation_status, market_value, previous_date, previous_shares,
                shares_change, shares_change_percent, gap_trading_days, change_type,
                change_calculation_status, corporate_action, corporate_action_date,
                corporate_action_ratio, source_change_code, source_change_amount,
                anomaly_flags, data_available, note, entry_status, source_endpoint,
                prev_close_price, prev_close_date, cisin
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                1, "share_id", 123, "1001", "فولاد", "source_id",
                "2026-11-18", "1405-08-27", 250, 25.0,
                1000, None, None, "2026-11-18", "unavailable", None,
                "2026-11-17", 200, 50, 25.0, 1, "increase",
                "consecutive_observations", None, None, None, None, None,
                None, 1, None, "existing", "shareholders_on_date",
                2500, "2026-11-17", "IR1234567890",
            ),
        )
        side.commit()
    finally:
        side.close()
    return api



@pytest.fixture
def read_only_portfolio_runtime(portfolio_api):
    return ReadOnlyPortfolioApi(
        portfolio_api.db_path,
        portfolio_api.sidecar_path,
        site_dir=portfolio_api.site_dir,
        output_dir="",
    )


def test_portfolio_microservice_http_routes_preserve_existing_response_contracts(
    read_only_portfolio_runtime,
):
    previous = portfolio_app.dependency_overrides.get(get_runtime)
    portfolio_app.dependency_overrides[get_runtime] = lambda: read_only_portfolio_runtime
    try:
        with TestClient(portfolio_app) as client:
            assert client.get("/healthz").json() == {
                "status": "ok", "service": "portfolio"
            }
            ready = client.get("/readyz")
            assert ready.status_code == 200, ready.text

            investors = client.get(
                "/api/v1/portfolios/investors",
                params={"q": "سرمایه‌گذار آزمایشی", "limit": 10},
            )
            assert investors.status_code == 200, investors.text
            assert investors.json()["investors"][0]["id"] == "share_id:123"

            detail = client.get("/api/v1/portfolios/share_id:123")
            assert detail.status_code == 200, detail.text
            assert detail.json()["summary"]["confirmed_combined_value_rial"] == 650000
            assert detail.json()["history_series"][0]["combined_value_rial"] == 650000

            history = client.get(
                "/api/v1/portfolios/share_id:123/history",
                params={"ins_code": "1001"},
            )
            assert history.status_code == 200, history.text
            assert history.json()["total"] == 1
            assert history.json()["rows"][0]["estimated_holding_value_rial"] == 650000
    finally:
        if previous is None:
            portfolio_app.dependency_overrides.pop(get_runtime, None)
        else:
            portfolio_app.dependency_overrides[get_runtime] = previous


def test_portfolio_microservice_uses_read_only_source_and_sidecar(
    read_only_portfolio_runtime,
):
    live = read_only_portfolio_runtime.live()
    try:
        assert live.execute("PRAGMA query_only").fetchone()[0] == 1
        databases = {row[1] for row in live.execute("PRAGMA database_list")}
        assert {"main", "sidecar_data"} <= databases
        with pytest.raises(sqlite3.OperationalError):
            live.execute("UPDATE shareholders SET name='mutated' WHERE shareholder_id=123")
    finally:
        live.close()

    sidecar = read_only_portfolio_runtime.sidecar()
    try:
        assert sidecar.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            sidecar.execute(
                "UPDATE site_investors SET linked_records_count=0 "
                "WHERE id_scheme='share_id' AND shareholder_id=123"
            )
    finally:
        sidecar.close()


def test_portfolio_microservice_rejects_bad_identifiers_and_missing_source(
    read_only_portfolio_runtime,
):
    previous = portfolio_app.dependency_overrides.get(get_runtime)
    portfolio_app.dependency_overrides[get_runtime] = lambda: read_only_portfolio_runtime
    try:
        with TestClient(portfolio_app) as client:
            invalid = client.get("/api/v1/portfolios/not-an-id")
            assert invalid.status_code == 400
            invalid_history = client.get("/api/v1/portfolios/share_id:123/history")
            assert invalid_history.status_code == 400
    finally:
        if previous is None:
            portfolio_app.dependency_overrides.pop(get_runtime, None)
        else:
            portfolio_app.dependency_overrides[get_runtime] = previous

    missing = ReadOnlyPortfolioApi(
        str(read_only_portfolio_runtime.db_path) + ".missing",
        read_only_portfolio_runtime.sidecar_path,
        site_dir=read_only_portfolio_runtime.site_dir,
        output_dir="",
    )
    assert missing.readiness() == (
        False, "پایگاه داده‌ی منبع پرتفوی در دسترس نیست."
    )


def test_site_api_composes_portfolio_service(portfolio_api):
    assert isinstance(portfolio_api.portfolio_service, PortfolioService)
    assert portfolio_api.portfolio_service._majority_twin_map.__self__ is portfolio_api


def test_portfolio_investors_keeps_existing_list_contract(portfolio_api):
    payload = portfolio_api.portfolio_investors(
        {"q": ["سرمایه‌گذار"], "sort_by": ["linked"], "limit": ["10"]}
    )
    assert payload == {
        "investors": [
            {
                "id": "share_id:123",
                "canonical_name": "سرمایه‌گذار آزمایشی",
                "verified_links_count": 0,
                "inferred_links_count": 0,
                "linked_records_count": 1,
            }
        ]
    }


def test_portfolio_history_preserves_observation_and_value_fields(portfolio_api):
    payload = portfolio_api.portfolio_history(
        "share_id:123",
        {"ins_code": ["1001"], "limit": ["10"], "offset": ["0"]},
    )

    assert payload["investor_id"] == "share_id:123"
    assert payload["ins_code"] == "1001"
    assert payload["total"] == 1
    assert len(payload["rows"]) == 1
    row = payload["rows"][0]
    assert row["symbol"] == "فولاد"
    assert row["date"] == "2026-11-18"
    assert row["change_classification"] == "net_increase"
    assert row["shares"] == 250
    assert row["shares_change"] == 50
    assert row["ownership_percent_calculated"] == 25.0
    assert row["reference_price"] == 2600.0
    assert row["estimated_holding_value_rial"] == 650000.0
    assert row["estimated_change_value_rial"] == 130000.0
    assert row["identity_conflict"] is False


def test_portfolio_detail_preserves_positions_history_and_identity_evidence(
    portfolio_api,
):
    payload = portfolio_api.portfolio_detail("share_id:123", {})

    assert payload["investor"]["id"] == "share_id:123"
    assert payload["investor"]["canonical_name"] == "سرمایه‌گذار آزمایشی"
    assert payload["summary"]["confirmed_combined_value_rial"] == 650000
    assert payload["summary"]["confirmed_companies_count"] == 1
    assert payload["summary"]["probable_companies_count"] == 0
    assert payload["identity_evidence"]["linked_identity_ids"] == ["share_id:123"]
    assert payload["historical_summary"]["observations_count"] == 1
    assert payload["historical_symbols"][0]["symbol"] == "فولاد"
    assert payload["historical_symbols"][0]["exit_status"] == "currently_disclosed"
    assert payload["history_series"][0]["combined_value_rial"] == 650000
    assert payload["sector_affinity"]["status"] == "registry_missing"


def test_portfolio_api_rejects_invalid_identifiers_and_missing_history_symbol(
    portfolio_api,
):
    with pytest.raises(ApiError) as bad_detail:
        portfolio_api.portfolio_detail("not-an-id", {})
    assert bad_detail.value.status == 400

    with pytest.raises(ApiError) as bad_history:
        portfolio_api.portfolio_history("share_id:123", {})
    assert bad_history.value.status == 400


@pytest.mark.parametrize(
    ("path", "params", "key"),
    [
        ("/api/v1/portfolios/investors", {}, "investors"),
        ("/api/v1/portfolios/share_id:123/history", {"ins_code": ["1001"]}, "rows"),
        ("/api/v1/portfolios/share_id:123", {}, "investor"),
    ],
)
def test_phase_1_dispatcher_routes_into_phase_5_portfolio_service(
    portfolio_api, path, params, key
):
    payload, status, content_type = dispatch_api(
        portfolio_api, "GET", path, params, {}
    )
    assert status == 200
    assert content_type == "json"
    assert key in payload


def test_portfolio_service_enforces_sidecar_readiness(portfolio_api):
    side = portfolio_api.sidecar()
    try:
        side.execute("DELETE FROM site_meta WHERE key='ownership_resolver_schema'")
        side.commit()
    finally:
        side.close()

    with pytest.raises(ApiError) as error:
        portfolio_api.portfolio_investors({})
    assert error.value.status == 503


def test_market_ownership_and_portfolio_routes_compose_on_one_site_api(
    portfolio_api,
):
    """The Phase 3→4→5 read path must work through one shared composition root."""
    requests = [
        ("GET", "/api/v1/market/overview", {}, "summary"),
        ("GET", "/api/v1/market/watch", {"market": ["TSE"]}, "rows"),
        ("GET", "/api/v1/stocks/search", {"q": ["فولاد"]}, "results"),
        ("GET", "/api/v1/stocks/فولاد", {}, "instrument"),
        ("GET", "/api/v1/stocks/فولاد/prices", {}, "prices"),
        ("GET", "/api/v1/stocks/فولاد/shareholders", {}, "groups"),
        (
            "GET", "/api/v1/stocks/فولاد/ownership-series",
            {"id_scheme": ["share_id"], "shareholder_id": ["123"]},
            "company_series",
        ),
        (
            "GET", "/api/v1/shareholders/search",
            {"q": ["share_id:123"]},
            "rows",
        ),
        (
            "GET", "/api/v1/shareholders/observation-history",
            {
                "symbol": ["فولاد"],
                "id_scheme": ["share_id"],
                "shareholder_id": ["123"],
            },
            "history",
        ),
        (
            "GET", "/api/v1/portfolios/investors",
            {"q": ["سرمایه‌گذار"]},
            "investors",
        ),
        (
            "GET", "/api/v1/portfolios/share_id:123/history",
            {"ins_code": ["1001"]},
            "rows",
        ),
        ("GET", "/api/v1/portfolios/share_id:123", {}, "investor"),
    ]

    results = {}
    for method, path, params, expected_key in requests:
        payload, status, content_type = dispatch_api(
            portfolio_api, method, path, params, {}
        )
        assert status == 200, path
        assert content_type == "json", path
        assert expected_key in payload, path
        results[path] = payload

    # Phase 3 — the market service sees the same instrument/price/ownership
    # projections consumed by the subsequent services.
    assert results["/api/v1/market/overview"]["summary"]["total_companies"] == 1
    assert results["/api/v1/market/watch"]["rows"][0]["symbol"] == "فولاد"
    assert results["/api/v1/stocks/search"]["results"][0]["symbol"] == "فولاد"
    assert results["/api/v1/stocks/فولاد"]["instrument"]["ins_code"] == "1001"
    assert results["/api/v1/stocks/فولاد/prices"]["prices"][0]["close"] == 2600

    # Phase 4 — ownership reads the same symbol snapshot and same-date totals.
    shareholders = results["/api/v1/stocks/فولاد/shareholders"]
    assert shareholders["resolved_shares"] == 250
    assert shareholders["resolved_ownership_percent_sum"] == 25.0
    assert results["/api/v1/stocks/فولاد/ownership-series"]["company_series"] == [
        {"time": "2026-11-18", "value": 25.0}
    ]
    assert results["/api/v1/shareholders/observation-history"]["history"] == [
        {"time": "2026-11-18", "value": 25.0}
    ]
    assert results["/api/v1/shareholders/search"]["rows"][0]["group_id"] == "share_id:123"

    # Phase 5 — follow the same identity into the history stream and valuation.
    assert results["/api/v1/portfolios/investors"]["investors"][0]["id"] == "share_id:123"
    history = results["/api/v1/portfolios/share_id:123/history"]
    assert history["total"] == 1
    assert history["rows"][0]["estimated_holding_value_rial"] == 650000
    detail = results["/api/v1/portfolios/share_id:123"]
    assert detail["summary"]["confirmed_combined_value_rial"] == 650000
    assert detail["history_series"][0]["combined_value_rial"] == 650000


def test_market_service_dispatcher_responses_remain_json_and_ordered(
    portfolio_api,
):
    paths = [
        ("/api/v1/market/overview", {}, "summary"),
        ("/api/v1/market/watch", {"limit": ["1"], "offset": ["0"]}, "rows"),
        ("/api/v1/stocks/search", {"q": ["فولاد"]}, "results"),
        ("/api/v1/stocks/فولاد", {}, "instrument"),
        ("/api/v1/stocks/فولاد/prices", {}, "prices"),
    ]
    for path, params, key in paths:
        payload, status, content_type = dispatch_api(
            portfolio_api, "GET", path, params, {}
        )
        assert status == 200
        assert content_type == "json"
        assert key in payload
    assert portfolio_api.market_service._overview_cache["payload"] is not None


def test_market_service_rechecks_readiness_after_cache_expiry(portfolio_api):
    portfolio_api.market_overview()
    portfolio_api.market_service._overview_cache = {"at": 0.0, "payload": None}
    side = portfolio_api.sidecar()
    try:
        side.execute("DELETE FROM site_meta WHERE key='ownership_resolver_schema'")
        side.commit()
    finally:
        side.close()
    with pytest.raises(ApiError) as error:
        portfolio_api.market_overview()
    assert error.value.status == 503


def test_phase_0_to_6_services_compose_without_contract_drift(portfolio_api):
    """One SiteApi instance exercises the Phase 3–6 path after the 0–2 checks.

    Phase 0's manifest and Phase 1/2's HTTP/worker integration tests run in the
    same pytest suite; this test proves the extracted read services compose
    over one Sidecar and the Phase 1 dispatcher.
    """
    from zipfile import ZipFile
    from io import BytesIO

    from site_api.analytics_service import AnalyticsService
    from site_api.market_service import MarketService
    from site_api.ownership_service import OwnershipService

    assert isinstance(portfolio_api.market_service, MarketService)
    assert isinstance(portfolio_api.ownership_service, OwnershipService)
    assert isinstance(portfolio_api.portfolio_service, PortfolioService)
    assert isinstance(portfolio_api.analytics_service, AnalyticsService)

    side = portfolio_api.sidecar()
    try:
        side.execute(
            """
            CREATE TABLE IF NOT EXISTS site_corp_action_counts (
                ins_code TEXT PRIMARY KEY,
                n INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        side.execute(
            "INSERT OR REPLACE INTO site_corp_action_counts(ins_code,n) VALUES (?,?)",
            ("1001", 1),
        )
        side.commit()
    finally:
        side.close()

    # Phase 3: live market read models use the one shared Sidecar snapshot.
    overview, status, content_type = dispatch_api(
        portfolio_api, "GET", "/api/v1/market/overview", {}, {}
    )
    assert (status, content_type) == (200, "json")
    assert overview["summary"]["total_companies"] == 1

    prices, status, content_type = dispatch_api(
        portfolio_api, "GET", "/api/v1/stocks/فولاد/prices", {}, {}
    )
    assert (status, content_type) == (200, "json")
    assert prices["prices"][0]["close"] == 2600

    # Phase 4 and 5: same shareholder identity and same-date valuation survive
    # the hop from ownership views to the investor portfolio service.
    ownership, status, content_type = dispatch_api(
        portfolio_api,
        "GET",
        "/api/v1/stocks/فولاد/ownership-series",
        {"id_scheme": ["share_id"], "shareholder_id": ["123"]},
        {},
    )
    assert (status, content_type) == (200, "json")
    assert ownership["company_series"] == [
        {"time": "2026-11-18", "value": 25.0}
    ]

    portfolio, status, content_type = dispatch_api(
        portfolio_api,
        "GET",
        "/api/v1/portfolios/share_id:123",
        {},
        {},
    )
    assert (status, content_type) == (200, "json")
    assert portfolio["summary"]["confirmed_combined_value_rial"] == 650000

    # Phase 6: screener query and XLSX export are dispatched through the same
    # API composition root and use the stock/holder rows seeded above.
    query_body = {
        "conditions": [{"field": "market", "op": "=", "value": "TSE"}]
    }
    screener, status, content_type = dispatch_api(
        portfolio_api, "POST", "/api/v1/screener/query", {}, query_body
    )
    assert (status, content_type) == (200, "json")
    assert [row["symbol"] for row in screener["rows"]] == ["فولاد"]
    assert screener["rows"][0]["ownership_percent_calculated"] == 25.0
    assert "shareholder_id" not in screener["rows"][0]

    workbook, status, content_type = dispatch_api(
        portfolio_api, "POST", "/api/v1/screener/export", {}, query_body
    )
    assert (status, content_type) == (200, "xlsx")
    with ZipFile(BytesIO(workbook)) as archive:
        assert archive.testzip() is None
        worksheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "فولاد" in worksheet
    assert "shareholder_id" not in worksheet



def test_phase_0_to_7_market_and_saas_analytics_coexist_without_cross_database_access(
    portfolio_api,
):
    """Market reads remain isolated while the canonical SaaS analytics API is mounted."""
    from fastapi.testclient import TestClient
    from saas_core.auth.api import app as saas_app

    market_payload, status, content_type = dispatch_api(
        portfolio_api, "GET", "/api/v1/stocks/فولاد/prices", {}, {}
    )
    assert (status, content_type) == (200, "json")
    assert market_payload["prices"][0]["close"] == 2600

    owner_payload, status, content_type = dispatch_api(
        portfolio_api,
        "GET",
        "/api/v1/stocks/فولاد/ownership-series",
        {"id_scheme": ["share_id"], "shareholder_id": ["123"]},
        {},
    )
    assert (status, content_type) == (200, "json")
    assert owner_payload["company_series"][0]["value"] == 25.0

    portfolio_payload, status, content_type = dispatch_api(
        portfolio_api, "GET", "/api/v1/portfolios/share_id:123", {}, {}
    )
    assert (status, content_type) == (200, "json")
    assert portfolio_payload["summary"]["confirmed_combined_value_rial"] == 650000

    screener_payload, status, content_type = dispatch_api(
        portfolio_api,
        "POST",
        "/api/v1/screener/query",
        {},
        {"conditions": [{"field": "market", "op": "=", "value": "TSE"}]},
    )
    assert (status, content_type) == (200, "json")
    assert screener_payload["rows"][0]["symbol"] == "فولاد"

    # The source holdings file is opened read-only and Sidecar is a distinct
    # rebuildable file. SaaS routes are registered in FastAPI and do not use
    # the Market Sidecar connection.
    assert portfolio_api.db_path != portfolio_api.sidecar_path
    live = portfolio_api.live()
    try:
        assert live.execute("PRAGMA query_only").fetchone()[0] == 1
        assert {row[1] for row in live.execute("PRAGMA database_list")} >= {
            "main", "sidecar_data"
        }
    finally:
        live.close()

    paths = set(saas_app.openapi()["paths"])
    assert "/analytics/events" in paths
    assert "/analytics/admin/overview" in paths
    assert "/analytics/admin/paths" in paths

    with TestClient(saas_app) as client:
        assert client.get("/health").status_code == 200

    # The market domain remains healthy after the SaaS ASGI application is
    # loaded and queried; full PostgreSQL SaaS write flows run in test_postgres_integration_phase9.
    market_again, status, content_type = dispatch_api(
        portfolio_api, "GET", "/api/v1/market/overview", {}, {}
    )
    assert (status, content_type) == (200, "json")
    assert market_again["summary"]["total_companies"] == 1


def test_phases_0_to_8_compose_with_separate_market_and_saas_databases(portfolio_api):
    """All market domain routes and real SaaS admin route registrations coexist safely."""
    from fastapi.testclient import TestClient
    from saas_core.auth.api import app as saas_app

    prices, status, content_type = dispatch_api(
        portfolio_api, "GET", "/api/v1/stocks/فولاد/prices", {}, {}
    )
    assert (status, content_type) == (200, "json")
    assert prices["prices"][0]["close"] == 2600

    ownership, status, content_type = dispatch_api(
        portfolio_api,
        "GET",
        "/api/v1/stocks/فولاد/ownership-series",
        {"id_scheme": ["share_id"], "shareholder_id": ["123"]},
        {},
    )
    assert (status, content_type) == (200, "json")
    assert ownership["company_series"][0]["value"] == 25.0

    portfolio, status, content_type = dispatch_api(
        portfolio_api, "GET", "/api/v1/portfolios/share_id:123", {}, {}
    )
    assert (status, content_type) == (200, "json")
    assert portfolio["summary"]["confirmed_combined_value_rial"] == 650000

    screener, status, content_type = dispatch_api(
        portfolio_api,
        "POST",
        "/api/v1/screener/query",
        {},
        {"conditions": [{"field": "market", "op": "=", "value": "TSE"}]},
    )
    assert (status, content_type) == (200, "json")
    assert screener["rows"][0]["symbol"] == "فولاد"

    # Phase 7/8 are canonical FastAPI modules backed by the independent SaaS
    # PostgreSQL dependency. Their role/MFA/write behavior is tested with real
    # PostgreSQL in tests/saas/test_postgres_integration_phase9.py.
    paths = set(saas_app.openapi()["paths"])
    required_admin_routes = {
        "/admin/dashboard",
        "/admin/users",
        "/admin/users/{user_id}/status",
        "/admin/plans",
        "/admin/payments/orders",
        "/admin/withdrawals",
        "/admin/referrals",
        "/admin/audit-logs",
    }
    assert required_admin_routes <= paths

    with TestClient(saas_app) as client:
        assert client.get("/health").json()["service"] == "saas-api"

    live = portfolio_api.live()
    try:
        assert live.execute("PRAGMA query_only").fetchone()[0] == 1
        databases = {row[1] for row in live.execute("PRAGMA database_list")}
        assert "main" in databases
        assert "sidecar_data" in databases
    finally:
        live.close()

    # Running SaaS liveness and route registration cannot mutate/replace the
    # market Sidecar; market service calls still return the original test data.
    market_again, status, content_type = dispatch_api(
        portfolio_api, "GET", "/api/v1/market/overview", {}, {}
    )
    assert (status, content_type) == (200, "json")
    assert market_again["summary"]["total_companies"] == 1


def test_market_http_adapter_health_cors_and_public_read_only_boundary(portfolio_api, monkeypatch):
    """Exercise the actual HTTP handler and CORS headers over a local socket."""
    import http.client
    import threading
    from http.server import ThreadingHTTPServer

    monkeypatch.setenv("MARKET_PUBLIC_READ_ONLY", "true")
    monkeypatch.setenv("MARKET_ALLOWED_ORIGINS", "https://itmahdik.github.io")
    handler = type("IsolatedMarketHandler", (server.Handler,), {"api": portfolio_api})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address

    def request(method, path, *, origin=None, body=None):
        connection = http.client.HTTPConnection(host, port, timeout=5)
        headers = {}
        if origin:
            headers["Origin"] = origin
        payload = None
        if body is not None:
            import json
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        data = response.read()
        result = (response.status, dict(response.getheaders()), data)
        connection.close()
        return result

    try:
        status, headers, payload = request("GET", "/healthz", origin="https://itmahdik.github.io")
        assert status == 200
        assert headers.get("Access-Control-Allow-Origin") == "https://itmahdik.github.io"
        assert b'"service": "market-api"' in payload

        status, headers, payload = request("GET", "/readyz")
        assert status == 200
        assert b'"status": "ready"' in payload

        status, headers, payload = request("GET", "/api/v1/market/overview")
        assert status == 200
        assert b'"total_companies": 1' in payload

        status, headers, payload = request(
            "GET", "/api/v1/market/overview", origin="https://attacker.example"
        )
        assert status == 200
        assert "Access-Control-Allow-Origin" not in headers

        status, headers, payload = request("POST", "/api/v1/watchlists/default/symbols",
                                           body={"symbol": "فولاد"})
        assert status == 403

        status, headers, payload = request("GET", "/api/v1/watchlists/default")
        assert status == 403

        status, headers, payload = request("GET", "/api/v1/admin/overview")
        assert status == 404

        live = portfolio_api.live()
        try:
            assert live.execute("PRAGMA query_only").fetchone()[0] == 1
        finally:
            live.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_phase_9_cross_platform_end_to_end_smoke(
    portfolio_api, monkeypatch
):
    """Run the Market HTTP API and real PostgreSQL-backed SaaS API together.

    The Market service uses disposable SQLite source/Sidecar files while SaaS
    uses the separately named PostgreSQL integration database. This exercises
    the phase 0–8 runtime boundary in one test process without connecting to
    any production data source or external provider.
    """
    import os
    import threading
    from http.server import ThreadingHTTPServer
    from uuid import uuid4

    import psycopg
    from fastapi.testclient import TestClient
    from psycopg.rows import dict_row

    from cryptography.fernet import Fernet
    from saas_core.auth.api import app as saas_app, get_conn
    from saas_core.auth.mfa import totp_code
    from saas_core.auth.repository import create_session, register_password_user

    dsn = os.environ.get("SAAS_TEST_DATABASE_URL", "").strip()
    expected_database = os.environ.get("SAAS_TEST_DATABASE_NAME", "").strip()
    if not dsn or not expected_database:
        pytest.skip(
            "Phase 9 cross-platform verification requires the named disposable "
            "SAAS_TEST_DATABASE_URL database"
        )

    monkeypatch.setenv("MARKET_PUBLIC_READ_ONLY", "true")
    monkeypatch.setenv(
        "MARKET_ALLOWED_ORIGINS", "https://itmahdik.github.io"
    )
    monkeypatch.setenv(
        "SAAS_ADMIN_MFA_FERNET_KEY",
        Fernet.generate_key().decode("ascii"),
    )

    # Provision one isolated test administrator in the disposable SaaS DB.
    username = f"phase9_e2e_{uuid4().hex[:12]}"
    with psycopg.connect(dsn, row_factory=dict_row) as setup_conn:
        actual_database = setup_conn.execute(
            "SELECT current_database() AS name"
        ).fetchone()["name"]
        assert actual_database == expected_database, (
            "Refusing Phase 9 integration test against an unexpected database"
        )
        user = register_password_user(
            setup_conn,
            username,
            "Phase9-cross-test-Password-2026",
            display_name="Phase 9 Integration Test",
        )
        setup_conn.execute(
            """INSERT INTO user_roles(user_id, role_id)
               SELECT %s, id FROM roles WHERE code='superadmin'
               ON CONFLICT DO NOTHING""",
            (user["id"],),
        )
        token, _expires = create_session(setup_conn, user["id"])
        setup_conn.commit()

    def test_database_connection():
        with psycopg.connect(dsn, row_factory=dict_row) as connection:
            yield connection

    previous_override = saas_app.dependency_overrides.get(get_conn)
    saas_app.dependency_overrides[get_conn] = test_database_connection

    handler = type(
        "Phase9MarketHandler",
        (server.Handler,),
        {"api": portfolio_api},
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address

    def market_request(method, path, body=None):
        import http.client
        import json

        connection = http.client.HTTPConnection(host, port, timeout=5)
        headers = {}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            return response.status, raw
        finally:
            connection.close()

    try:
        # Phase 3–6 are served by the real Market HTTP adapter.
        market_status, market_body = market_request(
            "GET", "/api/v1/market/overview"
        )
        assert market_status == 200, market_body.decode("utf-8", "replace")
        assert b'"total_companies": 1' in market_body

        # The public Market deployment must not expose user state or legacy
        # administrative writes, even while the SaaS admin API is active.
        write_status, _ = market_request(
            "POST",
            "/api/v1/watchlists/default/symbols",
            {"symbol": "فولاد"},
        )
        legacy_admin_status, _ = market_request(
            "GET", "/api/v1/admin/overview"
        )
        assert write_status == 403
        assert legacy_admin_status == 404

        # Phase 7–8 are served by FastAPI against the independently named
        # PostgreSQL database. The admin surface is unavailable until this
        # exact session completes TOTP enrollment and confirmation.
        auth_headers = {"Authorization": f"Bearer {token}"}
        with TestClient(saas_app) as saas_client:
            health = saas_client.get("/health")
            assert health.status_code == 200
            assert health.json()["service"] == "saas-api"

            identity = saas_client.get("/auth/me", headers=auth_headers)
            assert identity.status_code == 200
            assert identity.json()["user"]["id"] == str(user["id"])

            blocked_admin = saas_client.get(
                "/admin/dashboard", headers=auth_headers
            )
            assert blocked_admin.status_code == 403

            setup = saas_client.post(
                "/auth/mfa/setup", headers=auth_headers
            )
            assert setup.status_code == 200, setup.text
            confirmed = saas_client.post(
                "/auth/mfa/confirm",
                headers=auth_headers,
                json={"code": totp_code(setup.json()["secret"])},
            )
            assert confirmed.status_code == 200, confirmed.text

            allowed_admin = saas_client.get(
                "/admin/dashboard", headers=auth_headers
            )
            assert allowed_admin.status_code == 200, allowed_admin.text

        # SaaS activity must not replace, mutate or make the Market SQLite
        # source writable. Re-query Market after the SaaS admin flow.
        market_again_status, market_again_body = market_request(
            "GET", "/api/v1/market/overview"
        )
        assert market_again_status == 200, market_again_body.decode(
            "utf-8", "replace"
        )
        assert b'"total_companies": 1' in market_again_body

        assert portfolio_api.db_path != portfolio_api.sidecar_path
        live = portfolio_api.live()
        try:
            assert live.execute("PRAGMA query_only").fetchone()[0] == 1
            databases = {
                row[1] for row in live.execute("PRAGMA database_list")
            }
            assert {"main", "sidecar_data"} <= databases
        finally:
            live.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        if previous_override is None:
            saas_app.dependency_overrides.pop(get_conn, None)
        else:
            saas_app.dependency_overrides[get_conn] = previous_override


def test_phase_10_crm_runs_alongside_market_without_cross_database_access(
    portfolio_api, monkeypatch
):
    """An admin-only CRM workflow must coexist with, but not access, Market data."""
    import os
    import threading
    from http.server import ThreadingHTTPServer
    from uuid import uuid4

    import psycopg
    from fastapi.testclient import TestClient
    from psycopg.rows import dict_row
    from cryptography.fernet import Fernet

    from saas_core.auth.api import app as saas_app, get_conn
    from saas_core.auth.mfa import totp_code
    from saas_core.auth.repository import create_session, register_password_user

    dsn = os.environ.get("SAAS_TEST_DATABASE_URL", "").strip()
    expected_database = os.environ.get("SAAS_TEST_DATABASE_NAME", "").strip()
    if not dsn or not expected_database:
        pytest.skip(
            "Phase 10 cross-platform test requires the named disposable SaaS PostgreSQL database"
        )

    monkeypatch.setenv("MARKET_PUBLIC_READ_ONLY", "true")
    monkeypatch.setenv("SAAS_ADMIN_MFA_FERNET_KEY", Fernet.generate_key().decode("ascii"))

    with psycopg.connect(dsn, row_factory=dict_row) as setup_conn:
        actual = setup_conn.execute("SELECT current_database() AS name").fetchone()["name"]
        assert actual == expected_database, "Refusing Phase 10 test on an unexpected database"

        staff = register_password_user(
            setup_conn,
            f"phase10_crm_staff_{uuid4().hex[:10]}",
            "Phase10-CRM-staff-password-2026",
            display_name="Phase 10 CRM Staff",
        )
        customer = register_password_user(
            setup_conn,
            f"phase10_crm_customer_{uuid4().hex[:10]}",
            "Phase10-CRM-customer-password-2026",
            display_name="Phase 10 CRM Customer",
        )
        setup_conn.execute(
            """INSERT INTO user_roles(user_id,role_id)
               SELECT %s,id FROM roles WHERE code='admin'
               ON CONFLICT DO NOTHING""",
            (staff["id"],),
        )
        staff_token, _ = create_session(setup_conn, staff["id"])
        customer_token, _ = create_session(setup_conn, customer["id"])
        setup_conn.commit()

    def test_connection():
        with psycopg.connect(dsn, row_factory=dict_row) as connection:
            yield connection

    previous_override = saas_app.dependency_overrides.get(get_conn)
    saas_app.dependency_overrides[get_conn] = test_connection

    handler = type(
        "Phase10MarketHandler",
        (server.Handler,),
        {"api": portfolio_api},
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address

    def market_overview():
        import http.client

        connection = http.client.HTTPConnection(host, port, timeout=5)
        try:
            connection.request("GET", "/api/v1/market/overview")
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    try:
        before_status, before_body = market_overview()
        assert before_status == 200, before_body.decode("utf-8", "replace")
        assert b'"total_companies": 1' in before_body

        headers = {"Authorization": f"Bearer {staff_token}"}
        with TestClient(saas_app) as client:
            # CRM staff may enroll MFA without receiving Superadmin-console access.
            setup = client.post("/auth/mfa/setup", headers=headers)
            assert setup.status_code == 200, setup.text
            verified = client.post(
                "/auth/mfa/confirm",
                headers=headers,
                json={"code": totp_code(setup.json()["secret"])},
            )
            assert verified.status_code == 200, verified.text

            assert client.get("/admin/dashboard", headers=headers).status_code == 403

            crm_customer = client.get(
                f"/crm/customers/{customer['id']}", headers=headers
            )
            assert crm_customer.status_code == 200, crm_customer.text
            assert crm_customer.json()["customer"]["id"] == str(customer["id"])

            interaction = client.post(
                f"/crm/customers/{customer['id']}/interactions",
                headers=headers,
                json={
                    "channel": "note",
                    "subject": "Market research follow-up",
                    "summary": "Reviewed the customer's requested product feedback.",
                },
            )
            assert interaction.status_code == 201, interaction.text

            # A standard customer does not inherit staff CRM permissions.
            denied = client.get(
                "/crm/customers",
                headers={"Authorization": f"Bearer {customer_token}"},
            )
            assert denied.status_code == 403

        after_status, after_body = market_overview()
        assert after_status == 200, after_body.decode("utf-8", "replace")
        assert b'"total_companies": 1' in after_body

        assert portfolio_api.db_path != portfolio_api.sidecar_path
        live = portfolio_api.live()
        try:
            assert live.execute("PRAGMA query_only").fetchone()[0] == 1
            databases = {row[1] for row in live.execute("PRAGMA database_list")}
            assert {"main", "sidecar_data"} <= databases
        finally:
            live.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        if previous_override is None:
            saas_app.dependency_overrides.pop(get_conn, None)
        else:
            saas_app.dependency_overrides[get_conn] = previous_override
