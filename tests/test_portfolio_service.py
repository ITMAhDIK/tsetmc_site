"""Integration tests for the extracted PortfolioService."""
from __future__ import annotations

import sqlite3

import pytest

import server
from server import ApiError, SiteApi
from site_api.dispatch import dispatch_api
from site_api.portfolio_service import PortfolioService


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


def test_site_api_composes_portfolio_service(portfolio_api):
    assert isinstance(portfolio_api.portfolio_service, PortfolioService)
    assert portfolio_api.portfolio_service.majority_twin_map.__self__ is portfolio_api


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
