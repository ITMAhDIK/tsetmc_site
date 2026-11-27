"""Read-only repository for TSETMC instruments and daily OHLCV data.

The market holdings SQLite file remains the source of truth. The Sidecar is
consumed strictly read-only and is used only for instrument-name lookup and
its refresh/readiness metadata. No identity, billing, or SaaS database is read.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import date
from pathlib import Path
from urllib.parse import quote

from .dates import (
    gregorian_to_jalali_date, normalize_gregorian_date, normalize_symbol,
)


class MarketDataUnavailable(RuntimeError):
    """The source database or its materialized lookup projection is not ready."""


class InstrumentNotFound(LookupError):
    """The requested instrument does not exist in the current market projection."""


class MarketDataRepository:
    def __init__(
        self,
        source_db_path: str,
        sidecar_path: str,
        *,
        expected_sidecar_version: str = "10",
        sqlite_timeout: float = 5.0,
    ):
        self.source_db_path = str(source_db_path or "").strip()
        self.sidecar_path = str(sidecar_path or "").strip()
        self.expected_sidecar_version = str(expected_sidecar_version)
        self.sqlite_timeout = float(sqlite_timeout)

    def _connect_readonly(self, path: str):
        if not path or not os.path.isfile(path):
            raise MarketDataUnavailable("Market data store is not mounted")
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        try:
            conn = sqlite3.connect(
                uri, uri=True, timeout=self.sqlite_timeout,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            return conn
        except sqlite3.Error:
            raise MarketDataUnavailable("Market data store cannot be opened read-only") from None

    def source(self):
        return self._connect_readonly(self.source_db_path)

    def sidecar(self):
        return self._connect_readonly(self.sidecar_path)

    @staticmethod
    def _table_exists(conn, table_name: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        return row is not None

    @staticmethod
    def _metadata(conn) -> dict[str, str]:
        if not MarketDataRepository._table_exists(conn, "site_meta"):
            return {}
        return {
            str(row["key"]): str(row["value"] or "")
            for row in conn.execute("SELECT key,value FROM site_meta").fetchall()
        }

    def readiness(self) -> tuple[bool, str]:
        if not self.source_db_path or not os.path.isfile(self.source_db_path):
            return False, "source database is not mounted"
        if not self.sidecar_path or not os.path.isfile(self.sidecar_path):
            return False, "instrument projection is not mounted"
        try:
            source = self.source()
            sidecar = self.sidecar()
            try:
                source_tables = {
                    row["name"] for row in source.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                if not {"companies", "trading_days"}.issubset(source_tables):
                    return False, "source database schema is incomplete"
                if not self._table_exists(sidecar, "site_watch"):
                    return False, "instrument projection is incomplete"
                meta = self._metadata(sidecar)
                if not meta.get("last_refresh"):
                    return False, "instrument projection has not completed a refresh"
                if meta.get("sidecar_version") != self.expected_sidecar_version:
                    return False, "instrument projection version is incompatible"
                if not meta.get("db_fingerprint"):
                    return False, "instrument projection has no source fingerprint"
                stat = os.stat(self.source_db_path)
                actual_fingerprint = f"{stat.st_size}:{int(stat.st_mtime)}"
                if meta["db_fingerprint"] != actual_fingerprint:
                    return False, "instrument projection is stale relative to source database"
            finally:
                source.close()
                sidecar.close()
        except (sqlite3.Error, OSError, MarketDataUnavailable):
            return False, "market data stores are unavailable"
        return True, "ready"

    def require_ready(self):
        ready, reason = self.readiness()
        if not ready:
            raise MarketDataUnavailable(reason)

    @staticmethod
    def _validate_market(market: str) -> str:
        value = str(market or "ALL").strip().upper()
        if value not in {"ALL", "TSE", "IFB", "UNKNOWN"}:
            raise ValueError("market must be one of ALL, TSE, IFB or UNKNOWN")
        return value

    @staticmethod
    def _validate_date(value: str | None, field_name: str):
        if value is None or str(value).strip() == "":
            return None
        raw = str(value).strip().replace("/", "-")
        if len(raw) == 8 and raw.isdigit():
            raw = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
        try:
            return date.fromisoformat(raw).isoformat()
        except ValueError:
            raise ValueError(f"{field_name} must be a Gregorian YYYY-MM-DD date") from None

    def search_instruments(self, *, q: str = "", market: str = "ALL",
                           limit: int = 8, offset: int = 0):
        if isinstance(limit, bool) or not 1 <= int(limit) <= 100:
            raise ValueError("limit must be between 1 and 100")
        if isinstance(offset, bool) or not 0 <= int(offset) <= 1_000_000:
            raise ValueError("offset must be between 0 and 1000000")
        market = self._validate_market(market)
        query = normalize_symbol(q)
        clauses, args = [], []
        if market != "ALL":
            clauses.append("market=?")
            args.append(market)
        if query:
            clauses.append(
                "(replace(replace(symbol,'ي','ی'),'ك','ک') LIKE ? "
                "OR replace(replace(company_name,'ي','ی'),'ك','ک') LIKE ?)"
            )
            args.extend([f"%{query}%", f"%{query}%"])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        conn = self.sidecar()
        try:
            self.require_ready()
            total = conn.execute(
                "SELECT COUNT(*) FROM site_watch" + where, args
            ).fetchone()[0]
            rows = conn.execute(
                """SELECT ins_code,symbol,company_name,market
                   FROM site_watch""" + where +
                " ORDER BY symbol,ins_code LIMIT ? OFFSET ?",
                args + [int(limit), int(offset)],
            ).fetchall()
            return {
                "total": int(total),
                "rows": [dict(row) for row in rows],
            }
        finally:
            conn.close()

    def _resolve_instrument_row(self, sidecar, symbol: str):
        query = str(symbol or "").strip()
        row = sidecar.execute(
            "SELECT * FROM site_watch WHERE symbol=? ORDER BY ins_code LIMIT 1",
            (query,),
        ).fetchone()
        if row is not None:
            return row
        normalized = normalize_symbol(query)
        row = sidecar.execute(
            """SELECT * FROM site_watch
               WHERE replace(replace(symbol,'ي','ی'),'ك','ک')=?
               ORDER BY ins_code LIMIT 1""",
            (normalized,),
        ).fetchone()
        return row

    def instrument_detail(self, symbol: str):
        self.require_ready()
        sidecar = self.sidecar()
        try:
            row = self._resolve_instrument_row(sidecar, symbol)
            if row is None:
                raise InstrumentNotFound(f"instrument not found: {symbol}")
            instrument = {
                "ins_code": row["ins_code"],
                "symbol": row["symbol"],
                "company_name": row["company_name"],
                "market": row["market"],
            }
        finally:
            sidecar.close()

        source = self.source()
        try:
            company = source.execute(
                """SELECT cisin,total_shares_current
                   FROM companies WHERE ins_code=? LIMIT 1""",
                (instrument["ins_code"],),
            ).fetchone()
            latest = source.execute(
                """SELECT date,p_closing,volume
                   FROM trading_days
                   WHERE ins_code=? AND p_closing IS NOT NULL
                   ORDER BY date DESC LIMIT 1""",
                (instrument["ins_code"],),
            ).fetchone()
        finally:
            source.close()

        instrument["cisin"] = company["cisin"] if company else None
        instrument["total_shares"] = (
            company["total_shares_current"] if company else None
        )
        last_trade = None
        if latest:
            last_trade = {
                "date": normalize_gregorian_date(latest["date"]),
                "date_jalali": gregorian_to_jalali_date(latest["date"]),
                "close": latest["p_closing"],
                "volume": latest["volume"],
            }
        return {"instrument": instrument, "last_trade": last_trade}

    def daily_prices(self, symbol: str, *, start_date: str | None = None,
                     end_date: str | None = None, limit: int = 20000):
        if isinstance(limit, bool) or not 1 <= int(limit) <= 20000:
            raise ValueError("limit must be between 1 and 20000")
        start = self._validate_date(start_date, "start_date")
        end = self._validate_date(end_date, "end_date")
        if start and end and start > end:
            raise ValueError("start_date must be less than or equal to end_date")

        self.require_ready()
        sidecar = self.sidecar()
        try:
            row = self._resolve_instrument_row(sidecar, symbol)
            if row is None:
                raise InstrumentNotFound(f"instrument not found: {symbol}")
            ins_code = row["ins_code"]
        finally:
            sidecar.close()

        clauses = ["ins_code=?", "p_closing IS NOT NULL"]
        args = [ins_code]
        if start:
            clauses.append("date>=?")
            args.append(start)
        if end:
            clauses.append("date<=?")
            args.append(end)

        source = self.source()
        try:
            rows = source.execute(
                """SELECT date,p_first,p_max,p_min,p_closing,volume
                   FROM trading_days WHERE """ + " AND ".join(clauses) +
                " ORDER BY date DESC LIMIT ?",
                args + [int(limit)],
            ).fetchall()
        finally:
            source.close()

        prices = []
        for row in reversed(rows):
            close = row["p_closing"]
            prices.append({
                "time": normalize_gregorian_date(row["date"]),
                "date_jalali": gregorian_to_jalali_date(row["date"]),
                "open": row["p_first"] if row["p_first"] is not None else close,
                "high": row["p_max"] if row["p_max"] not in (None, 0) else close,
                "low": row["p_min"] if row["p_min"] not in (None, 0) else close,
                "close": close,
                "volume": row["volume"],
            })
        return {"prices": prices}
