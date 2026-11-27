"""Read-only runtime for the independently deployed Screener service."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from server import SiteApi, _norm_date
from site_api.errors import ApiError
from screener_service.service import ScreenerService


class ReadOnlyScreenerRuntime:
    """Serve screener queries from the materialized Sidecar projection only.

    Screening is computed from the refresh worker's Sidecar read model. The
    service does not open the source holdings database or any SaaS database.
    """

    REQUIRED_SIDECAR_TABLES = {
        "site_corp_action_counts",
        "site_holder_history",
        "site_holder_latest",
        "site_meta",
        "site_shareholder_names",
        "site_watch",
    }

    def __init__(self, sidecar_path: str, *, sqlite_timeout: float = 10.0):
        self.sidecar_path = str(sidecar_path)
        self.sqlite_timeout = float(sqlite_timeout)
        self._refresh_error = None
        if self.sqlite_timeout <= 0 or self.sqlite_timeout > 120:
            raise ValueError("sqlite_timeout must be between 0 and 120 seconds")
        self.service = ScreenerService(
            sidecar=self.sidecar,
            require_ready=self.require_ready,
            norm_date=_norm_date,
        )

    @staticmethod
    def _readonly_uri(path: str) -> str:
        return Path(path).resolve().as_uri() + "?mode=ro"

    def sidecar(self):
        """Open the projection in SQLite URI read-only mode."""
        if not self.sidecar_path or not os.path.isfile(self.sidecar_path):
            raise ApiError(503, "نمای داده‌ی غربال‌گری هنوز در دسترس نیست.")
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
            raise ApiError(503, "پایگاه داده‌ی غربال‌گری قابل خواندن نیست.") from None

    def meta(self, conn, key, default=None):
        row = conn.execute(
            "SELECT value FROM site_meta WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else default

    def require_ready(self, conn):
        # Reuse the canonical readiness contract (refresh marker, layout
        # version, ownership resolver schema and historical ledger).
        return SiteApi.require_ready(self, conn)

    @staticmethod
    def _table_names(conn) -> set[str]:
        return {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

    def readiness(self) -> tuple[bool, str]:
        conn = None
        try:
            conn = self.sidecar()
            self.require_ready(conn)
            missing = self.REQUIRED_SIDECAR_TABLES - self._table_names(conn)
            if missing:
                return False, "screener projection schema is incomplete"
        except ApiError as exc:
            return False, exc.detail
        except (sqlite3.Error, OSError):
            return False, "screener projection is unavailable"
        finally:
            if conn is not None:
                conn.close()
        return True, "ready"

    def screener_query(self, body: dict):
        return self.service.screener_query(body)

    def screener_export(self, body: dict):
        return self.service.screener_export(body)
