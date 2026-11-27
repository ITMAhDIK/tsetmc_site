"""Dedicated Commerce PostgreSQL connection pool and dependency."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

_POOL: ConnectionPool | None = None


def pool_settings() -> tuple[int, int, float]:
    try:
        minimum = int(os.getenv("COMMERCE_DB_POOL_MIN", "1"))
        maximum = int(os.getenv("COMMERCE_DB_POOL_MAX", "8"))
        timeout = float(os.getenv("COMMERCE_DB_POOL_TIMEOUT_SECONDS", "5"))
    except ValueError as exc:
        raise RuntimeError("Commerce pool settings must be numeric") from exc
    if minimum < 1 or maximum < minimum or maximum > 100:
        raise RuntimeError("Commerce pool bounds must satisfy 1 <= min <= max <= 100")
    if not 0.1 <= timeout <= 60:
        raise RuntimeError("COMMERCE_DB_POOL_TIMEOUT_SECONDS must be between 0.1 and 60")
    return minimum, maximum, timeout


def lifespan_pool():
    """Return an async context manager so app startup owns the pool lifecycle."""
    @asynccontextmanager
    async def lifespan(_app):
        global _POOL
        dsn = os.getenv("COMMERCE_DATABASE_URL", "").strip()
        if not dsn:
            if os.getenv("APP_ENV", "development").strip().lower() in {"prod", "production"}:
                raise RuntimeError("COMMERCE_DATABASE_URL is required in production")
            yield
            return
        minimum, maximum, timeout = pool_settings()
        pool = ConnectionPool(
            conninfo=dsn, min_size=minimum, max_size=maximum, timeout=timeout,
            kwargs={"row_factory": dict_row, "connect_timeout": 5},
            open=False, name="tloomberg-commerce-api",
        )
        try:
            pool.open(wait=True, timeout=5)
            with pool.connection(timeout=timeout) as conn:
                actual = conn.execute("SELECT current_database() AS name").fetchone()["name"]
            expected = os.getenv("COMMERCE_EXPECTED_DATABASE_NAME", "").strip()
            if expected and actual != expected:
                raise RuntimeError("connected PostgreSQL database does not match COMMERCE_EXPECTED_DATABASE_NAME")
            _POOL = pool
            yield
        finally:
            _POOL = None
            pool.close()
    return lifespan


def get_conn():
    dsn = os.getenv("COMMERCE_DATABASE_URL", "").strip()
    if not dsn:
        raise HTTPException(status_code=503, detail="Commerce database is not configured")
    try:
        if _POOL is not None:
            _minimum, _maximum, timeout = pool_settings()
            with _POOL.connection(timeout=timeout) as conn:
                yield conn
            return
        if os.getenv("APP_ENV", "development").strip().lower() in {"prod", "production"}:
            raise HTTPException(status_code=503, detail="Commerce database pool is not ready")
        with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5) as conn:
            yield conn
    except (psycopg.OperationalError, PoolTimeout) as exc:
        raise HTTPException(status_code=503, detail="Commerce service is unavailable") from exc
