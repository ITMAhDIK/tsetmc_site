"""Support tickets and FAQ persistence for the isolated SaaS database."""
from __future__ import annotations

import json
import re
from uuid import UUID

from saas_core.auth.authorization import require_permission

_TICKET_STATUSES={"open","waiting","answered","closed"}
_TRANSITIONS={
    "open":{"waiting","answered","closed"},
    "waiting":{"open","answered","closed"},
    "answered":{"open","waiting","closed"},
    "closed":{"open"},
}
_SLUG=re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _text(value, field, maximum, minimum=1):
    if not isinstance(value,str):
        raise ValueError(f"{field} must be text")
    cleaned=value.strip()
    if not minimum <= len(cleaned) <= maximum:
        raise ValueError(f"{field} must contain {minimum} to {maximum} characters")
    return cleaned


def _audit(conn, actor_id, action, target_id, before=None, after=None, target_type="ticket"):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO admin_audit_logs
               (actor_user_id,action,target_type,target_id,before_state,after_state)
               VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb)""",
            (actor_id,action,target_type,str(target_id),
             json.dumps(before) if before is not None else None,
             json.dumps(after) if after is not None else None),
        )


def list_faq(conn):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id,slug,question,answer,sort_order,updated_at
               FROM faq_articles WHERE published=TRUE
               ORDER BY sort_order,question"""
        )
        return cur.fetchall()


def create_ticket(conn, *, user_id: UUID, subject: str, category: str | None, message: str):
    subject=_text(subject,"subject",180,3)
    message=_text(message,"message",10000)
    category=_text(category,"category",40) if category is not None else None
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO tickets(user_id,subject,category,status,priority)
                   VALUES (%s,%s,%s,'open','normal')
                   RETURNING id,ticket_number,user_id,assigned_to,subject,category,priority,status,
                             created_at,updated_at""",
                (user_id,subject,category),
            )
            ticket=cur.fetchone()
            cur.execute(
                """INSERT INTO ticket_messages(ticket_id,sender_user_id,body,is_internal_note)
                   VALUES (%s,%s,%s,FALSE)""",
                (ticket["id"],user_id,message),
            )
            return ticket


def list_user_tickets(conn, *, user_id: UUID, limit: int=50, offset: int=0):
    if not 1 <= limit <= 100 or offset < 0:
        raise ValueError("invalid ticket pagination")
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id,ticket_number,subject,category,priority,status,created_at,updated_at
               FROM tickets WHERE user_id=%s
               ORDER BY updated_at DESC,id DESC LIMIT %s OFFSET %s""",
            (user_id,limit,offset),
        )
        return cur.fetchall()


def _ticket_messages(conn,ticket_id,include_internal):
    with conn.cursor() as cur:
        if include_internal:
            cur.execute(
                """SELECT id,sender_user_id,body,is_internal_note,created_at
                   FROM ticket_messages WHERE ticket_id=%s
                   ORDER BY created_at,id""",(ticket_id,)
            )
        else:
            cur.execute(
                """SELECT id,sender_user_id,body,is_internal_note,created_at
                   FROM ticket_messages WHERE ticket_id=%s AND is_internal_note=FALSE
                   ORDER BY created_at,id""",(ticket_id,)
            )
        return cur.fetchall()


