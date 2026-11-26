"""Authenticated support tickets, staff queue and public FAQ API."""
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from saas_core.auth.api import current_user, get_conn
from saas_core.auth.authorization import AuthorizationDenied, require_permission
from .service import (
    add_internal_note, assign_ticket, change_ticket_status, create_faq,
    create_ticket, get_staff_ticket, get_user_ticket, list_faq,
    list_staff_tickets, list_user_tickets, reply_user_ticket,
    staff_reply_ticket, update_faq,
)

router=APIRouter(prefix="/support",tags=["support"])


class TicketCreateBody(BaseModel):
    subject: str=Field(min_length=3,max_length=180)
    category: str|None=Field(default=None,max_length=40)
    message: str=Field(min_length=1,max_length=10000)


class MessageBody(BaseModel):
    message: str=Field(min_length=1,max_length=10000)


class AssignBody(BaseModel):
    assignee_user_id: UUID


class StatusBody(BaseModel):
    status: str=Field(pattern="^(open|waiting|answered|closed)$")
    note: str|None=Field(default=None,max_length=500)


class FaqCreateBody(BaseModel):
    slug: str=Field(min_length=1,max_length=120,pattern="^[a-z0-9]+(?:-[a-z0-9]+)*$")
    question: str=Field(min_length=1,max_length=300)
    answer: str=Field(min_length=1,max_length=20000)
    sort_order: int=Field(default=0,ge=0,le=100000)
    published: bool=False


class FaqUpdateBody(BaseModel):
    slug: str|None=Field(default=None,min_length=1,max_length=120,pattern="^[a-z0-9]+(?:-[a-z0-9]+)*$")
    question: str|None=Field(default=None,min_length=1,max_length=300)
    answer: str|None=Field(default=None,min_length=1,max_length=20000)
    sort_order: int|None=Field(default=None,ge=0,le=100000)
    published: bool|None=None


def _require(conn,user,permission="tickets.manage"):
    try:
        require_permission(conn,user["id"],permission)
    except AuthorizationDenied as exc:
        raise HTTPException(status_code=403,detail="required permission is missing") from exc


def _not_found(exc):
    raise HTTPException(status_code=404,detail=str(exc)) from exc


@router.get("/faqs")
def public_faq(conn=Depends(get_conn)):
    return {"faqs":list_faq(conn)}


@router.post("/tickets",status_code=201)
def ticket_create(body:TicketCreateBody,user=Depends(current_user),conn=Depends(get_conn)):
    try:
        return {"ticket":create_ticket(
            conn,user_id=user["id"],subject=body.subject,category=body.category,message=body.message
        )}
    except ValueError as exc:
        raise HTTPException(status_code=422,detail=str(exc)) from exc


@router.get("/tickets")
def tickets(limit:int=50,offset:int=0,user=Depends(current_user),conn=Depends(get_conn)):
    try:
        return {"tickets":list_user_tickets(conn,user_id=user["id"],limit=limit,offset=offset)}
    except ValueError as exc:
        raise HTTPException(status_code=422,detail=str(exc)) from exc


@router.get("/tickets/{ticket_id}")
def ticket_detail(ticket_id:UUID,user=Depends(current_user),conn=Depends(get_conn)):
    try:
        return get_user_ticket(conn,user_id=user["id"],ticket_id=ticket_id)
    except LookupError as exc:
        _not_found(exc)


@router.post("/tickets/{ticket_id}/messages",status_code=201)
def ticket_reply(ticket_id:UUID,body:MessageBody,user=Depends(current_user),conn=Depends(get_conn)):
    try:
        return {"message":reply_user_ticket(
            conn,user_id=user["id"],ticket_id=ticket_id,message=body.message
        )}
    except LookupError as exc:
        _not_found(exc)
    except ValueError as exc:
        raise HTTPException(status_code=409,detail=str(exc)) from exc


@router.get("/admin/tickets")
def staff_queue(status:str|None=None,assigned_to:UUID|None=None,limit:int=100,offset:int=0,
                user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn,user)
    try:
        return {"tickets":list_staff_tickets(
            conn,status=status,assigned_to=assigned_to,limit=limit,offset=offset
        )}
    except ValueError as exc:
        raise HTTPException(status_code=422,detail=str(exc)) from exc


@router.get("/admin/tickets/{ticket_id}")
def staff_ticket_detail(ticket_id:UUID,user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn,user)
    try:
        return get_staff_ticket(conn,ticket_id=ticket_id)
    except LookupError as exc:
        _not_found(exc)


@router.post("/admin/tickets/{ticket_id}/assign")
def staff_assign(ticket_id:UUID,body:AssignBody,user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn,user)
    try:
        return {"ticket":assign_ticket(
            conn,ticket_id=ticket_id,actor_id=user["id"],assignee_id=body.assignee_user_id
        )}
    except LookupError as exc:
        _not_found(exc)
    except ValueError as exc:
        raise HTTPException(status_code=409,detail=str(exc)) from exc
    except AuthorizationDenied as exc:
        raise HTTPException(status_code=403,detail="assignee is not a support agent") from exc


@router.post("/admin/tickets/{ticket_id}/messages",status_code=201)
def staff_reply(ticket_id:UUID,body:MessageBody,user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn,user)
    try:
        return {"message":staff_reply_ticket(
            conn,ticket_id=ticket_id,actor_id=user["id"],message=body.message
        )}
    except LookupError as exc:
        _not_found(exc)
    except ValueError as exc:
        raise HTTPException(status_code=409,detail=str(exc)) from exc


@router.post("/admin/tickets/{ticket_id}/notes",status_code=201)
def staff_note(ticket_id:UUID,body:MessageBody,user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn,user)
    try:
        return {"note":add_internal_note(
            conn,ticket_id=ticket_id,actor_id=user["id"],note=body.message
        )}
    except LookupError as exc:
        _not_found(exc)


@router.patch("/admin/tickets/{ticket_id}/status")
def staff_status(ticket_id:UUID,body:StatusBody,user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn,user)
    try:
        return {"ticket":change_ticket_status(
            conn,ticket_id=ticket_id,actor_id=user["id"],
            new_status=body.status,note=body.note,
        )}
    except LookupError as exc:
        _not_found(exc)
    except ValueError as exc:
        raise HTTPException(status_code=409,detail=str(exc)) from exc


@router.post("/admin/faqs",status_code=201)
def faq_create(body:FaqCreateBody,user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn,user,"faq.manage")
    try:
        return {"faq":create_faq(
            conn,actor_id=user["id"],slug=body.slug,question=body.question,
            answer=body.answer,sort_order=body.sort_order,published=body.published,
        )}
    except ValueError as exc:
        raise HTTPException(status_code=422,detail=str(exc)) from exc


@router.patch("/admin/faqs/{faq_id}")
def faq_update(faq_id:UUID,body:FaqUpdateBody,user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn,user,"faq.manage")
    try:
        return {"faq":update_faq(
            conn,actor_id=user["id"],faq_id=faq_id,
            changes=body.model_dump(exclude_unset=True),
        )}
    except LookupError as exc:
        _not_found(exc)
    except ValueError as exc:
        raise HTTPException(status_code=422,detail=str(exc)) from exc
