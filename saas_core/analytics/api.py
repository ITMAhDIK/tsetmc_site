"""Consent-aware product analytics API and privacy-gated reporting."""
import os
from uuid import UUID
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from saas_core.auth.api import current_user, get_conn
from saas_core.auth.authorization import AuthorizationDenied, require_permission
from .service import (
    DEFAULT_RETENTION_DAYS, get_consent, heatmap, normalize_page_path,
    funnel, overview, purge_expired_events, record_events, retention,
    top_pages, update_consent, user_event_history, repeated_paths,
)

router=APIRouter(prefix="/analytics",tags=["analytics"])
CONSENT_VERSION=os.getenv("SAAS_ANALYTICS_CONSENT_VERSION","v1")[:24]


def _require(conn, principal, permission):
    user_id = principal["id"]
    try:
        require_permission(conn,user_id,permission,principal=principal)
    except AuthorizationDenied:
        raise HTTPException(status_code=403,detail="permission denied")


class ConsentBody(BaseModel):
    product_analytics: bool


class EventInput(BaseModel):
    client_event_id: UUID
    event_name: Literal["page_view","click","navigation","scroll","active_time","feature_view"]
    page_path: str = Field(min_length=1,max_length=300)
    component_id: str | None = Field(default=None,max_length=80)
    element_role: str | None = Field(default=None,max_length=80)
    viewport_width: int | None = Field(default=None,ge=1,le=10_000)
    viewport_height: int | None = Field(default=None,ge=1,le=10_000)
    scroll_depth_pct: float | None = Field(default=None,ge=0,le=100)
    active_duration_ms: int | None = Field(default=None,ge=0,le=180_000)
    x_pct: float | None = Field(default=None,ge=0,le=100)
    y_pct: float | None = Field(default=None,ge=0,le=100)

    @field_validator("viewport_width","viewport_height","active_duration_ms",mode="before")
    @classmethod
    def reject_boolean_integers(cls,value):
        if isinstance(value,bool):
            raise ValueError("boolean values are not valid numeric measurements")
        return value

    @field_validator("scroll_depth_pct","x_pct","y_pct",mode="before")
    @classmethod
    def reject_boolean_percentages(cls,value):
        if isinstance(value,bool):
            raise ValueError("boolean values are not valid percentages")
        return value

    @model_validator(mode="after")
    def coordinate_pair(self):
        if (self.x_pct is None) != (self.y_pct is None):
            raise ValueError("x_pct and y_pct must be supplied together")
        return self


class EventBatch(BaseModel):
    events: list[EventInput] = Field(min_length=1,max_length=50)


@router.get("/consent")
def consent_status(user=Depends(current_user),conn=Depends(get_conn)):
    return {"consent":get_consent(conn,user_id=user["id"])}


@router.put("/consent")
def consent_update(body:ConsentBody,user=Depends(current_user),conn=Depends(get_conn)):
    row=update_consent(
        conn,user_id=user["id"],product_analytics=body.product_analytics,
        consent_version=CONSENT_VERSION,
    )
    return {"consent":row}


@router.post("/events")
def capture_events(body:EventBatch,user=Depends(current_user),conn=Depends(get_conn)):
    try:
        result=record_events(
            conn,user_id=user["id"],session_id=user["session_id"],
            events=[event.model_dump() for event in body.events],
        )
        return result
    except PermissionError as exc:
        raise HTTPException(status_code=403,detail=str(exc))
    except (TypeError,ValueError) as exc:
        raise HTTPException(status_code=422,detail=str(exc))


@router.get("/admin/overview")
def admin_overview(days:int=Query(default=30,ge=1,le=730),
                   user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn, user,"analytics.read")
    return {"days":days,"overview":overview(conn,days=days)}


@router.get("/admin/pages")
def admin_pages(days:int=Query(default=30,ge=1,le=730),
                limit:int=Query(default=20,ge=1,le=100),
                user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn, user,"analytics.read")
    return {"days":days,"pages":top_pages(conn,days=days,limit=limit)}


@router.get("/admin/heatmap")
def admin_heatmap(page_path:str=Query(min_length=1,max_length=300),
                  days:int=Query(default=30,ge=1,le=730),
                  user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn, user,"analytics.read")
    try:
        return {"page_path":normalize_page_path(page_path),"days":days,"cells":heatmap(conn,page_path=page_path,days=days)}
    except ValueError as exc:
        raise HTTPException(status_code=422,detail=str(exc))


@router.get("/admin/funnel")
def admin_funnel(steps:list[str]=Query(min_length=2,max_length=5),
                 days:int=Query(default=30,ge=1,le=730),
                 user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn, user,"analytics.read")
    try:
        return {"days":days,**funnel(conn,steps=steps,days=days)}
    except ValueError as exc:
        raise HTTPException(status_code=422,detail=str(exc))


@router.get("/admin/retention")
def admin_retention(days:int=Query(default=180,ge=1,le=730),
                    user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn, user,"analytics.read")
    return {"days":days,"cohorts":retention(conn,days=days)}


@router.get("/admin/users/{user_id}/events")
def admin_user_events(user_id:UUID,limit:int=Query(default=100,ge=1,le=200),
                      user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn, user,"analytics.users.read")
    return {"user_id":str(user_id),"events":user_event_history(conn,user_id=user_id,limit=limit)}


@router.get("/admin/paths")
def admin_repeated_paths(
    days:int=Query(default=30,ge=1,le=730),
    max_steps:int=Query(default=5,ge=2,le=5),
    min_repetitions:int=Query(default=2,ge=2,le=10_000),
    limit:int=Query(default=50,ge=1,le=100),
    user=Depends(current_user),conn=Depends(get_conn),
):
    _require(conn, user,"analytics.read")
    return {
        "days":days,
        "max_steps":max_steps,
        **repeated_paths(
            conn,days=days,max_steps=max_steps,
            min_repetitions=min_repetitions,limit=limit,
        ),
    }


@router.get("/admin/users/{user_id}/paths")
def admin_user_repeated_paths(
    user_id:UUID,
    days:int=Query(default=30,ge=1,le=730),
    max_steps:int=Query(default=5,ge=2,le=5),
    min_repetitions:int=Query(default=2,ge=2,le=10_000),
    limit:int=Query(default=50,ge=1,le=100),
    user=Depends(current_user),conn=Depends(get_conn),
):
    _require(conn, user,"analytics.users.read")
    return {
        "user_id":str(user_id),
        "days":days,
        "max_steps":max_steps,
        **repeated_paths(
            conn,days=days,max_steps=max_steps,
            min_repetitions=min_repetitions,limit=limit,
            user_id=user_id,actor_user_id=user["id"],
        ),
    }


@router.delete("/admin/retention/expired")
def admin_purge_expired(user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn, user,"analytics.retention.manage")
    try:
        days=int(os.getenv("SAAS_ANALYTICS_RETENTION_DAYS",str(DEFAULT_RETENTION_DAYS)))
        deleted=purge_expired_events(conn,retention_days=days)
        return {"deleted_events":deleted,"retention_days":days}
    except ValueError as exc:
        raise HTTPException(status_code=503,detail="analytics retention configuration is invalid") from exc
