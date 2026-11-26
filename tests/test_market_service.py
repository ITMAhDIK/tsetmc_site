"""Integration tests for the extracted MarketService.

The fixture uses disposable SQLite files. The tests exercise the actual SiteApi
composition root and versioned dispatcher, while the source connection is opened
through the same read-only path as production.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

import server
from server import ApiError, SiteApi
from site_api.dispatch import dispatch_api
from site_api.market_service import MarketService


@pytest.fixture
def market_api(tmp_path):
    source_path = tmp_path / "source.sqlite"
    sidecar_path = tmp_path / "sidecar.sqlite"

    side = sqlite3.connect(sidecar_path)
    side.executescript(
        """
        CREATE TABLE site_meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE site_holder_history (id INTEGER PRIMARY KEY);
        CREATE TABLE site_watch (
            ins_code TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            company_name TEXT,
            market TEXT NOT NULL,
            first_date TEXT,
            first_date_jalali TEXT,
            latest_date TEXT,
            latest_date_jalali TEXT,
            total_company_shares INTEGER,
            disclosed_shareholder_count INTEGER,
            total_shareholder_count INTEGER,
            total_shareholder_count_note TEXT,
            disclosed_ownership_percent_sum REAL,
            data_completeness TEXT,
            resolved_shareholder_count INTEGER,
            resolved_ownership_percent_sum REAL,
            resolved_unresolved_count INTEGER,
            resolved_status TEXT,
            resolved_status_note TEXT,
            raw_ownership_percent_sum REAL,
            latest_price REAL,
            latest_price_date TEXT,
            market_value REAL
        );
        CREATE TABLE site_company_daily_dedup (
            ins_code TEXT NOT NULL,
            date TEXT NOT NULL,
            resolved_count INTEGER,
            resolved_shares INTEGER,
            resolved_pct REAL,
            raw_count INTEGER,
            raw_pct REAL,
            raw_shares INTEGER,
            unquantified_count INTEGER,
            unresolved_count INTEGER,
            unresolved_shares INTEGER,
            total_shares INTEGER,
            status TEXT,
            status_note TEXT,
            PRIMARY KEY (ins_code, date)
        );
        """
    )
    side.executemany(
        "INSERT INTO site_meta(key, value) VALUES (?, ?)",
        [
            ("last_refresh", "2026-11-26T18:00:00"),
            ("ownership_resolver_schema", "resolved-v1-source-exclusions"),
            ("sidecar_version", str(server.SIDECAR_VERSION)),
            ("earliest_date", "2026-11-17"),
            ("latest_date", "2026-11-18"),
            ("holdings_count", "250"),
            ("last_fetch_ts", "2026-11-26T18:00:00"),
            ("top_changes", json.dumps([{"symbol": "فولاد", "change": 1}])),
            ("recent_holders", json.dumps([{"name": "سهام‌دار آزمایشی"}])),
            ("top_changes_date_jalali", "1405-08-27"),
        ],
    )
    insert_watch = """
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
    """
    side.execute(
        insert_watch,
        (
            "1001", "فولاد", "فولاد مبارکه", "TSE", "2026-11-17",
            "1405-08-26", "2026-11-18", "1405-08-27",
            1000, 3, 8, "برآوردی", 72.5, "0.95",
            2, 65.0, 1, "review", "نیازمند بررسی",
            72.5, 2600, "2026-11-18", 2600000,
        ),
    )
    side.execute(
        insert_watch,
        (
            "1002", "فملی", "ملی صنایع مس ایران", "IFB", "2026-11-16",
            "1405-08-25", "2026-11-18", "1405-08-27",
            2000, 4, 12, None, 55.0, "1.0",
            3, 50.0, 0, "ok", None,
            55.0, 1800, "2026-11-18", 3600000,
        ),
    )
    side.execute(
        """
        INSERT INTO site_company_daily_dedup VALUES
        (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "1001", "2026-11-18", 2, 650, 65.0, 3, 72.5, 725,
            1, 1, 75, 1000, "review", "رکورد تکراری حل‌نشده",
        ),
    )
    side.commit()
    side.close()

    live = sqlite3.connect(source_path)
    live.executescript(
        """
        CREATE TABLE companies (
            ins_code TEXT PRIMARY KEY,
            cisin TEXT,
            total_shares_current INTEGER
        );
        CREATE TABLE company_daily (
            ins_code TEXT,
            date TEXT,
            disclosed_shareholder_count INTEGER,
            disclosed_ownership_percent_sum REAL
        );
        CREATE TABLE trading_days (
            ins_code TEXT,
            date TEXT,
            p_first REAL,
            p_max REAL,
            p_min REAL,
            p_closing REAL,
            volume INTEGER
        );
        """
    )
    live.execute(
        "INSERT INTO companies VALUES (?,?,?)",
        ("1001", "IR1234567890", 1000),
    )
    live.execute(
        "INSERT INTO company_daily VALUES (?,?,?,?)",
        ("1001", "2026-11-18", 3, 72.5),
    )
    live.executemany(
        "INSERT INTO trading_days VALUES (?,?,?,?,?,?,?)",
        [
            ("1001", "2026-11-17", 2400, 2550, 2350, 2500, 12000),
            ("1001", "2026-11-18", None, 0, None, 2600, 15000),
            ("1001", "2026-11-19", 2600, 2700, 2500, None, 16000),
        ],
    )
    live.commit()
    live.close()

    return SiteApi(str(source_path), str(sidecar_path), "", "")


