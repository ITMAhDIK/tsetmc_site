"""Dedicated Notification PostgreSQL pool."""
from __future__ import annotations
import os
from contextlib import asynccontextmanager
import psycopg
from fastapi import HTTPException
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool,PoolTimeout
_POOL=None
def _settings():
 try: lo=int(os.getenv("NOTIFICATION_DB_POOL_MIN","1")); hi=int(os.getenv("NOTIFICATION_DB_POOL_MAX","8")); timeout=float(os.getenv("NOTIFICATION_DB_POOL_TIMEOUT_SECONDS","5"))
 except ValueError as e: raise RuntimeError("Notification pool settings must be numeric") from e
 if lo<1 or hi<lo or hi>100 or not .1<=timeout<=60: raise RuntimeError("Invalid Notification pool bounds")
 return lo,hi,timeout
def validate_production_configuration():
 if os.getenv("APP_ENV","development").lower() not in {"prod","production"}: return
 dsn=os.getenv("NOTIFICATION_DATABASE_URL","").strip(); expected=os.getenv("NOTIFICATION_EXPECTED_DATABASE_NAME","").strip()
 if not dsn or not expected: raise RuntimeError("Notification database URL and expected database name are required")
 params=conninfo_to_dict(dsn)
 if params.get("sslmode","prefer").lower()!="verify-full": raise RuntimeError("Notification database requires sslmode=verify-full")
 names=[os.getenv(x,"").strip() for x in ("SAAS_EXPECTED_DATABASE_NAME","IDENTITY_EXPECTED_DATABASE_NAME","COMMERCE_EXPECTED_DATABASE_NAME","ANALYTICS_EXPECTED_DATABASE_NAME","CRM_EXPECTED_DATABASE_NAME")]
 if expected in names: raise RuntimeError("Notification must use its own dedicated database")
 for env in ("NOTIFICATION_INGEST_TOKEN","IDENTITY_SERVICE_TOKEN","NOTIFICATION_FERNET_KEY"):
  if len(os.getenv(env,"").strip())<32: raise RuntimeError(f"{env} must contain at least 32 characters")
 if not os.getenv("IDENTITY_SERVICE_URL","").strip(): raise RuntimeError("Identity service URL is required")
 hosts=[x.strip() for x in os.getenv("NOTIFICATION_TRUSTED_HOSTS","").split(",") if x.strip()]
 if not hosts or "*" in hosts: raise RuntimeError("Explicit Notification trusted hosts are required")
 _settings()
@asynccontextmanager
async def lifespan_pool(app):
 global _POOL
 validate_production_configuration(); dsn=os.getenv("NOTIFICATION_DATABASE_URL","").strip()
 if not dsn:
  if os.getenv("APP_ENV","development").lower() in {"prod","production"}: raise RuntimeError("NOTIFICATION_DATABASE_URL required")
  yield; return
 lo,hi,timeout=_settings(); pool=ConnectionPool(dsn,min_size=lo,max_size=hi,timeout=timeout,kwargs={"row_factory":dict_row,"connect_timeout":5},open=False,name="tloomberg-notifications")
 try:
  pool.open(wait=True,timeout=5)
  actual=pool.getconn().execute("SELECT current_database() AS name").fetchone()["name"]; pool.putconn(pool.getconn()) if False else None
  expected=os.getenv("NOTIFICATION_EXPECTED_DATABASE_NAME","").strip()
  if expected and actual!=expected: raise RuntimeError("Unexpected Notification database")
  _POOL=pool; yield
 finally: _POOL=None; pool.close()
def get_conn():
 if _POOL is None: raise HTTPException(503,detail="Notification database unavailable")
 try:
  with _POOL.connection(timeout=_settings()[2]) as conn: yield conn
 except PoolTimeout: raise HTTPException(503,detail="Notification database pool exhausted") from None
 except psycopg.Error: raise HTTPException(503,detail="Notification database unavailable") from None
