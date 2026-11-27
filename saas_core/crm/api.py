"""Permission-gated CRM APIs for authorized SaaS staff."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from saas_core.auth.api import current_user, get_conn, require_admin_mfa
from saas_core.auth.authorization import AuthorizationDenied, require_permission
from .service import (
    create_followup,
    create_interaction,
    get_customer,
    list_customers,
    list_followups,
    list_interactions,
    update_customer_profile,
    update_followup_status,
)

router = APIRouter(
    prefix="/crm",
    tags=["crm"],
    dependencies=[Depends(require_admin_mfa)],
)


def _require(conn, user_id: UUID, permission: str) -> None:
    try:
        require_permission(conn, user_id, permission)
    except AuthorizationDenied as exc:
        raise HTTPException(status_code=403, detail="CRM permission required") from exc


class CustomerProfileBody(BaseModel):
    lifecycle_stage: Literal["new", "active", "at_risk", "churned"]
    assigned_to: UUID | None = None


class InteractionBody(BaseModel):
    channel: Literal["call", "email", "meeting", "note", "support", "other"]
    subject: str = Field(min_length=3, max_length=180)
    summary: str = Field(min_length=2, max_length=4000)
    happened_at: datetime | None = None


class FollowupCreateBody(BaseModel):
    customer_id: UUID
    assigned_to: UUID | None = None
    title: str = Field(min_length=3, max_length=180)
    details: str = Field(default="", max_length=2000)
    due_at: datetime


class FollowupStatusBody(BaseModel):
    status: Literal["open", "completed", "cancelled"]


@router.get("/customers")
def customers(
    q: str | None = Query(default=None, max_length=80),
    account_status: str | None = None,
    lifecycle_stage: str | None = None,
    assigned_to: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
    user=Depends(current_user),
    conn=Depends(get_conn),
):
    _require(conn, user["id"], "crm.read")
    try:
        return list_customers(
            conn,
            actor_user_id=user["id"],
            query=q,
            account_status=account_status,
            lifecycle_stage=lifecycle_stage,
            assigned_to=assigned_to,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/customers/{customer_id}")
def customer_detail(
    customer_id: UUID,
    user=Depends(current_user),
    conn=Depends(get_conn),
):
    _require(conn, user["id"], "crm.read")
    customer = get_customer(conn, actor_user_id=user["id"], customer_id=customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="customer not found")
    return {"customer": customer}


@router.patch("/customers/{customer_id}/profile")
def customer_profile(
    customer_id: UUID,
    body: CustomerProfileBody,
    user=Depends(current_user),
    conn=Depends(get_conn),
):
    _require(conn, user["id"], "crm.manage")
    try:
        profile = update_customer_profile(
            conn,
            actor_user_id=user["id"],
            customer_id=customer_id,
            lifecycle_stage=body.lifecycle_stage,
            assigned_to=body.assigned_to,
        )
        return {"profile": profile}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/customers/{customer_id}/interactions")
def customer_interactions(
    customer_id: UUID,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
    user=Depends(current_user),
    conn=Depends(get_conn),
):
    _require(conn, user["id"], "crm.read")
    try:
        return list_interactions(
            conn,
            actor_user_id=user["id"],
            customer_id=customer_id,
            limit=limit,
            offset=offset,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/customers/{customer_id}/interactions", status_code=201)
def add_customer_interaction(
    customer_id: UUID,
    body: InteractionBody,
    user=Depends(current_user),
    conn=Depends(get_conn),
):
    _require(conn, user["id"], "crm.manage")
    try:
        interaction = create_interaction(
            conn,
            actor_user_id=user["id"],
            customer_id=customer_id,
            channel=body.channel,
            subject=body.subject,
            summary=body.summary,
            happened_at=body.happened_at,
        )
        return {"interaction": interaction}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/followups")
def followups(
    status: str | None = Query(default="open"),
    assigned_to: UUID | None = None,
    customer_id: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
    user=Depends(current_user),
    conn=Depends(get_conn),
):
    _require(conn, user["id"], "crm.read")
    try:
        return list_followups(
            conn,
            actor_user_id=user["id"],
            status=status,
            assigned_to=assigned_to,
            customer_id=customer_id,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/followups", status_code=201)
def add_followup(
    body: FollowupCreateBody,
    user=Depends(current_user),
    conn=Depends(get_conn),
):
    _require(conn, user["id"], "crm.manage")
    try:
        followup = create_followup(
            conn,
            actor_user_id=user["id"],
            customer_id=body.customer_id,
            assigned_to=body.assigned_to,
            title=body.title,
            details=body.details,
            due_at=body.due_at,
        )
        return {"followup": followup}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.patch("/followups/{followup_id}/status")
def set_followup_status(
    followup_id: UUID,
    body: FollowupStatusBody,
    user=Depends(current_user),
    conn=Depends(get_conn),
):
    _require(conn, user["id"], "crm.manage")
    try:
        followup = update_followup_status(
            conn,
            actor_user_id=user["id"],
            followup_id=followup_id,
            status=body.status,
        )
        return {"followup": followup}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
