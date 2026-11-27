from __future__ import annotations

import os
import sqlite3

import pytest

from market_data_service.repository import MarketDataRepository


@pytest.fixture
def market_store(tmp_path):
    source_path = tmp_path / "source.sqlite"
    sidecar_path = tmp_path / "sidecar.sqlite"

    source = sqlite3.connect(source_path)
    source.executescript(
        """
        CREATE TABLE companies (
            ins_code TEXT PRIMARY KEY,
            cisin TEXT,
            total_shares_current INTEGER
        );
        CREATE TABLE trading_days (
            ins_code TEXT NOT NULL,
            date TEXT NOT NULL,
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
        ("1001", "IR1234567890", 1000000),
    )
    source.executemany(
        "INSERT INTO trading_days VALUES (?,?,?,?,?,?,?)",
        [
            ("1001", "2026-11-17", 2400, 2550, 2350, 2500, 12000),
            ("1001", "2026-11-18", None, 0, None, 2600, 15000),
            ("1001", "2026-11-19", 2600, 2700, 2500, None, 16000),
        ],
    )
    source.commit()
    source.close()

    stat = os.stat(source_path)
    fingerprint = f"{stat.st_size}:{int(stat.st_mtime)}"
    side = sqlite3.connect(sidecar_path)
    side.executescript(
        """
        CREATE TABLE site_meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE site_watch (
            ins_code TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            company_name TEXT,
            market TEXT NOT NULL,
            total_company_shares INTEGER,
            latest_date TEXT,
            latest_date_jalali TEXT,
            latest_price REAL,
            latest_price_date TEXT
        );
        """
    )
    side.executemany(
        "INSERT INTO site_meta(key,value) VALUES (?,?)",
        [
            ("last_refresh", "2026-11-26T18:00:00"),
            ("sidecar_version", "10"),
            ("db_fingerprint", fingerprint),
        ],
    )
    side.executemany(
        """INSERT INTO site_watch
           (ins_code,symbol,company_name,market,total_company_shares,
            latest_date,latest_date_jalali,latest_price,latest_price_date)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        [
            ("1001", "فولاد", "فولاد مبارکه اصفهان", "TSE", 1000000,
             "2026-11-18", "1405-08-27", 2600, "2026-11-18"),
            ("1002", "فملی", "ملی صنایع مس ایران", "IFB", 2000000,
             "2026-11-18", "1405-08-27", 1800, "2026-11-18"),
            ("1003", "كگل", "معدنی و صنعتی گل گهر", "TSE", 3000000,
             "2026-11-18", "1405-08-27", 900, "2026-11-18"),
        ],
    )
    side.commit()
    side.close()

    return MarketDataRepository(
        str(source_path), str(sidecar_path), expected_sidecar_version="10"
    ), source_path, sidecar_path
