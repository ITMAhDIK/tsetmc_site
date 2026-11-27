"""Independent Subscription, Payment and Wallet service."""
from __future__ import annotations

import hmac
import os
from contextlib import asynccontextmanager
from uuid import UUID

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from commerce_service.db import get_conn, lifespan_pool
from commerce_service.identity import current_user
from commerce_service.payments.api import router as payments_router
from commerce_service.wallet.api import router as wallet_router


APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
TRUSTED_HOSTS = [v.strip() for v in os.getenv("COMMERCE_TRUSTED_HOSTS", "").split(",") if v.strip()]
ALLOWED_ORIGINS = [v.strip().rstrip("/") for v in os.getenv("COMMERCE_ALLOWED_ORIGINS", "").split(",") if v.strip()]


def validate_production_configuration() -> None:
    environment = os.getenv("APP_ENV", APP_ENV).strip().lower()
    if environment not in {"prod", "production"}:
        return
    from psycopg.conninfo import conninfo_to_dict
    from urllib.parse import urlsplit

    dsn = os.getenv("COMMERCE_DATABASE_URL", "").strip()
    expected = os.getenv("COMMERCE_EXPECTED_DATABASE_NAME", "").strip()
    if not dsn or not expected:
        raise RuntimeError("Commerce database URL and expected database name are required")
    params = conninfo_to_dict(dsn)
    if params.get("sslmode", "prefer").lower() != "verify-full":
        raise RuntimeError("COMMERCE_DATABASE_URL must require sslmode=verify-full")
    if len(os.getenv("COMMERCE_SERVICE_TOKEN", "").strip()) < 32:
        raise RuntimeError("COMMERCE_SERVICE_TOKEN must contain at least 32 characters")
    identity_url = os.getenv("IDENTITY_SERVICE_URL", "").strip()
    identity_token = os.getenv("IDENTITY_SERVICE_TOKEN", "").strip()
    if not identity_url or len(identity_token) < 32:
        raise RuntimeError("Identity private URL and service token are required")
    if hmac.compare_digest(identity_token, os.getenv("COMMERCE_SERVICE_TOKEN", "").strip()):
        raise RuntimeError("Identity and Commerce service tokens must be different")
    parsed = urlsplit(identity_url)
    if (parsed.scheme not in {"http", "https"} or not parsed.netloc
            or parsed.path not in {"", "/"} or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise RuntimeError("IDENTITY_SERVICE_URL must be a private HTTP(S) service origin")
    trusted_hosts = [
        value.strip() for value in os.getenv("COMMERCE_TRUSTED_HOSTS", "").split(",")
        if value.strip()
    ]
    allowed_origins = [
        value.strip().rstrip("/") for value in os.getenv("COMMERCE_ALLOWED_ORIGINS", "").split(",")
        if value.strip()
    ]
    if not trusted_hosts or "*" in trusted_hosts:
        raise RuntimeError("COMMERCE_TRUSTED_HOSTS must contain explicit hostnames")
    if not allowed_origins:
        raise RuntimeError("COMMERCE_ALLOWED_ORIGINS must contain explicit HTTPS origins")
    for origin in allowed_origins:
        parsed_origin = urlsplit(origin)
        if (parsed_origin.scheme != "https" or not parsed_origin.netloc
                or parsed_origin.path or parsed_origin.query or parsed_origin.fragment
                or parsed_origin.username or parsed_origin.password):
            raise RuntimeError("Commerce production origins must be HTTPS origins without paths")
    minimum = int(os.getenv("COMMERCE_DB_POOL_MIN", "1"))
    maximum = int(os.getenv("COMMERCE_DB_POOL_MAX", "8"))
    timeout = float(os.getenv("COMMERCE_DB_POOL_TIMEOUT_SECONDS", "5"))
    if minimum < 1 or maximum < minimum or maximum > 100 or not 0.1 <= timeout <= 60:
        raise RuntimeError("Invalid Commerce database pool bounds")


@asynccontextmanager
async def app_lifespan(app: FastAPI):
    validate_production_configuration()
    async with lifespan_pool()(app):
        yield


app = FastAPI(title="Tloomberg Subscription & Payment Service", version="1.0.0", lifespan=app_lifespan)
if TRUSTED_HOSTS:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=TRUSTED_HOSTS)
if ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
        max_age=600,
    )


