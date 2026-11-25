"""Authenticated customer support, staff queue and FAQ endpoints."""
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from saas_core.auth.api import current_user, get_conn
from saas_core.auth.authorization import AuthorizationDenied, require_permission
from .service import (
    TICKET_STATUSES, add_internal_note, add_public_reply, assign_ticket,
    create_faq_article, create_ticket, get_ticket, list_faq,
    list_staff_tickets, list_ticket_messages, list_user_tickets, update_faq_article,
    update_ticket_status,
)

router=APIRouter(prefix="/support",tags=["support"])


def _require(conn,user_id,permission):
    try:
        require_permission(conn,user_id,permission)
    except AuthorizationDenied:
        raise HTTPException(status_code=403,detail="permission denied")


class CreateTicketBody(BaseModel):
    subject: str = Field(min_length=3,max_length=180)
    message: str = Field(min_length=2,max_length=10_000)
    category: str | None = Field(default=None,max_length=40)


class MessageBody(BaseModel):
    message: str = Field(min_length=2,max_length=10_000)


class AssignTicketBody(BaseModel):
    assigned_to: UUID | None = None


class TicketStatusBody(BaseModel):
    status: str = Field(pattern="^(open|waiting|answered|closed)$")


class FAQCreateBody(BaseModel):
    slug: str = Field(min_length=2,max_length=120)
    question: str = Field(min_length=3,max_length=300)
    answer: str = Field(min_length=2,max_length=20_000)
    sort_order: int = Field(default=0,ge=-10_000,le=10_000)
    published: bool = False


class FAQUpdateBody(BaseModel):
    slug: str | None = Field(default=None,min_length=2,max_length=120)
    question: str | None = Field(default=None,min_length=3,max_length=300)
    answer: str | None = Field(default=None,min_length=2,max_length=20_000)
    sort_order: int | None = Field(default=None,ge=-10_000,le=10_000)
    published: bool | None = None


@router.get("/faq")
def public_faq(conn=Depends(get_conn)):
    return {"articles":list_faq(conn,published_only=True)}


@router.get("/tickets")
def my_tickets(limit:int=Query(default=50,ge=1,le=100),
               before_ticket_number:int|None=Query(default=None,ge=1),
               user=Depends(current_user),conn=Depends(get_conn)):
    return {"tickets":list_user_tickets(conn,user_id=user["id"],limit=limit,
                                         before_ticket_number=before_ticket_number)}


@router.post("/tickets",status_code=201)
def open_ticket(body:CreateTicketBody,user=Depends(current_user),conn=Depends(get_conn)):
    try:
        return {"ticket":create_ticket(conn,user_id=user["id"],subject=body.subject,
                                        body=body.message,category=body.category)}
    except PermissionError as exc:
        raise HTTPException(status_code=429,detail=str(exc))
    except (TypeError,ValueError) as exc:
        raise HTTPException(status_code=422,detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404,detail=str(exc))


@router.get("/tickets/{ticket_id}")
def ticket_detail(ticket_id:UUID,user=Depends(current_user),conn=Depends(get_conn)):
    ticket=get_ticket(conn,ticket_id=ticket_id,user_id=user["id"])
    if not ticket:
        raise HTTPException(status_code=404,detail="ticket not found")
    return {"ticket":ticket,"messages":list_ticket_messages(conn,ticket_id=ticket_id,
                                                               include_internal=False)}


@router.post("/tickets/{ticket_id}/messages",status_code=201)
def customer_reply(ticket_id:UUID,body:MessageBody,user=Depends(current_user),conn=Depends(get_conn)):
    ticket=get_ticket(conn,ticket_id=ticket_id,user_id=user["id"])
    if not ticket:
        raise HTTPException(status_code=404,detail="ticket not found")
    try:
        message=add_public_reply(conn,ticket_id=ticket_id,sender_user_id=user["id"],
                                 body=body.message,staff_reply=False)
        return {"message":message}
    except ValueError as exc:
        raise HTTPException(status_code=422,detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404,detail=str(exc))


