"""Read-only composition root for the Ownership microservice.

The domain implementation lives in site_api.ownership_service. This adapter
gives it an independent process and enforces read-only SQLite access without
calling SiteApi's writable Sidecar or refresh methods.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from server import SiteApi, open_live_db
from site_api.errors import ApiError


class ReadOnlyOwnershipApi(SiteApi):
    """Compose OwnershipService over read-only source and Sidecar handles."""

    REQUIRED_SOURCE_TABLES = {
        "companies", "company_daily", "shareholder_holdings",
        "shareholder_id_xref", "shareholders", "trading_days",
    }
    REQUIRED_SIDECAR_TABLES = {
        "site_company_daily_dedup", "site_excluded_holder_rows",
        "site_holder_history", "site_holder_latest", "site_meta",
        "site_shareholder_names",
    }

    def __init__(
        self,
        db_path: str,
        sidecar_path: str,
        site_dir: str = "/app/docs",
        output_dir: str = "/tmp/ownership",
        *,
        sqlite_timeout: float = 5.0,
    ):
        self.sqlite_timeout = float(sqlite_timeout)
        if self.sqlite_timeout <= 0:
            raise ValueError("sqlite_timeout must be positive")
        super().__init__(db_path, sidecar_path, site_dir, output_dir)

    @staticmethod
    def _readonly_uri(path: str) -> str:
        return Path(path).resolve().as_uri() + "?mode=ro"

    def sidecar(self):
        """Open Sidecar in URI read-only mode and prohibit SQL writes."""
        if not self.sidecar_path or not os.path.isfile(self.sidecar_path):
            raise ApiError(503, "داده‌های مالکیت هنوز در دسترس نیستند.")
        try:
            conn = sqlite3.connect(
                self._readonly_uri(self.sidecar_path),
                uri=True,
                timeout=self.sqlite_timeout,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            conn.execute("PRAGMA busy_timeout = %d" % int(self.sqlite_timeout * 1000))
            conn.execute("PRAGMA cache_size = -65536")
            return conn
        except sqlite3.Error:
            raise ApiError(503, "پایگاه داده‌ی نمای مالکیت قابل خواندن نیست.") from None

    def live(self):
        """Open source data read-only and attach the Sidecar read-only."""
        if not self.db_path or not os.path.isfile(self.db_path):
            raise ApiError(503, "پایگاه داده‌ی منبع مالکیت در دسترس نیست.")
        if not self.sidecar_path or not os.path.isfile(self.sidecar_path):
            raise ApiError(503, "نمای مالکیت هنوز ساخته نشده است.")
        conn = None
        try:
            conn = open_live_db(self.db_path)
            conn.execute(
                "ATTACH DATABASE ? AS sidecar_data",
                (self._readonly_uri(self.sidecar_path),),
            )
            conn.execute("PRAGMA query_only = ON")
            conn.execute("PRAGMA busy_timeout = %d" % int(self.sqlite_timeout * 1000))
            return conn
        except (sqlite3.Error, OSError):
            if conn is not None:
                conn.close()
            raise ApiError(503, "پایگاه داده‌ی منبع مالکیت قابل خواندن نیست.") from None

    @staticmethod
    def _table_names(conn) -> set[str]:
        return {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

    def readiness(self) -> tuple[bool, str]:
        """Validate completed Ownership projections and required source schema."""
        sidecar = None
        live = None
        try:
            sidecar = self.sidecar()
            self.require_ready(sidecar)
            if self.REQUIRED_SIDECAR_TABLES - self._table_names(sidecar):
                return False, "ownership projection schema is incomplete"
            live = self.live()
            if self.REQUIRED_SOURCE_TABLES - self._table_names(live):
                return False, "ownership source schema is incomplete"
        except ApiError as exc:
            return False, exc.detail
        except (sqlite3.Error, OSError):
            return False, "ownership stores are unavailable"
        finally:
            if live is not None:
                live.close()
            if sidecar is not None:
                sidecar.close()
        return True, "ready"
