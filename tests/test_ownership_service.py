"""Cross-phase tests for OwnershipService and its integration with Phases 0–3.

All rows live in disposable SQLite files. The live/source database is opened by
the production read-only connector; the tests do not touch production data.
"""
from __future__ import annotations

import sqlite3

import pytest

import server
from server import ApiError, SiteApi
from site_api.dispatch import dispatch_api
from site_api.ownership_service import OwnershipService


@pytest.fixture
def ownership_api(tmp_path):
    source_path = tmp_path / "source.sqlite"
    sidecar_path = tmp_path / "site_data.sqlite"

    source = sqlite3.connect(source_path)
    source.executescript(
        """
        CREATE TABLE companies (
            ins_code TEXT PRIMARY KEY,
            cisin TEXT,
            total_shares_current INTEGER
        );
        CREATE TABLE company_daily (
            ins_code TEXT,
            date TEXT,
            total_company_shares INTEGER,
            disclosed_shareholder_count INTEGER,
            disclosed_ownership_percent_sum REAL
        );
        CREATE TABLE shareholders (
            id_scheme TEXT,
            shareholder_id INTEGER,
            name TEXT,
            name_normalized TEXT,
            shareholder_type TEXT,
            first_seen_date TEXT,
            last_seen_date TEXT,
            raw_source_ids TEXT,
            PRIMARY KEY (id_scheme, shareholder_id)
        );
        CREATE TABLE shareholder_id_xref (
            ins_code TEXT,
            source_scheme TEXT,
            source_id INTEGER,
            canonical_scheme TEXT,
            canonical_id INTEGER,
            status TEXT
        );
        CREATE TABLE shareholder_holdings (
            ins_code TEXT,
            date TEXT,
            id_scheme TEXT,
            shareholder_id INTEGER,
            shares INTEGER,
            ownership_percent REAL,
            ownership_percent_calc REAL,
            shares_change INTEGER,
            market_value INTEGER,
            price INTEGER,
            valuation_status TEXT,
            identity_status TEXT,
            entry_status TEXT,
            change_type TEXT,
            jalali_date TEXT,
            total_company_shares INTEGER
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
    source.execute(
        "INSERT INTO companies VALUES (?,?,?)",
        ("1001", "IR1234567890", 1000),
    )
    source.execute(
        "INSERT INTO company_daily VALUES (?,?,?,?,?)",
        ("1001", "2026-11-18", 1000, 1, 25.0),
    )
    source.execute(
        "INSERT INTO shareholders VALUES (?,?,?,?,?,?,?,?)",
        (
            "share_id", 123, "سرمایه‌گذار آزمایشی",
            "سرمایه‌گذار آزمایشی", "legal",
            "2024-01-01", "2026-11-18", "share_id:123",
        ),
    )
    source.execute(
        """
        INSERT INTO shareholder_holdings VALUES
        (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "1001", "2026-11-18", "share_id", 123,
            250, 25.0, 25.0, 50, None, None,
            "unavailable", "source_id", "existing", "new",
            "1405-08-27", 1000,
        ),
    )
    source.execute(
        "INSERT INTO trading_days VALUES (?,?,?,?,?,?,?)",
        ("1001", "2026-11-18", 2500, 2700, 2400, 2600, 15000),
    )
    source.commit()
    source.close()

    api = SiteApi(str(source_path), str(sidecar_path), "", "")
    side = api.sidecar()  # creates the real current Sidecar schema
    try:
        side.execute("CREATE TABLE IF NOT EXISTS site_holder_history (id INTEGER PRIMARY KEY)")
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
            INSERT INTO site_watch (
                ins_code, symbol, company_name, market, first_date,
                first_date_jalali, latest_date, latest_date_jalali,
                total_company_shares, disclosed_shareholder_count,
                total_shareholder_count, disclosed_ownership_percent_sum,
                resolved_shareholder_count, resolved_ownership_percent_sum,
                resolved_unresolved_count, resolved_status,
                raw_ownership_percent_sum, latest_price, latest_price_date,
                market_value
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "1001", "فولاد", "فولاد مبارکه", "TSE",
                "2026-11-18", "1405-08-27", "2026-11-18", "1405-08-27",
                1000, 1, 1, 25.0, 1, 25.0, 0, "ok",
                25.0, 2600, "2026-11-18", 2600000,
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
                "1001", "2026-11-18", 1, 250, 25.0,
                1, 25.0, 250, 0, 0, 0, 1000, "ok", None,
            ),
        )
        side.execute(
            """
            INSERT INTO site_holder_latest (
                ins_code, symbol, id_scheme, shareholder_id, identity_status,
                latest_date, latest_date_jalali, shares,
                ownership_percent_calc, ownership_percent_reported,
                shares_change, change_type, entry_status, price, market_value
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "1001", "فولاد", "share_id", 123, "source_id",
                "2026-11-18", "1405-08-27", 250, 25.0, 25.0,
                50, "new", "existing", None, None,
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
        side.commit()
    finally:
        side.close()
    return api


def test_site_api_composes_ownership_service_without_circular_server_dependency(
    ownership_api,
):
    assert isinstance(ownership_api.ownership_service, OwnershipService)
    assert ownership_api.ownership_service.sidecar.__self__ is ownership_api
    assert ownership_api.ownership_service.live.__self__ is ownership_api
    assert ownership_api.ownership_service.resolve_symbol.__self__ is (
        ownership_api.market_service
    )


def test_stock_shareholders_preserves_resolved_snapshot_contract(ownership_api):
    payload = ownership_api.stock_shareholders("فولاد", {})

    assert payload["total"] == 1
    assert payload["date_jalali"] == "1405-08-27"
    assert payload["resolved_shareholder_count"] == 1
    assert payload["unquantified_count"] == 0
    assert payload["resolved_shares"] == 250
    assert payload["resolved_ownership_percent_sum"] == 25.0
    assert payload["status"] == "ok"
    assert payload["groups"][0]["group_key"] == "share_id:123"
    assert payload["groups"][0]["shareholder_name"] == "سرمایه‌گذار آزمایشی"
    assert payload["groups"][0]["members"] == [
        {"id_scheme": "share_id", "shareholder_id": 123}
    ]


def test_stock_shareholders_rejects_invalid_jalali_date(ownership_api):
    with pytest.raises(ApiError) as error:
        ownership_api.stock_shareholders(
            "فولاد", {"date_jalali": ["1405-99-99"]}
        )
    assert error.value.status == 400


@pytest.mark.parametrize(
    "value",
    [
        "1405-00-01", "1405-13-01", "1405-07-31",
        "1405-12-30", "1405-08-00", "not-a-date",
    ],
)
def test_jalali_date_parser_rejects_out_of_range_values(value):
    assert server.jalali_to_gregorian_date(value) == ""


def test_jalali_date_parser_accepts_a_leap_year_esfand_30():
    assert server.jalali_to_gregorian_date("1400-12-30") == "2022-03-21"


def test_ownership_series_preserves_same_date_percentages_and_event_valuation(
    ownership_api,
):
    payload = ownership_api.stock_ownership_series(
        "فولاد", {"id_scheme": ["share_id"], "shareholder_id": ["123"]}
    )

    assert payload["company_series"] == [
        {"time": "2026-11-18", "value": 25.0}
    ]
    assert payload["company_shares_series"] == [
        {"time": "2026-11-18", "value": 250}
    ]
    assert payload["holder_series"] == [
        {"time": "2026-11-18", "value": 25.0}
    ]
    assert payload["events"] == [
        {
            "date_jalali": "1405-08-27",
            "date_gregorian": "2026-11-18",
            "shareholder_name": "سرمایه‌گذار آزمایشی",
            "change_type": "new",
            "shares_change": 50,
            "ownership_percent_calculated": 25.0,
            "value_rial": 130000,
        }
    ]


def test_cross_company_shareholder_search_preserves_identity_group_contract(
    ownership_api,
):
    payload = ownership_api.shareholders_search(
        {"q": ["share_id:123"], "limit": ["10"], "offset": ["0"]}
    )

    assert len(payload["rows"]) == 1
    row = payload["rows"][0]
    assert row["group_id"] == "share_id:123"
    assert row["shareholder_name"] == "سرمایه‌گذار آزمایشی"
    assert row["n_companies"] == 1
    assert row["symbols"][0]["symbol"] == "فولاد"
    assert row["latest_ownership_pct"] == 25.0
    assert row["first_observed_date_jalali"] == "1402-10-11"
    assert row["last_observed_date_jalali"] == "1405-08-27"


def test_shareholder_observation_history_preserves_date_series(ownership_api):
    payload = ownership_api.shareholder_observation_history(
        {
            "symbol": ["فولاد"],
            "id_scheme": ["share_id"],
            "shareholder_id": ["123"],
        }
    )
    assert payload == {
        "history": [{"time": "2026-11-18", "value": 25.0}]
    }


@pytest.mark.parametrize(
    ("method", "path", "params", "key"),
    [
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
    ],
)
def test_phase_1_dispatcher_routes_into_phase_4_ownership_service(
    ownership_api, method, path, params, key
):
    payload, status, content_type = dispatch_api(
        ownership_api, method, path, params, {}
    )
    assert status == 200
    assert content_type == "json"
    assert key in payload


def test_ownership_readiness_gate_remains_enforced_for_shareholder_search(
    ownership_api,
):
    side = ownership_api.sidecar()
    try:
        side.execute("DELETE FROM site_meta WHERE key='last_refresh'")
        side.commit()
    finally:
        side.close()

    with pytest.raises(ApiError) as error:
        ownership_api.shareholders_search({"q": ["share_id:123"]})
    assert error.value.status == 503


def test_extracted_service_keeps_source_database_read_only(ownership_api):
    live = ownership_api.live()
    try:
        assert live.execute("PRAGMA query_only").fetchone()[0] == 1
        assert live.execute(
            "SELECT COUNT(*) FROM shareholder_holdings"
        ).fetchone()[0] == 1
    finally:
        live.close()