@router.get("/manage/tickets")
def staff_ticket_queue(status:str|None=None,assigned_to:UUID|None=None,
                       limit:int=Query(default=50,ge=1,le=100),
                       before_ticket_number:int|None=Query(default=None,ge=1),
                       user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn,user["id"],"tickets.manage")
    try:
        return {"tickets":list_staff_tickets(conn,status=status,assigned_to=assigned_to,
                                               limit=limit,before_ticket_number=before_ticket_number)}
    except ValueError as exc:
        raise HTTPException(status_code=422,detail=str(exc))


@router.get("/manage/tickets/{ticket_id}")
def staff_ticket_detail(ticket_id:UUID,user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn,user["id"],"tickets.manage")
    ticket=get_ticket(conn,ticket_id=ticket_id)
    if not ticket:
        raise HTTPException(status_code=404,detail="ticket not found")
    return {"ticket":ticket,"messages":list_ticket_messages(conn,ticket_id=ticket_id,
                                                               include_internal=True)}


@router.post("/manage/tickets/{ticket_id}/assign")
def staff_assign_ticket(ticket_id:UUID,body:AssignTicketBody,
                        user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn,user["id"],"tickets.manage")
    try:
        return {"ticket":assign_ticket(conn,ticket_id=ticket_id,actor_user_id=user["id"],
                                         assigned_to=body.assigned_to)}
    except ValueError as exc:
        raise HTTPException(status_code=422,detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404,detail=str(exc))


@router.patch("/manage/tickets/{ticket_id}/status")
def staff_set_status(ticket_id:UUID,body:TicketStatusBody,
                     user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn,user["id"],"tickets.manage")
    try:
        return {"ticket":update_ticket_status(conn,ticket_id=ticket_id,
                                               actor_user_id=user["id"],status=body.status)}
    except ValueError as exc:
        raise HTTPException(status_code=422,detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404,detail=str(exc))


@router.post("/manage/tickets/{ticket_id}/messages",status_code=201)
def staff_reply(ticket_id:UUID,body:MessageBody,user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn,user["id"],"tickets.manage")
    try:
        message=add_public_reply(conn,ticket_id=ticket_id,sender_user_id=user["id"],
                                 body=body.message,staff_reply=True)
        return {"message":message}
    except ValueError as exc:
        raise HTTPException(status_code=409,detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404,detail=str(exc))


@router.post("/manage/tickets/{ticket_id}/internal-notes",status_code=201)
def staff_internal_note(ticket_id:UUID,body:MessageBody,user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn,user["id"],"tickets.manage")
    try:
        message=add_internal_note(conn,ticket_id=ticket_id,sender_user_id=user["id"],
                                  body=body.message)
        return {"message":message}
    except ValueError as exc:
        raise HTTPException(status_code=422,detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404,detail=str(exc))


@router.get("/manage/faq")
def manage_faq(user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn,user["id"],"faq.manage")
    return {"articles":list_faq(conn,published_only=False)}


@router.post("/manage/faq",status_code=201)
def add_faq(body:FAQCreateBody,user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn,user["id"],"faq.manage")
    try:
        article=create_faq_article(conn,actor_user_id=user["id"],**body.model_dump())
        return {"article":article}
    except (TypeError,ValueError) as exc:
        raise HTTPException(status_code=422,detail=str(exc))


@router.patch("/manage/faq/{article_id}")
def patch_faq(article_id:UUID,body:FAQUpdateBody,
              user=Depends(current_user),conn=Depends(get_conn)):
    _require(conn,user["id"],"faq.manage")
    changes=body.model_dump(exclude_unset=True)
    try:
        article=update_faq_article(conn,actor_user_id=user["id"],
                                   article_id=article_id,changes=changes)
        return {"article":article}
    except (TypeError,ValueError) as exc:
        raise HTTPException(status_code=422,detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404,detail=str(exc))
