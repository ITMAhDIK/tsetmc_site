"""HTTP application for safe stock screening and XLSX exports."""
from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import Body, Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import Response

from screener_service.runtime import ReadOnlyScreenerRuntime
from site_api.errors import ApiError

logger = logging.getLogger(__name__)
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def validate_production_configuration() -> None:
    if os.getenv("APP_ENV", "development").strip().lower() not in {"prod", "production"}:
        return
    if not os.getenv("SCREENER_TRUSTED_HOSTS", "").strip():
        raise RuntimeError("SCREENER_TRUSTED_HOSTS must be explicit in production")
    origins = [
        value.strip().rstrip("/")
        for value in os.getenv("SCREENER_ALLOWED_ORIGINS", "").split(",")
        if value.strip()
    ]
    if not origins:
        raise RuntimeError("SCREENER_ALLOWED_ORIGINS must be explicit in production")
    for origin in origins:
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "https" or not parsed.netloc or parsed.path
            or parsed.query or parsed.fragment or parsed.username or parsed.password
        ):
            raise RuntimeError("Screener production origins must be HTTPS origins")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    validate_production_configuration()
    yield


app = FastAPI(
    title="Tloomberg Screener Service",
    version="1.0.0",
    description=(
        "Validated stock screening against the materialized market and ownership "
        "projection, with allow-listed JSON fields and XLSX export."
    ),
    lifespan=lifespan,
)

trusted_hosts = [
    value.strip()
    for value in os.getenv("SCREENER_TRUSTED_HOSTS", "").split(",")
    if value.strip()
]
if trusted_hosts:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)

allowed_origins = [
    value.strip().rstrip("/")
    for value in os.getenv("SCREENER_ALLOWED_ORIGINS", "").split(",")
    if value.strip()
]
if allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=False,
        allow_methods=["POST", "OPTIONS"],
        allow_headers=["Accept", "Content-Type"],
        max_age=600,
    )


def get_runtime() -> ReadOnlyScreenerRuntime:
    return ReadOnlyScreenerRuntime(
        os.getenv("SCREENER_SIDECAR_PATH", "/var/lib/tloomberg/site_data.sqlite"),
        sqlite_timeout=float(os.getenv("SCREENER_SQLITE_TIMEOUT_SECONDS", "10")),
    )


def _validate_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="بدنه‌ی درخواست باید یک شیء JSON باشد.")
    conditions = payload.get("conditions") or []
    if not isinstance(conditions, list):
        raise HTTPException(status_code=422, detail="conditions باید آرایه باشد.")
    if len(conditions) > 50:
        raise HTTPException(status_code=422, detail="حداکثر ۵۰ شرط در هر فیلتر مجاز است.")
    if any(not isinstance(condition, dict) for condition in conditions):
        raise HTTPException(status_code=422, detail="هر شرط باید یک شیء JSON باشد.")
    if "limit" in payload:
        try:
            int(payload.get("limit") or 100)
        except (TypeError, ValueError, OverflowError):
            raise HTTPException(status_code=422, detail="limit باید عدد صحیح باشد.") from None
    return payload


def _execute(operation):
    try:
        return operation()
    except ApiError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from None
    except (sqlite3.Error, OSError):
        logger.exception("Screener query failed because the projection is unavailable")
        raise HTTPException(
            status_code=503,
            detail="سرویس غربال‌گری موقتاً به داده‌های بازار دسترسی ندارد.",
        ) from None


@app.get("/healthz")
def health():
    return {"status": "ok", "service": "screener"}


@app.get("/readyz")
def ready(runtime: ReadOnlyScreenerRuntime = Depends(get_runtime)):
    is_ready, reason = runtime.readiness()
    if not is_ready:
        raise HTTPException(status_code=503, detail=reason)
    return {"status": "ready", "service": "screener"}


@app.post("/api/v1/screener/query")
def screener_query(
    payload: dict = Body(...),
    runtime: ReadOnlyScreenerRuntime = Depends(get_runtime),
):
    safe_payload = _validate_payload(payload)
    return _execute(lambda: runtime.screener_query(safe_payload))


@app.post("/api/v1/screener/export")
def screener_export(
    payload: dict = Body(...),
    runtime: ReadOnlyScreenerRuntime = Depends(get_runtime),
):
    safe_payload = _validate_payload(payload)
    workbook = _execute(lambda: runtime.screener_export(safe_payload))
    return Response(
        content=workbook,
        media_type=XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": 'attachment; filename="tloomberg-screener.xlsx"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