@app.get("/health")
def health():
    return {"status": "ok", "service": "commerce"}


@app.get("/ready")
def ready(conn=Depends(get_conn)):
    try:
        name = conn.execute("SELECT current_database() AS name").fetchone()["name"]
        expected = os.getenv("COMMERCE_EXPECTED_DATABASE_NAME", "").strip()
        if expected and name != expected:
            raise HTTPException(status_code=503, detail="unexpected Commerce database")
        row = conn.execute(
            """SELECT to_regclass('public.payment_orders') IS NOT NULL AS orders,
                      to_regclass('public.wallet_ledger_entries') IS NOT NULL AS ledger,
                      to_regclass('public.subscriptions') IS NOT NULL AS subscriptions"""
        ).fetchone()
        if not all(row.values()):
            raise HTTPException(status_code=503, detail="Commerce schema is not ready")
    except psycopg.Error:
        raise HTTPException(status_code=503, detail="Commerce database is unavailable") from None
    return {"status": "ready", "service": "commerce"}


@app.get("/plans")
def plans(conn=Depends(get_conn)):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT code,title,duration_days,price_toman,currency
               FROM plans WHERE active=TRUE ORDER BY duration_days,price_toman"""
        )
        return {"plans": cur.fetchall()}


@app.get("/subscriptions")
def my_subscriptions(principal=Depends(current_user), conn=Depends(get_conn)):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT s.id,s.status,s.starts_at,s.ends_at,s.created_at,
                      p.code AS plan_code,p.title AS plan_title,p.duration_days,p.price_toman
               FROM subscriptions s JOIN plans p ON p.id=s.plan_id
               WHERE s.user_id=%s ORDER BY s.created_at DESC,s.id DESC""",
            (principal["id"],),
        )
        return {"subscriptions": cur.fetchall()}


def _service_auth(x_commerce_service_token: str | None = Header(default=None, alias="X-Commerce-Service-Token")):
    expected = os.getenv("COMMERCE_SERVICE_TOKEN", "").strip()
    if len(expected) < 32 or not x_commerce_service_token or not hmac.compare_digest(expected, x_commerce_service_token):
        raise HTTPException(status_code=401, detail="Commerce service authentication required")


@app.get("/internal/users/{user_id}/subscriptions", dependencies=[Depends(_service_auth)])
def internal_subscriptions(user_id: UUID, conn=Depends(get_conn)):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT s.id,s.status,s.starts_at,s.ends_at,s.created_at,
                      p.code AS plan_code,p.title AS plan_title,p.duration_days,p.price_toman
               FROM subscriptions s JOIN plans p ON p.id=s.plan_id
               WHERE s.user_id=%s ORDER BY s.created_at DESC,s.id DESC""",
            (user_id,),
        )
        return {"subscriptions": cur.fetchall()}


@app.get("/internal/users/{user_id}/entitlements", dependencies=[Depends(_service_auth)])
def internal_entitlements(user_id: UUID, conn=Depends(get_conn)):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT EXISTS(
                   SELECT 1 FROM subscriptions s JOIN plans p ON p.id=s.plan_id
                   WHERE s.user_id=%s AND s.status='active'
                     AND s.starts_at<=now() AND s.ends_at>now()
                     AND p.code IN ('premium_monthly','premium_quarterly')
               ) AS premium""",
            (user_id,),
        )
        premium = bool(cur.fetchone()["premium"])
    return {"entitlements": {
        "premium_analytics": premium,
        "ownership_graphs": premium,
        "investor_history": premium,
    }}


app.include_router(payments_router)
app.include_router(wallet_router)