def get_user_ticket(conn, *, user_id: UUID, ticket_id: UUID):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id,ticket_number,user_id,assigned_to,subject,category,priority,status,
                      created_at,updated_at
               FROM tickets WHERE id=%s AND user_id=%s""",(ticket_id,user_id)
        )
        ticket=cur.fetchone()
    if not ticket:
        raise LookupError("ticket not found")
    return {"ticket":ticket,"messages":_ticket_messages(conn,ticket_id,False)}


def get_staff_ticket(conn, *, actor_id: UUID, ticket_id: UUID):
    require_permission(conn,actor_id,"tickets.manage")
    with conn.cursor() as cur:
        cur.execute(
            """SELECT t.id,ticket_number,t.user_id,t.assigned_to,t.subject,t.category,t.priority,
                      t.status,t.created_at,t.updated_at,u.username,u.display_name
               FROM tickets t JOIN users u ON u.id=t.user_id WHERE t.id=%s""",(ticket_id,)
        )
        ticket=cur.fetchone()
    if not ticket:
        raise LookupError("ticket not found")
    return {"ticket":ticket,"messages":_ticket_messages(conn,ticket_id,True)}


def reply_user_ticket(conn, *, user_id: UUID, ticket_id: UUID, message: str):
    message=_text(message,"message",10000)
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """SELECT status FROM tickets WHERE id=%s AND user_id=%s FOR UPDATE""",
                (ticket_id,user_id),
            )
            ticket=cur.fetchone()
            if not ticket:
                raise LookupError("ticket not found")
            if ticket["status"]=="closed":
                raise ValueError("closed ticket cannot receive a user reply")
            cur.execute(
                """INSERT INTO ticket_messages(ticket_id,sender_user_id,body,is_internal_note)
                   VALUES (%s,%s,%s,FALSE) RETURNING id,sender_user_id,body,is_internal_note,created_at""",
                (ticket_id,user_id,message),
            )
            reply=cur.fetchone()
            cur.execute(
                "UPDATE tickets SET status='open',updated_at=now() WHERE id=%s",(ticket_id,)
            )
            return reply


def list_staff_tickets(conn, *, actor_id: UUID, status: str | None=None, assigned_to: UUID | None=None,
                       limit: int=100, offset: int=0):
    require_permission(conn,actor_id,"tickets.manage")
    if not 1 <= limit <= 200 or offset < 0:
        raise ValueError("invalid ticket pagination")
    if status is not None and status not in _TICKET_STATUSES:
        raise ValueError("invalid ticket status")
    clauses=[]
    params=[]
    if status:
        clauses.append("t.status=%s")
        params.append(status)
    if assigned_to:
        clauses.append("t.assigned_to=%s")
        params.append(assigned_to)
    where=("WHERE "+" AND ".join(clauses)) if clauses else ""
    params.extend([limit,offset])
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT t.id,t.ticket_number,t.user_id,t.assigned_to,t.subject,t.category,
                       t.priority,t.status,t.created_at,t.updated_at,
                       u.username,u.display_name
                FROM tickets t JOIN users u ON u.id=t.user_id
                {where}
                ORDER BY CASE t.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                         WHEN 'normal' THEN 2 ELSE 3 END,t.updated_at DESC
                LIMIT %s OFFSET %s""",tuple(params),
        )
        return cur.fetchall()


def assign_ticket(conn, *, ticket_id: UUID, actor_id: UUID, assignee_id: UUID):
    with conn.transaction():
        require_permission(conn,actor_id,"tickets.manage")
        require_permission(conn,assignee_id,"tickets.manage")
        with conn.cursor() as cur:
            cur.execute(
                """SELECT assigned_to,status FROM tickets WHERE id=%s FOR UPDATE""",(ticket_id,)
            )
            ticket=cur.fetchone()
            if not ticket:
                raise LookupError("ticket not found")
            cur.execute("SELECT status FROM users WHERE id=%s",(assignee_id,))
            assignee=cur.fetchone()
            if not assignee or assignee["status"]!="active":
                raise ValueError("assignee must be an active support agent")
            cur.execute(
                """UPDATE tickets SET assigned_to=%s,updated_at=now()
                   WHERE id=%s RETURNING id,ticket_number,assigned_to,status""",
                (assignee_id,ticket_id),
            )
            result=cur.fetchone()
            _audit(conn,actor_id,"support.ticket.assigned",ticket_id,
                   {"assigned_to":str(ticket["assigned_to"]) if ticket["assigned_to"] else None},
                   {"assigned_to":str(assignee_id)})
            return result


def staff_reply_ticket(conn, *, ticket_id: UUID, actor_id: UUID, message: str):
    message=_text(message,"message",10000)
    with conn.transaction():
        require_permission(conn,actor_id,"tickets.manage")
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM tickets WHERE id=%s FOR UPDATE",(ticket_id,))
            ticket=cur.fetchone()
            if not ticket:
                raise LookupError("ticket not found")
            if ticket["status"]=="closed":
                raise ValueError("closed ticket must be reopened before replying")
            cur.execute(
                """INSERT INTO ticket_messages(ticket_id,sender_user_id,body,is_internal_note)
                   VALUES (%s,%s,%s,FALSE) RETURNING id,sender_user_id,body,is_internal_note,created_at""",
                (ticket_id,actor_id,message),
            )
            reply=cur.fetchone()
            cur.execute(
                "UPDATE tickets SET status='answered',updated_at=now() WHERE id=%s",(ticket_id,)
            )
            _audit(conn,actor_id,"support.ticket.replied",ticket_id,
                   {"status":ticket["status"]},{"status":"answered"})
            return reply


def add_internal_note(conn, *, ticket_id: UUID, actor_id: UUID, note: str):
    note=_text(note,"internal note",10000)
    with conn.transaction():
        require_permission(conn,actor_id,"tickets.manage")
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM tickets WHERE id=%s FOR UPDATE",(ticket_id,))
            if not cur.fetchone():
                raise LookupError("ticket not found")
            cur.execute(
                """INSERT INTO ticket_messages(ticket_id,sender_user_id,body,is_internal_note)
                   VALUES (%s,%s,%s,TRUE) RETURNING id,sender_user_id,body,is_internal_note,created_at""",
                (ticket_id,actor_id,note),
            )
            result=cur.fetchone()
            _audit(conn,actor_id,"support.ticket.internal_note",ticket_id,
                   None,{"message_id":str(result["id"])})
            return result


