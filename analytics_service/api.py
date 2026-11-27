"""Public and administrator API for the independent Product Analytics service."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from analytics_service.db import get_conn, lifespan_pool
from analytics_service.identity import current_user
from analytics_service.service import (
    DEFAULT_RETENTION_DAYS, AnalyticsRateLimited, get_consent, heatmap, normalize_page_path,
    funnel, overview, purge_expired_events, record_events, retention,
    top_pages, update_consent, user_event_history, repeated_paths,
)

class _RequestBodyTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    """Reject oversized request bodies before FastAPI buffers/parses JSON."""

    def __init__(self, app, max_body_bytes: int = 64 * 1024):
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") in {"GET", "HEAD", "OPTIONS"}:
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_length = headers.get(b"content-length")
        if raw_length is not None:
            try:
                declared = int(raw_length)
            except (TypeError, ValueError):
                response = JSONResponse({"detail": "invalid content length"}, status_code=400)
                await response(scope, receive, send)
                return
            if declared < 0 or declared > self.max_body_bytes:
                response = JSONResponse({"detail": "request body too large"}, status_code=413)
                await response(scope, receive, send)
                return

        received = 0

        async def guarded_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    raise _RequestBodyTooLarge
            return message

        try:
            await self.app(scope, guarded_receive, send)
        except _RequestBodyTooLarge:
            response = JSONResponse({"detail": "request body too large"}, status_code=413)
            await response(scope, receive, send)


router = APIRouter(prefix="/analytics", tags=["analytics"])
CONSENT_VERSION = os.getenv("SAAS_ANALYTICS_CONSENT_VERSION", "v1")[:24]


def _require(principal: dict, permission: str) -> None:
    # Identity is the only authority for roles and permissions. Missing claims deny access.
    permissions = principal.get("permissions")
    if not isinstance(permissions, list) or permission not in permissions:
        raise HTTPException(status_code=403, detail="permission denied")
    if permission in {"analytics.users.read", "analytics.retention.manage"}:
        roles = principal.get("roles")
        if not isinstance(roles, list) or "superadmin" not in roles:
            raise HTTPException(status_code=403, detail="superadmin role required")
        verified = principal.get("mfa_verified_at")
        if not principal.get("mfa_enabled") or not verified:
            raise HTTPException(status_code=403, detail="recent privileged MFA verification is required")
        try:
            if isinstance(verified, datetime):
                verified_at = verified
            elif isinstance(verified, str):
                verified_at = datetime.fromisoformat(verified.replace("Z", "+00:00"))
            else:
                raise ValueError("invalid MFA timestamp")
            if verified_at.tzinfo is None:
                raise ValueError("MFA timestamp must be timezone-aware")
            age = datetime.now(timezone.utc) - verified_at.astimezone(timezone.utc)
            if age < timedelta(seconds=-120) or age > timedelta(minutes=15):
                raise ValueError("MFA verification is not recent")
        except (TypeError, ValueError, OverflowError):
            raise HTTPException(status_code=403, detail="recent privileged MFA verification is required") from None


class ConsentBody(BaseModel):
    product_analytics: bool


class EventInput(BaseModel):
    client_event_id: UUID
    event_name: Literal["page_view", "click", "navigation", "scroll", "active_time", "feature_view"]
    page_path: str = Field(min_length=1, max_length=300)
    component_id: str | None = Field(default=None, max_length=80)
    element_role: str | None = Field(default=None, max_length=80)
    viewport_width: int | None = Field(default=None, ge=1, le=10_000)
    viewport_height: int | None = Field(default=None, ge=1, le=10_000)
    scroll_depth_pct: float | None = Field(default=None, ge=0, le=100)
    active_duration_ms: int | None = Field(default=None, ge=0, le=180_000)
    x_pct: float | None = Field(default=None, ge=0, le=100)
    y_pct: float | None = Field(default=None, ge=0, le=100)

    @field_validator("viewport_width", "viewport_height", "active_duration_ms", mode="before")
    @classmethod
    def reject_boolean_integers(cls, value):
        if isinstance(value, bool):
            raise ValueError("boolean values are not valid numeric measurements")
        return value

    @field_validator("scroll_depth_pct", "x_pct", "y_pct", mode="before")
    @classmethod
    def reject_boolean_percentages(cls, value):
        if isinstance(value, bool):
            raise ValueError("boolean values are not valid percentages")
        return value

    @model_validator(mode="after")
    def coordinate_pair(self):
        if (self.x_pct is None) != (self.y_pct is None):
            raise ValueError("x_pct and y_pct must be supplied together")
        return self


class EventBatch(BaseModel):
    events: list[EventInput] = Field(min_length=1, max_length=50)


@router.get("/consent")
def consent_status(user=Depends(current_user), conn=Depends(get_conn)):
    return {"consent": get_consent(
        conn, user_id=user["id"], expected_version=CONSENT_VERSION
    )}


@router.put("/consent")
def consent_update(body: ConsentBody, user=Depends(current_user), conn=Depends(get_conn)):
    row = update_consent(
        conn, user_id=user["id"], product_analytics=body.product_analytics,
        consent_version=CONSENT_VERSION,
    )
    return {"consent": row}


@router.post("/events")
def capture_events(body: EventBatch, user=Depends(current_user), conn=Depends(get_conn)):
    try:
        return record_events(
            conn, user_id=user["id"], session_id=user["session_id"],
            consent_version=CONSENT_VERSION,
            events=[event.model_dump() for event in body.events],
        )
    except AnalyticsRateLimited as exc:
        raise HTTPException(
            status_code=429, detail=str(exc), headers={"Retry-After": "60"}
        ) from None
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.get("/admin/overview")
def admin_overview(days: int = Query(default=30, ge=1, le=730),
                   user=Depends(current_user), conn=Depends(get_conn)):
    _require(user, "analytics.read")
    return {"days": days, "overview": overview(conn, days=days)}


@router.get("/admin/pages")
def admin_pages(days: int = Query(default=30, ge=1, le=730),
                limit: int = Query(default=20, ge=1, le=100),
                user=Depends(current_user), conn=Depends(get_conn)):
    _require(user, "analytics.read")
    return {"days": days, "pages": top_pages(conn, days=days, limit=limit)}


@router.get("/admin/heatmap")
def admin_heatmap(page_path: str = Query(min_length=1, max_length=300),
                  days: int = Query(default=30, ge=1, le=730),
                  user=Depends(current_user), conn=Depends(get_conn)):
    _require(user, "analytics.read")
    try:
        normalized_path = normalize_page_path(page_path)
        return {"page_path": normalized_path, "days": days,
                "cells": heatmap(conn, page_path=normalized_path, days=days)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.get("/admin/funnel")
def admin_funnel(steps: list[str] = Query(min_length=2, max_length=5),
                 days: int = Query(default=30, ge=1, le=730),
                 user=Depends(current_user), conn=Depends(get_conn)):
    _require(user, "analytics.read")
    try:
        return {"days": days, **funnel(conn, steps=steps, days=days)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.get("/admin/retention")
def admin_retention(days: int = Query(default=180, ge=1, le=730),
                    user=Depends(current_user), conn=Depends(get_conn)):
    _require(user, "analytics.read")
    return {"days": days, "cohorts": retention(conn, days=days)}


@router.get("/admin/users/{user_id}/events")
def admin_user_events(user_id: UUID, limit: int = Query(default=100, ge=1, le=200),
                      user=Depends(current_user), conn=Depends(get_conn)):
    _require(user, "analytics.users.read")
    try:
        events = user_event_history(
            conn, user_id=user_id, actor_user_id=user["id"], limit=limit
        )
        return {"user_id": str(user_id), "events": events}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.get("/admin/paths")
def admin_repeated_paths(
    days: int = Query(default=30, ge=1, le=730),
    max_steps: int = Query(default=5, ge=2, le=5),
    min_repetitions: int = Query(default=2, ge=2, le=10_000),
    limit: int = Query(default=50, ge=1, le=100),
    user=Depends(current_user), conn=Depends(get_conn),
):
    _require(user, "analytics.read")
    return {"days": days, "max_steps": max_steps, **repeated_paths(
        conn, days=days, max_steps=max_steps,
        min_repetitions=min_repetitions, limit=limit,
    )}


@router.get("/admin/users/{user_id}/paths")
def admin_user_repeated_paths(
    user_id: UUID,
    days: int = Query(default=30, ge=1, le=730),
    max_steps: int = Query(default=5, ge=2, le=5),
    min_repetitions: int = Query(default=2, ge=2, le=10_000),
    limit: int = Query(default=50, ge=1, le=100),
    user=Depends(current_user), conn=Depends(get_conn),
):
    _require(user, "analytics.users.read")
    try:
        return {"user_id": str(user_id), "days": days, "max_steps": max_steps,
                **repeated_paths(
                    conn, days=days, max_steps=max_steps,
                    min_repetitions=min_repetitions, limit=limit,
                    user_id=user_id, actor_user_id=user["id"],
                )}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.delete("/admin/retention/expired")
def admin_purge_expired(user=Depends(current_user), conn=Depends(get_conn)):
    _require(user, "analytics.retention.manage")
    try:
        days = int(os.getenv("SAAS_ANALYTICS_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS)))
        deleted = purge_expired_events(
            conn, retention_days=days, actor_user_id=user["id"]
        )
        return {"deleted_events": deleted, "retention_days": days}
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="Analytics retention configuration is invalid") from exc


@asynccontextmanager
async def app_lifespan(app: FastAPI):
    async with lifespan_pool(app):
        yield


APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
TRUSTED_HOSTS = [v.strip() for v in os.getenv("ANALYTICS_TRUSTED_HOSTS", "").split(",") if v.strip()]
ALLOWED_ORIGINS = [v.strip().rstrip("/") for v in os.getenv("ANALYTICS_ALLOWED_ORIGINS", "").split(",") if v.strip()]
app = FastAPI(title="Tloomberg Analytics Service", version="1.0.0", lifespan=app_lifespan)
app.add_middleware(RequestBodyLimitMiddleware, max_body_bytes=64 * 1024)
if TRUSTED_HOSTS:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=TRUSTED_HOSTS)
if ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"], max_age=600,
    )


@app.get("/health")
def health():
    return {"status": "ok", "service": "analytics"}


@app.get("/ready")
def ready(conn=Depends(get_conn)):
    try:
        actual = conn.execute("SELECT current_database() AS name").fetchone()["name"]
        expected = os.getenv("ANALYTICS_EXPECTED_DATABASE_NAME", "").strip()
        if expected and actual != expected:
            raise HTTPException(status_code=503, detail="unexpected Analytics database")
        required = ("analytics_consents", "analytics_consent_history",
                    "analytics_events", "analytics_ingestion_limits",
                    "analytics_audit_logs", "analytics_schema_migrations")
        row = conn.execute(
            "SELECT " + ", ".join(
                f"to_regclass('public.{name}') IS NOT NULL AS {name}" for name in required
            )
        ).fetchone()
        if not all(row.values()):
            raise HTTPException(status_code=503, detail="Analytics schema is not ready")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=503, detail="Analytics database is unavailable") from None
    return {"status": "ready", "service": "analytics"}


app.include_router(router)