def test_site_api_composes_a_real_market_service(market_api):
    assert isinstance(market_api.market_service, MarketService)
    assert market_api.market_service.sidecar.__self__ is market_api
    assert market_api.market_service.live.__self__ is market_api


def test_market_overview_preserves_summary_and_precomputed_panels(market_api):
    payload = market_api.market_overview()

    assert payload["summary"] == {
        "total_companies": 2,
        "tse_companies": 1,
        "ifb_companies": 1,
        "unknown_market_companies": 0,
        "earliest_date_jalali": "1405-08-26",
        "latest_date_jalali": "1405-08-27",
        "total_holdings_rows": 250,
        "last_updated_at": "2026-11-26T18:00:00",
    }
    assert payload["top_ownership_changes"] == [{"symbol": "فولاد", "change": 1}]
    assert payload["recently_observed_shareholders"] == [
        {"name": "سهام‌دار آزمایشی"}
    ]


def test_market_overview_cache_remains_local_to_the_service(market_api):
    first = market_api.market_overview()
    second = market_api.market_overview()
    assert first is second
    assert market_api.market_service._overview_cache["payload"] is first


def test_market_watch_filters_and_paginates_with_existing_row_shape(market_api):
    payload = market_api.market_watch(
        {
            "market": ["TSE"],
            "q": ["فولاد"],
            "sort_by": ["disclosed_pct"],
            "sort_dir": ["DESC"],
            "limit": ["1"],
            "offset": ["0"],
        }
    )

    assert payload["total"] == 1
    assert len(payload["rows"]) == 1
    assert payload["rows"][0]["symbol"] == "فولاد"
    assert payload["rows"][0]["resolved_ownership_percent_sum"] == 65.0


def test_symbol_search_keeps_existing_response_contract(market_api):
    assert market_api.stocks_search({"q": ["مس"], "limit": ["5"]}) == {
        "results": [
            {"symbol": "فملی", "company_name": "ملی صنایع مس ایران", "market": "IFB"}
        ]
    }
    assert market_api.stocks_search({"q": [""], "limit": ["5"]}) == {
        "results": []
    }


def test_symbol_resolution_keeps_fa_normalization_and_404(market_api):
    side = market_api.sidecar()
    try:
        assert market_api.resolve_symbol(side, "فولاد")["ins_code"] == "1001"
        with pytest.raises(ApiError) as error:
            market_api.resolve_symbol(side, "نماد-ناموجود")
        assert error.value.status == 404
    finally:
        side.close()


def test_stock_detail_keeps_identity_resolved_and_raw_history_fields(market_api):
    payload = market_api.stock_detail("فولاد")

    assert payload["instrument"]["ins_code"] == "1001"
    assert payload["instrument"]["cisin"] == "IR1234567890"
    assert payload["latest_snapshot"]["disclosed_ownership_percent_sum"] == 65.0
    assert payload["latest_snapshot"]["raw_ownership_percent_sum"] == 72.5
    assert payload["company_history"] == [
        {
            "date_jalali": "1405-08-27",
            "disclosed_shareholder_count": 2,
            "disclosed_ownership_percent_sum": 65.0,
            "raw_ownership_percent_sum": 72.5,
            "raw_shareholder_count": 3,
            "resolved_shares": 650,
            "resolved": True,
            "unquantified_count": 1,
            "unresolved_count": 1,
            "unresolved_shares": 75,
            "total_shares": 1000,
            "status": "review",
            "status_note": "رکورد تکراری حل‌نشده",
        }
    ]
    assert payload["available_dates"] == ["1405-08-27"]


def test_daily_prices_still_come_from_trading_days_and_keep_ohlc_fallbacks(
    market_api,
):
    assert market_api.stock_prices("فولاد") == {
        "prices": [
            {
                "time": "2026-11-17",
                "date_jalali": "1405-08-26",
                "open": 2400.0,
                "high": 2550.0,
                "low": 2350.0,
                "close": 2500.0,
                "volume": 12000,
            },
            {
                "time": "2026-11-18",
                "date_jalali": "1405-08-27",
                "open": 2600.0,
                "high": 2600.0,
                "low": 2600.0,
                "close": 2600.0,
                "volume": 15000,
            },
        ]
    }


@pytest.mark.parametrize(
    ("method", "path", "params", "key"),
    [
        ("GET", "/api/v1/market/overview", {}, "summary"),
        ("GET", "/api/v1/market/watch", {"limit": ["1"]}, "rows"),
        ("GET", "/api/v1/stocks/search", {"q": ["مس"]}, "results"),
        ("GET", "/api/v1/stocks/فولاد/prices", {}, "prices"),
        ("GET", "/api/v1/stocks/فولاد", {}, "instrument"),
    ],
)
def test_phase_1_dispatcher_routes_into_market_service(
    market_api, method, path, params, key
):
    payload, status, content_type = dispatch_api(
        market_api, method, path, params, {}
    )
    assert status == 200
    assert content_type == "json"
    assert key in payload


def test_sidecar_readiness_gate_is_not_bypassed_by_extracted_service(market_api):
    side = market_api.sidecar()
    try:
        side.execute("DELETE FROM site_meta WHERE key='last_refresh'")
        side.commit()
    finally:
        side.close()

    with pytest.raises(ApiError) as error:
        market_api.market_watch({})
    assert error.value.status == 503


def test_existing_site_api_compatibility_methods_delegate_to_service(market_api):
    side = market_api.sidecar()
    try:
        row = market_api._resolve_symbol_row(side, "فولاد")
        assert row["ins_code"] == "1001"
        assert market_api._company_daily(
            market_api.live(), "1001"
        )[0]["date"] == "2026-11-18"
    finally:
        side.close()
