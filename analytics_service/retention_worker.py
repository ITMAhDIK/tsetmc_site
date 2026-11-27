"""Scheduled privacy-retention worker for consented Analytics events."""
from __future__ import annotations

import logging
import os
import time

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from analytics_service.service import DEFAULT_RETENTION_DAYS, purge_expired_events

LOG = logging.getLogger("tloomberg.analytics.retention")


def validate_retention_connection(dsn: str) -> None:
    """Require verified TLS whenever the worker connects to remote PostgreSQL."""
    try:
        params = conninfo_to_dict(dsn)
    except Exception as exc:
        raise RuntimeError("Analytics retention database URL is invalid") from exc
    hosts = [
        value.strip().lower()
        for value in params.get("host", "localhost").split(",")
        if value.strip()
    ]
    verify_full = params.get("sslmode", "prefer").lower() == "verify-full"
    if os.getenv("APP_ENV", "development").strip().lower() in {"prod", "production"} and not verify_full:
        raise RuntimeError("Production Analytics retention connections require sslmode=verify-full")
    if any(host not in {"localhost", "127.0.0.1", "::1"} for host in hosts):
        if not verify_full:
            raise RuntimeError(
                "Remote Analytics retention connections require sslmode=verify-full"
            )


def retention_settings() -> tuple[int, int]:
    days = int(os.getenv("SAAS_ANALYTICS_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS)))
    interval = int(os.getenv("ANALYTICS_RETENTION_POLL_SECONDS", "3600"))
    if not 30 <= days <= 730:
        raise ValueError("SAAS_ANALYTICS_RETENTION_DAYS must be between 30 and 730")
    if not 60 <= interval <= 86400:
        raise ValueError("ANALYTICS_RETENTION_POLL_SECONDS must be between 60 and 86400")
    return days, interval


def run_once(conn, *, retention_days: int | None = None) -> int:
    """Use a transaction-scoped advisory lock to prevent duplicate worker runs."""
    days = retention_days if retention_days is not None else retention_settings()[0]
    with conn.transaction():
        lock = conn.execute(
            "SELECT pg_try_advisory_xact_lock(20261127, 7007) AS acquired"
        ).fetchone()
        if not lock["acquired"]:
            return 0
        return purge_expired_events(conn, retention_days=days)


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    dsn = os.getenv("ANALYTICS_DATABASE_URL", "").strip()
    expected = os.getenv("ANALYTICS_EXPECTED_DATABASE_NAME", "").strip()
    if not dsn or not expected:
        raise RuntimeError("Analytics database URL and expected database name are required")
    if expected in {
        os.getenv("SAAS_EXPECTED_DATABASE_NAME", "").strip(),
        os.getenv("IDENTITY_EXPECTED_DATABASE_NAME", "").strip(),
        os.getenv("COMMERCE_EXPECTED_DATABASE_NAME", "").strip(),
    }:
        raise RuntimeError("Analytics retention worker requires a dedicated Analytics database")
    validate_retention_connection(dsn)
    days, interval = retention_settings()
    while True:
        try:
            with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5) as conn:
                actual = conn.execute("SELECT current_database() AS name").fetchone()["name"]
                if actual != expected:
                    raise RuntimeError("retention worker connected to an unexpected database")
                deleted = run_once(conn, retention_days=days)
                LOG.info("analytics_retention_purge completed deleted_events=%s retention_days=%s", deleted, days)
        except Exception:
            LOG.exception("analytics_retention_purge failed")
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
