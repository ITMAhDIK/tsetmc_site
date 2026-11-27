"""Dedicated CRM PostgreSQL pool. Never connects to SaaS or Market databases."""
from __future__ import annotations
import os
from contextlib import asynccontextmanager
import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

_POOL: ConnectionPool | None = None

def _settings():
    try:
        lo=int(os.getenv("CRM_DB_POOL_MIN","1")); hi=int(os.getenv("CRM_DB_POOL_MAX","8")); timeout=float(os.getenv("CRM_DB_POOL_TIMEOUT_SECONDS","5"))
    except ValueError as exc: raise RuntimeError("CRM pool settings must be numeric") from exc
    if lo<1 or hi<lo or hi>100 or not .1<=timeout<=60: raise RuntimeError("Invalid CRM pool bounds")
    return lo,hi,timeout

def validate_production_configuration():
    if os.getenv("APP_ENV","development").lower() not in {"prod","production"}: return
    from psycopg.conninfo import conninfo_to_dict
    dsn=os.getenv("CRM_DATABASE_URL","").strip(); expected=os.getenv("CRM_EXPECTED_DATABASE_NAME","").strip()
    if not dsn or not expected: raise RuntimeError("CRM database URL and expected database name are required")
    params=conninfo_to_dict(dsn)
    if params.get("sslmode","prefer").lower()!="verify-full": raise RuntimeError("CRM_DATABASE_URL must use sslmode=verify-full")
    if expected in {os.getenv("SAAS_EXPECTED_DATABASE_NAME","").strip(),os.getenv("IDENTITY_EXPECTED_DATABASE_NAME","").strip(),os.getenv("COMMERCE_EXPECTED_DATABASE_NAME","").strip(),os.getenv("ANALYTICS_EXPECTED_DATABASE_NAME","").strip()}:
        raise RuntimeError("CRM must use its own dedicated database")
    if len(os.getenv("CRM_PROJECTION_TOKEN","").strip())<32: raise RuntimeError("CRM projection token must be at least 32 characters")
    if not os.getenv("IDENTITY_SERVICE_URL","").strip() or len(os.getenv("IDENTITY_SERVICE_TOKEN","").strip())<32: raise RuntimeError("CRM requires private Identity credentials")
    hosts=[x.strip() for x in os.getenv("CRM_TRUSTED_HOSTS","").split(",") if x.strip()]
    origins=[x.strip() for x in os.getenv("CRM_ALLOWED_ORIGINS","").split(",") if x.strip()]
    if not hosts or "*" in hosts or not origins: raise RuntimeError("CRM trusted hosts and allowed origins are required")
    for origin in origins:
        from urllib.parse import urlsplit
        p=urlsplit(origin)
        if p.scheme!="https" or not p.netloc or p.path or p.query or p.fragment: raise RuntimeError("CRM origins must be HTTPS origins")
    _settings()

@asynccontextmanager
async def lifespan_pool(app):
    global _POOL
    validate_production_configuration()
    dsn=os.getenv("CRM_DATABASE_URL","").strip()
    if not dsn:
        if os.getenv("APP_ENV","development").lower() in {"prod","production"}: raise RuntimeError("CRM_DATABASE_URL required")
        yield; return
    lo,hi,timeout=_settings()
    pool=ConnectionPool(dsn,min_size=lo,max_size=hi,timeout=timeout,kwargs={"row_factory":dict_row,"connect_timeout":5},open=False,name="tloomberg-crm")
    try:
        pool.open(wait=True,timeout=5)
        with pool.connection() as conn:
            actual=conn.execute("SELECT current_database() AS name").fetchone()["name"]
        expected=os.getenv("CRM_EXPECTED_DATABASE_NAME","").strip()
        if expected and actual!=expected: raise RuntimeError("Unexpected CRM database")
        _POOL=pool
        yield
    finally:
        _POOL=None; pool.close()

def get_conn():
    if _POOL is None: raise HTTPException(status_code=503,detail="CRM database unavailable")
    try:
        with _POOL.connection(timeout=_settings()[2]) as conn: yield conn
    except PoolTimeout: raise HTTPException(status_code=503,detail="CRM database pool exhausted") from None
    except psycopg.Error: raise HTTPException(status_code=503,detail="CRM database unavailable") from None
