"""FastAPI application for shareholder ownership and change history."""
from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from ownership_service.runtime import ReadOnlyOwnershipApi
from site_api.errors import ApiError

logger = logging.getLogger(__name__)


def validate_production_configuration() -> None:
    if os.getenv("APP_ENV", "development").strip().lower() not in {"prod", "production"}:
        return
    if not os.getenv("OWNERSHIP_TRUSTED_HOSTS", "").strip():
        raise RuntimeError("OWNERSHIP_TRUSTED_HOSTS must be explicit in production")
    origins = [
        value.strip().rstrip("/")
        for value in os.getenv("OWNERSHIP_ALLOWED_ORIGINS", "").split(",")
        if value.strip()
    ]
    if not origins:
        raise RuntimeError("OWNERSHIP_ALLOWED_ORIGINS must be explicit in production")
    for origin in origins:
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "https" or not parsed.netloc or parsed.path
            or parsed.query or parsed.fragment or parsed.username or parsed.password
        ):
            raise RuntimeError("Ownership production origins must be HTTPS origins")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    validate_production_configuration()
    yield


app = FastAPI(
    title="Tloomberg Ownership Service",
    version="1.0.0",
    description=(
        "Read-only shareholder snapshots, ownership resolution, "
        "cross-company holder search and shareholder change history."
    ),
    lifespan=lifespan,
)

trusted_hosts = [
    value.strip()
    for value in os.getenv("OWNERSHIP_TRUSTED_HOSTS", "").split(",")
    if value.strip()
]
if trusted_hosts:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)

allowed_origins = [
    value.strip().rstrip("/")
    for value in os.getenv("OWNERSHIP_ALLOWED_ORIGINS", "").split(",")
    if value.strip()
]
if allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "OPTIONS"],
        allow_headers=["Accept"],
        max_age=600,
    )


def get_runtime() -> ReadOnlyOwnershipApi:
    return ReadOnlyOwnershipApi(
        os.getenv("OWNERSHIP_SOURCE_DB_PATH", "/data/source/market.sqlite"),
        os.getenv("OWNERSHIP_SIDECAR_PATH", "/var/lib/tloomberg/site_data.sqlite"),
        sqlite_timeout=float(os.getenv("OWNERSHIP_SQLITE_TIMEOUT_SECONDS", "5")),
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
        logger.exception("Ownership query failed because a read store is unavailable")
        raise HTTPException(
            status_code=503,
            detail="سرویس مالکیت موقتاً به داده‌های بازار دسترسی ندارد.",
        ) from None


@app.get("/healthz")
def health():
    return {"status": "ok", "service": "ownership"}


@app.get("/readyz")
def ready(runtime: ReadOnlyOwnershipApi = Depends(get_runtime)):
    is_ready, reason = runtime.readiness()
    if not is_ready:
        raise HTTPException(status_code=503, detail=reason)
    return {"status": "ready", "service": "ownership"}


@app.get("/api/v1/stocks/{symbol}/shareholders")
def stock_shareholders(
    symbol: str,
    request: Request,
    runtime: ReadOnlyOwnershipApi = Depends(get_runtime),
):
    return _execute(lambda: runtime.stock_shareholders(symbol, _query_dict(request)))


@app.get("/api/v1/stocks/{symbol}/ownership-series")
def stock_ownership_series(
    symbol: str,
    request: Request,
    runtime: ReadOnlyOwnershipApi = Depends(get_runtime),
):
    return _execute(lambda: runtime.stock_ownership_series(symbol, _query_dict(request)))


@app.get("/api/v1/shareholders/search")
def shareholders_search(
    request: Request,
    runtime: ReadOnlyOwnershipApi = Depends(get_runtime),
):
    return _execute(lambda: runtime.shareholders_search(_query_dict(request)))


@app.get("/api/v1/shareholders/observation-history")
def shareholder_observation_history(
    request: Request,
    runtime: ReadOnlyOwnershipApi = Depends(get_runtime),
):
    return _execute(lambda: runtime.shareholder_observation_history(_query_dict(request)))
