from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from market_data_service.repository import MarketDataRepository
from market_data_service.dates import gregorian_to_jalali_date, normalize_symbol


def test_jalali_date_conversion_and_symbol_normalization():
    assert gregorian_to_jalali_date("2026-11-17") == "1405-08-26"
    assert gregorian_to_jalali_date("20261118") == "1405-08-27"
    from market_data_service.dates import normalize_gregorian_date
    assert normalize_gregorian_date("20261118") == "2026-11-18"
    assert normalize_gregorian_date("2026/11/18") == "2026-11-18"
    assert normalize_symbol("كگل") == normalize_symbol("کگل")


def test_market_data_readiness_requires_matching_sidecar_fingerprint(market_store):
    repository, _source_path, _sidecar_path = market_store
    ready, reason = repository.readiness()
    assert ready is True
    assert reason == "ready"


def test_market_data_refuses_stale_sidecar(market_store):
    repository, source_path, _sidecar_path = market_store
    stat = os.stat(source_path)
    os.utime(source_path, (stat.st_atime + 20, stat.st_mtime + 20))
    ready, reason = repository.readiness()
    assert ready is False
    assert "stale" in reason


def test_instrument_search_filters_and_normalizes_persian_arabic_letters(market_store):
    repository, _, _ = market_store
    result = repository.search_instruments(q="كگل", market="TSE", limit=10)
    assert result["total"] == 1
    assert result["rows"][0]["symbol"] == "كگل"
    assert result["rows"][0]["ins_code"] == "1003"


def test_instrument_search_paginates_and_returns_only_instrument_fields(market_store):
    repository, _, _ = market_store
    result = repository.search_instruments(market="ALL", limit=1, offset=1)
    assert result["total"] == 3
    assert len(result["rows"]) == 1
    assert set(result["rows"][0]) == {
        "ins_code", "symbol", "company_name", "market"
    }


def test_instrument_detail_joins_only_market_reference_fields(market_store):
    repository, _, _ = market_store
    result = repository.instrument_detail("فولاد")
    assert result["instrument"] == {
        "ins_code": "1001",
        "symbol": "فولاد",
        "company_name": "فولاد مبارکه اصفهان",
        "market": "TSE",
        "cisin": "IR1234567890",
        "total_shares": 1000000,
    }
    assert result["last_trade"] == {
        "date": "2026-11-18",
        "date_jalali": "1405-08-27",
        "close": 2600,
        "volume": 15000,
    }
    assert "shareholders" not in result
    assert "wallet" not in result


def test_prices_use_trading_days_ohlc_fallbacks_and_ascending_order(market_store):
    repository, _, _ = market_store
    result = repository.daily_prices("فولاد")
    assert result["prices"] == [
        {
            "time": "2026-11-17",
            "date_jalali": "1405-08-26",
            "open": 2400,
            "high": 2550,
            "low": 2350,
            "close": 2500,
            "volume": 12000,
        },
        {
            "time": "2026-11-18",
            "date_jalali": "1405-08-27",
            "open": 2600,
            "high": 2600,
            "low": 2600,
            "close": 2600,
            "volume": 15000,
        },
    ]


def test_price_date_range_and_limit_are_applied_before_response(market_store):
    repository, _, _ = market_store
    result = repository.daily_prices(
        "فولاد", start_date="2026-11-18", end_date="2026-11-18", limit=1
    )
    assert len(result["prices"]) == 1
    assert result["prices"][0]["time"] == "2026-11-18"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"market": "FOREX"}, "market must"),
        ({"limit": 0}, "limit must"),
        ({"offset": -1}, "offset must"),
    ],
)
def test_instrument_search_rejects_invalid_parameters(market_store, kwargs, message):
    repository, _, _ = market_store
    with pytest.raises(ValueError, match=message):
        repository.search_instruments(**kwargs)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"start_date": "1405-08-27"}, "Gregorian"),
        ({"start_date": "2026-11-20", "end_date": "2026-11-18"}, "less than"),
        ({"limit": 20001}, "limit must"),
    ],
)
def test_price_query_rejects_invalid_parameters(market_store, kwargs, message):
    repository, _, _ = market_store
    with pytest.raises(ValueError, match=message):
        repository.daily_prices("فولاد", **kwargs)


def test_unknown_symbol_raises_not_found(market_store):
    from market_data_service.repository import InstrumentNotFound
    repository, _, _ = market_store
    with pytest.raises(InstrumentNotFound):
        repository.instrument_detail("نماد-ناموجود")


def test_source_and_sidecar_connections_are_enforced_read_only(market_store):
    repository, _, _ = market_store
    source = repository.source()
    sidecar = repository.sidecar()
    try:
        with pytest.raises(sqlite3.OperationalError):
            source.execute("DELETE FROM trading_days")
        with pytest.raises(sqlite3.OperationalError):
            sidecar.execute("DELETE FROM site_watch")
    finally:
        source.close()
        sidecar.close()
