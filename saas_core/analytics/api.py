"""Consent-aware analytics ingestion and permission-gated reports."""
from datetime import date, datetime
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, ConfigDict

from saas_core.auth.api import current_user, get_conn
from saas_core.auth.authorization import AuthorizationDenied, require_permission
from .service import get_funnels, get_overview, get_retention, get_user_activity, record_events, set_consent

router=APIRouter(prefix="/analytics",tags=["analytics"])

class ConsentBody(BaseModel):
    product_analytics: bool

class EventBody(BaseModel):
    model_config=ConfigDict(extra="forbid")
    event_id: UUID
    session_id: UUID | None = None
    event_name: str=Field(min_length=1,max_length=64)
    page_path: str=Field(min_length=1,max_length=300)
    component_id: str|None=Field(default=None,max_length=160)
    element_role: str|None=Field(default=None,max_length=40)
    occurred_at: datetime
    viewport_width: int|None=Field(default=None,ge=1,le=10000)
    viewport_height: int|None=Field(default=None,ge=1,le=10000)
    scroll_depth_pct: float|None=Field(default=None,ge=0,le=100)
    active_duration_ms: int|None=Field(default=None,ge=0,le=300000)
    metadata: dict=Field(default_factory=dict)

class EventBatch(BaseModel):
    events: list[EventBody]=Field(min_length=1,max_length=50)


def _admin(conn,user,permission="analytics.read"):
    try:
        require_permission(conn,user["id"],permission)
    except AuthorizationDenied as exc:
        raise HTTPException(status_code=403,detail="required analytics permission is missing") from exc


def _dates(start: date, end: date):
    if start > end:
        raise HTTPException(status_code=422,detail="start must not be after end")
    if (end-start).days > 366:
        raise HTTPException(status_code=422,detail="date range cannot exceed 367 days")

@router.put("/consent")
def consent(body:ConsentBody,user=Depends(current_user),conn=Depends(get_conn)):
    return {"consent":set_consent(conn,user_id=user["id"],enabled=body.product_analytics)}

@router.post("/events",status_code=202)
def ingest(body:EventBatch,user=Depends(current_user),conn=Depends(get_conn)):
    try:
        return record_events(conn,user_id=user["id"],events=[event.model_dump() for event in body.events])
    except PermissionError as exc:
        raise HTTPException(status_code=403,detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422,detail=str(exc)) from exc

@router.get("/admin/overview")
def overview(start:date=Query(...),end:date=Query(...),user=Depends(current_user),conn=Depends(get_conn)):
    _admin(conn,user);_dates(start,end)
    return get_overview(conn,start=start,end=end)

@router.get("/admin/funnels")
def funnels(start:date=Query(...),end:date=Query(...),user=Depends(current_user),conn=Depends(get_conn)):
    _admin(conn,user);_dates(start,end)
    return {"funnels":get_funnels(conn,start=start,end=end)}

@router.get("/admin/retention")
def retention(start:date=Query(...),end:date=Query(...),user=Depends(current_user),conn=Depends(get_conn)):
    _admin(conn,user);_dates(start,end)
    return {"retention":get_retention(conn,start=start,end=end)}

@router.get("/admin/users/{user_id}")
def user_activity(user_id:UUID,start:date=Query(...),end:date=Query(...),
                  user=Depends(current_user),conn=Depends(get_conn)):
    _admin(conn,user,"analytics.users.read");_dates(start,end)
    try:
        return get_user_activity(conn,actor_id=user["id"],user_id=user_id,start=start,end=end)
    except AuthorizationDenied as exc:
        raise HTTPException(status_code=403,detail="required analytics permission is missing") from exc
    except LookupError as exc:
        raise HTTPException(status_code=404,detail="user not found") from exc
