"""Independent HTTP API for instrument reference data and daily prices."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from market_data_service.repository import (
    InstrumentNotFound,
    MarketDataRepository,
    MarketDataUnavailable,
)


def repository_from_environment():
    return MarketDataRepository(
        os.getenv("MARKET_DATA_SOURCE_DB_PATH", "/data/source/market.sqlite"),
        os.getenv("MARKET_DATA_SIDECAR_PATH", "/var/lib/tloomberg/site_data.sqlite"),
        expected_sidecar_version=os.getenv("MARKET_DATA_EXPECTED_SIDECAR_VERSION", "10"),
        sqlite_timeout=float(os.getenv("MARKET_DATA_SQLITE_TIMEOUT_SECONDS", "5")),
    )


def validate_production_configuration():
    if os.getenv("APP_ENV", "development").strip().lower() not in {"prod", "production"}:
        return
    source_path = os.getenv("MARKET_DATA_SOURCE_DB_PATH", "").strip()
    sidecar_path = os.getenv("MARKET_DATA_SIDECAR_PATH", "").strip()
    if not source_path or not sidecar_path:
        raise RuntimeError("Market Data source and Sidecar paths are required in production")
    if not os.getenv("MARKET_DATA_TRUSTED_HOSTS", "").strip():
        raise RuntimeError("MARKET_DATA_TRUSTED_HOSTS must be explicit in production")
    if not os.getenv("MARKET_DATA_ALLOWED_ORIGINS", "").strip():
        raise RuntimeError("MARKET_DATA_ALLOWED_ORIGINS must be explicit in production")
    origins = [
        value.strip().rstrip("/")
        for value in os.getenv("MARKET_DATA_ALLOWED_ORIGINS", "").split(",")
        if value.strip()
    ]
    from urllib.parse import urlsplit
    for origin in origins:
        parsed = urlsplit(origin)
        if (parsed.scheme != "https" or not parsed.netloc or parsed.path
                or parsed.query or parsed.fragment or parsed.username or parsed.password):
            raise RuntimeError("Market Data production origins must be HTTPS origins")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    validate_production_configuration()
    yield


app = FastAPI(
    title="Tloomberg Market Data Service",
    version="1.0.0",
    description="Read-only instrument reference data and daily OHLCV prices.",
    lifespan=lifespan,
)

trusted_hosts = [
    value.strip()
    for value in os.getenv("MARKET_DATA_TRUSTED_HOSTS", "").split(",")
    if value.strip()
]
if trusted_hosts:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)

allowed_origins = [
    value.strip().rstrip("/")
    for value in os.getenv("MARKET_DATA_ALLOWED_ORIGINS", "").split(",")
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


def get_repository():
    return repository_from_environment()


@app.get("/healthz")
def health():
    return {"status": "ok", "service": "market-data"}


@app.get("/readyz")
def ready(repository=Depends(get_repository)):
    is_ready, reason = repository.readiness()
    if not is_ready:
        raise HTTPException(status_code=503, detail=reason)
    return {"status": "ready", "service": "market-data"}


@app.get("/api/v1/market/instruments")
def instruments(
    q: str = "",
    market: str = "ALL",
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1_000_000),
    repository=Depends(get_repository),
):
    try:
        return repository.search_instruments(
            q=q, market=market, limit=limit, offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except MarketDataUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


@app.get("/api/v1/market/instruments/{symbol}")
def instrument_detail(symbol: str, repository=Depends(get_repository)):
    try:
        return repository.instrument_detail(symbol)
    except InstrumentNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except MarketDataUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


@app.get("/api/v1/stocks/search")
def legacy_symbol_search(
    q: str = "",
    limit: int = Query(default=8, ge=1, le=50),
    repository=Depends(get_repository),
):
    """Stable compatibility route consumed by the current dashboard."""
    try:
        result = repository.search_instruments(q=q, limit=limit, offset=0)
        return {"results": [
            {key: row[key] for key in ("symbol", "company_name", "market")}
            for row in result["rows"]
        ]}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except MarketDataUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


@app.get("/api/v1/stocks/{symbol}/prices")
def legacy_daily_prices(
    symbol: str,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = Query(default=20000, ge=1, le=20000),
    repository=Depends(get_repository),
):
    """Stable compatibility route for the dashboard's daily OHLCV chart."""
    try:
        return repository.daily_prices(
            symbol, start_date=start_date, end_date=end_date, limit=limit,
        )
    except InstrumentNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except MarketDataUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
