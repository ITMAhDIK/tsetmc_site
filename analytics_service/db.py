"""Dedicated Analytics PostgreSQL pool; no connection to SaaS or market stores."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import psycopg
from fastapi import HTTPException
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

_POOL: ConnectionPool | None = None


def pool_settings() -> tuple[int, int, float]:
    try:
        minimum = int(os.getenv("ANALYTICS_DB_POOL_MIN", "1"))
        maximum = int(os.getenv("ANALYTICS_DB_POOL_MAX", "8"))
        timeout = float(os.getenv("ANALYTICS_DB_POOL_TIMEOUT_SECONDS", "5"))
    except ValueError as exc:
        raise RuntimeError("Analytics pool settings must be numeric") from exc
    if minimum < 1 or maximum < minimum or maximum > 100:
        raise RuntimeError("Analytics pool bounds must satisfy 1 <= min <= max <= 100")
    if not 0.1 <= timeout <= 60:
        raise RuntimeError("ANALYTICS_DB_POOL_TIMEOUT_SECONDS must be between 0.1 and 60")
    return minimum, maximum, timeout


def validate_production_configuration() -> None:
    environment = os.getenv("APP_ENV", "development").strip().lower()
    if environment not in {"prod", "production"}:
        return
    dsn = os.getenv("ANALYTICS_DATABASE_URL", "").strip()
    expected = os.getenv("ANALYTICS_EXPECTED_DATABASE_NAME", "").strip()
    if not dsn or not expected:
        raise RuntimeError("Analytics database URL and expected database name are required")
    params = conninfo_to_dict(dsn)
    if params.get("sslmode", "prefer").lower() != "verify-full":
        raise RuntimeError("ANALYTICS_DATABASE_URL must require sslmode=verify-full in production")
    if expected in {
        os.getenv("SAAS_EXPECTED_DATABASE_NAME", "").strip(),
        os.getenv("IDENTITY_EXPECTED_DATABASE_NAME", "").strip(),
        os.getenv("COMMERCE_EXPECTED_DATABASE_NAME", "").strip(),
    }:
        raise RuntimeError("Analytics must use a database dedicated to product analytics")
    identity_url = os.getenv("IDENTITY_SERVICE_URL", "").strip()
    identity_token = os.getenv("IDENTITY_SERVICE_TOKEN", "").strip()
    parsed = urlsplit(identity_url)
    if (parsed.scheme not in {"http", "https"} or not parsed.netloc
            or parsed.path not in {"", "/"} or parsed.username or parsed.password
            or parsed.query or parsed.fragment or len(identity_token) < 32):
        raise RuntimeError("Analytics requires a valid private Identity URL and service token")
    hosts = [v.strip() for v in os.getenv("ANALYTICS_TRUSTED_HOSTS", "").split(",") if v.strip()]
    origins = [v.strip().rstrip("/") for v in os.getenv("ANALYTICS_ALLOWED_ORIGINS", "").split(",") if v.strip()]
    if not hosts or "*" in hosts or not origins:
        raise RuntimeError("Analytics requires explicit trusted hosts and allowed origins")
    for origin in origins:
        parsed_origin = urlsplit(origin)
        if (parsed_origin.scheme != "https" or not parsed_origin.netloc
                or parsed_origin.path or parsed_origin.query or parsed_origin.fragment
                or parsed_origin.username or parsed_origin.password):
            raise RuntimeError("Analytics allowed origins must be HTTPS origins without paths")
    try:
        event_limit = int(os.getenv("ANALYTICS_EVENTS_PER_MINUTE", "600"))
    except ValueError as exc:
        raise RuntimeError("ANALYTICS_EVENTS_PER_MINUTE must be an integer") from exc
    if not 60 <= event_limit <= 10_000:
        raise RuntimeError("ANALYTICS_EVENTS_PER_MINUTE must be between 60 and 10000")
    pool_settings()


@asynccontextmanager
async def lifespan_pool(_app):
    global _POOL
    validate_production_configuration()
    dsn = os.getenv("ANALYTICS_DATABASE_URL", "").strip()
    if not dsn:
        if os.getenv("APP_ENV", "development").strip().lower() in {"prod", "production"}:
            raise RuntimeError("ANALYTICS_DATABASE_URL is required in production")
        yield
        return
    minimum, maximum, timeout = pool_settings()
    pool = ConnectionPool(
        conninfo=dsn, min_size=minimum, max_size=maximum, timeout=timeout,
        kwargs={"row_factory": dict_row, "connect_timeout": 5},
        open=False, name="tloomberg-analytics-api",
    )
    try:
        pool.open(wait=True, timeout=5)
        with pool.connection(timeout=timeout) as conn:
            actual = conn.execute("SELECT current_database() AS name").fetchone()["name"]
        expected = os.getenv("ANALYTICS_EXPECTED_DATABASE_NAME", "").strip()
        if expected and actual != expected:
            raise RuntimeError("connected database does not match ANALYTICS_EXPECTED_DATABASE_NAME")
        _POOL = pool
        yield
    finally:
        _POOL = None
        pool.close()


def get_conn():
    dsn = os.getenv("ANALYTICS_DATABASE_URL", "").strip()
    if not dsn:
        raise HTTPException(status_code=503, detail="Analytics database is not configured")
    try:
        if _POOL is not None:
            _minimum, _maximum, timeout = pool_settings()
            with _POOL.connection(timeout=timeout) as conn:
                yield conn
            return
        if os.getenv("APP_ENV", "development").strip().lower() in {"prod", "production"}:
            raise HTTPException(status_code=503, detail="Analytics database pool is not ready")
        with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5) as conn:
            yield conn
    except (psycopg.OperationalError, PoolTimeout) as exc:
        raise HTTPException(status_code=503, detail="Analytics service is unavailable") from exc
