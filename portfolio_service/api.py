"""HTTP application for investor portfolio reconstruction."""
from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from portfolio_service.runtime import ReadOnlyPortfolioApi
from site_api.errors import ApiError

logger = logging.getLogger(__name__)


def validate_production_configuration() -> None:
    if os.getenv("APP_ENV", "development").strip().lower() not in {"prod", "production"}:
        return
    if not os.getenv("PORTFOLIO_TRUSTED_HOSTS", "").strip():
        raise RuntimeError("PORTFOLIO_TRUSTED_HOSTS must be explicit in production")
    origins = [v.strip().rstrip("/") for v in
               os.getenv("PORTFOLIO_ALLOWED_ORIGINS", "").split(",") if v.strip()]
    if not origins:
        raise RuntimeError("PORTFOLIO_ALLOWED_ORIGINS must be explicit in production")
    for origin in origins:
        parsed = urlsplit(origin)
        if (parsed.scheme != "https" or not parsed.netloc or parsed.path
                or parsed.query or parsed.fragment or parsed.username or parsed.password):
            raise RuntimeError("Portfolio production origins must be HTTPS origins")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    validate_production_configuration()
    yield


app = FastAPI(
    title="Tloomberg Portfolio Service", version="1.0.0",
    description=("Investor lookup, conservatively linked identities, current holdings, "
                 "historical portfolio valuation and disclosure history."),
    lifespan=lifespan,
)

trusted_hosts = [v.strip() for v in
                 os.getenv("PORTFOLIO_TRUSTED_HOSTS", "").split(",") if v.strip()]
if trusted_hosts:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)
allowed_origins = [v.strip().rstrip("/") for v in
                   os.getenv("PORTFOLIO_ALLOWED_ORIGINS", "").split(",") if v.strip()]
if allowed_origins:
    app.add_middleware(
        CORSMiddleware, allow_origins=allowed_origins, allow_credentials=False,
        allow_methods=["GET", "OPTIONS"], allow_headers=["Accept"], max_age=600,
    )


def get_runtime() -> ReadOnlyPortfolioApi:
    return ReadOnlyPortfolioApi(
        os.getenv("PORTFOLIO_SOURCE_DB_PATH", "/data/source/market.sqlite"),
        os.getenv("PORTFOLIO_SIDECAR_PATH", "/var/lib/tloomberg/site_data.sqlite"),
        site_dir=os.getenv("PORTFOLIO_SITE_DIR", "/app/docs"),
        sqlite_timeout=float(os.getenv("PORTFOLIO_SQLITE_TIMEOUT_SECONDS", "10")),
    )


def _query_dict(request: Request) -> dict[str, list[str]]:
    params: dict[str, list[str]] = {}
    for key, value in request.query_params.multi_items():
        params.setdefault(key, []).append(value)
    return params


def _execute(operation):
    try:
        return operation()
    except ApiError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from None
    except (sqlite3.Error, OSError):
        logger.exception("Portfolio query failed because a read store is unavailable")
        raise HTTPException(
            status_code=503,
            detail="سرویس پرتفوی موقتاً به داده‌های بازار دسترسی ندارد.",
        ) from None


@app.get("/healthz")
def health():
    return {"status": "ok", "service": "portfolio"}


@app.get("/readyz")
def ready(runtime: ReadOnlyPortfolioApi = Depends(get_runtime)):
    is_ready, reason = runtime.readiness()
    if not is_ready:
        raise HTTPException(status_code=503, detail=reason)
    return {"status": "ready", "service": "portfolio"}


@app.get("/api/v1/portfolios/investors")
def portfolio_investors(request: Request,
                        runtime: ReadOnlyPortfolioApi = Depends(get_runtime)):
    return _execute(lambda: runtime.portfolio_investors(_query_dict(request)))


@app.get("/api/v1/portfolios/{investor_id}/history")
def portfolio_history(investor_id: str, request: Request,
                      runtime: ReadOnlyPortfolioApi = Depends(get_runtime)):
    return _execute(lambda: runtime.portfolio_history(investor_id, _query_dict(request)))


@app.get("/api/v1/portfolios/{investor_id}")
def portfolio_detail(investor_id: str, request: Request,
                     runtime: ReadOnlyPortfolioApi = Depends(get_runtime)):
    return _execute(lambda: runtime.portfolio_detail(investor_id, _query_dict(request)))