def change_ticket_status(conn, *, ticket_id: UUID, actor_id: UUID,
                         new_status: str, note: str | None=None):
    if new_status not in _TICKET_STATUSES:
        raise ValueError("invalid ticket status")
    note=_text(note,"status note",500) if note is not None else None
    with conn.transaction():
        require_permission(conn,actor_id,"tickets.manage")
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM tickets WHERE id=%s FOR UPDATE",(ticket_id,))
            ticket=cur.fetchone()
            if not ticket:
                raise LookupError("ticket not found")
            old=ticket["status"]
            if new_status==old:
                return {"id":ticket_id,"status":old}
            if new_status not in _TRANSITIONS[old]:
                raise ValueError(f"invalid ticket status transition: {old} -> {new_status}")
            cur.execute(
                """UPDATE tickets SET status=%s,updated_at=now()
                   WHERE id=%s RETURNING id,ticket_number,status,updated_at""",
                (new_status,ticket_id),
            )
            result=cur.fetchone()
            _audit(conn,actor_id,"support.ticket.status_changed",ticket_id,
                   {"status":old},{"status":new_status,"note":note})
            return result


def create_faq(conn, *, actor_id: UUID, slug: str, question: str, answer: str,
               sort_order: int=0, published: bool=False):
    slug=_text(slug,"slug",120)
    if not _SLUG.fullmatch(slug):
        raise ValueError("slug must contain lowercase letters, numbers and hyphens")
    question=_text(question,"question",300)
    answer=_text(answer,"answer",20000)
    if isinstance(sort_order,bool) or not isinstance(sort_order,int) or not 0<=sort_order<=100000:
        raise ValueError("sort_order is out of range")
    if not isinstance(published,bool):
        raise ValueError("published must be boolean")
    with conn.transaction():
        require_permission(conn,actor_id,"faq.manage")
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO faq_articles(slug,question,answer,sort_order,published)
                   VALUES (%s,%s,%s,%s,%s)
                   RETURNING id,slug,question,answer,sort_order,published,updated_at""",
                (slug,question,answer,sort_order,published),
            )
            faq=cur.fetchone()
            _audit(conn,actor_id,"support.faq.created",faq["id"],None,
                   {"slug":slug,"published":published},target_type="faq")
            return faq


def update_faq(conn, *, actor_id: UUID, faq_id: UUID, changes: dict):
    allowed={"slug","question","answer","sort_order","published"}
    if not changes or set(changes)-allowed:
        raise ValueError("no valid FAQ changes supplied")
    normalized={}
    if "slug" in changes:
        slug=_text(changes["slug"],"slug",120)
        if not _SLUG.fullmatch(slug):
            raise ValueError("slug must contain lowercase letters, numbers and hyphens")
        normalized["slug"]=slug
    if "question" in changes:
        normalized["question"]=_text(changes["question"],"question",300)
    if "answer" in changes:
        normalized["answer"]=_text(changes["answer"],"answer",20000)
    if "sort_order" in changes:
        value=changes["sort_order"]
        if isinstance(value,bool) or not isinstance(value,int) or not 0<=value<=100000:
            raise ValueError("sort_order is out of range")
        normalized["sort_order"]=value
    if "published" in changes:
        if not isinstance(changes["published"],bool):
            raise ValueError("published must be boolean")
        normalized["published"]=changes["published"]
    columns={"slug":"slug","question":"question","answer":"answer",
             "sort_order":"sort_order","published":"published"}
    with conn.transaction():
        require_permission(conn,actor_id,"faq.manage")
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM faq_articles WHERE id=%s FOR UPDATE",(faq_id,))
            if not cur.fetchone():
                raise LookupError("FAQ article not found")
            assignments=[f"{columns[key]}=%s" for key in normalized]
            values=list(normalized.values())
            assignments.append("updated_at=now()")
            values.append(faq_id)
            cur.execute(
                f"""UPDATE faq_articles SET {','.join(assignments)}
                    WHERE id=%s
                    RETURNING id,slug,question,answer,sort_order,published,updated_at""",
                tuple(values),
            )
            faq=cur.fetchone()
            _audit(conn,actor_id,"support.faq.updated",faq_id,None,
                   {"changed_fields":sorted(normalized)},target_type="faq")
            return faq
