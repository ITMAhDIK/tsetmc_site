"""Persistence and policy helpers for SaaS support tickets and FAQ.

All database work uses the isolated SaaS PostgreSQL connection.
Ticket messages are plain text; clients must render them as text, not trusted HTML.
"""
from __future__ import annotations
import json
from uuid import UUID

TICKET_STATUSES = {"open", "waiting", "answered", "closed"}
TICKET_PRIORITIES = {"low", "normal", "high", "urgent"}
MAX_OPEN_TICKETS_PER_DAY = 10


def clean_text(value: str, *, field: str, minimum: int = 1, maximum: int = 10_000) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be text")
    cleaned = value.strip()
    if not minimum <= len(cleaned) <= maximum:
        raise ValueError(f"{field} must contain between {minimum} and {maximum} characters")
    return cleaned


def _audit(conn, *, actor_user_id: UUID, action: str, target_type: str,
           target_id: str, before: dict | None = None, after: dict | None = None):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO admin_audit_logs
               (actor_user_id,action,target_type,target_id,before_state,after_state)
               VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb)""",
            (actor_user_id,action,target_type,target_id,
             json.dumps(before,default=str) if before is not None else None,
             json.dumps(after,default=str) if after is not None else None),
        )


def create_ticket(conn, *, user_id: UUID, subject: str, body: str, category: str | None = None):
    subject = clean_text(subject,field="subject",minimum=3,maximum=180)
    body = clean_text(body,field="message",minimum=2,maximum=10_000)
    if category is not None:
        category = clean_text(category,field="category",minimum=2,maximum=40)
    with conn.transaction():
        with conn.cursor() as cur:
            # Serialize the per-user daily quota check to prevent parallel spam.
            cur.execute("SELECT id FROM users WHERE id=%s AND status='active' FOR UPDATE",(user_id,))
            if not cur.fetchone():
                raise LookupError("active account not found")
            cur.execute(
                """SELECT COUNT(*)::integer AS total FROM tickets
                   WHERE user_id=%s AND created_at >= now() - interval '24 hours'""",
                (user_id,),
            )
            if cur.fetchone()["total"] >= MAX_OPEN_TICKETS_PER_DAY:
                raise PermissionError("daily ticket creation limit reached")
            cur.execute(
                """INSERT INTO tickets(user_id,subject,category,status,priority)
                   VALUES (%s,%s,%s,'open','normal')
                   RETURNING id,ticket_number,user_id,subject,category,priority,status,
                             assigned_to,created_at,updated_at""",
                (user_id,subject,category),
            )
            ticket=cur.fetchone()
            cur.execute(
                """INSERT INTO ticket_messages(ticket_id,sender_user_id,body,is_internal_note)
                   VALUES (%s,%s,%s,FALSE)
                   RETURNING id,ticket_id,sender_user_id,body,is_internal_note,created_at""",
                (ticket["id"],user_id,body),
            )
            ticket["first_message"]=cur.fetchone()
            return ticket


def list_user_tickets(conn, *, user_id: UUID, limit: int = 50, before_ticket_number: int | None = None):
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    with conn.cursor() as cur:
        cur.execute(
            """SELECT t.id,t.ticket_number,t.subject,t.category,t.priority,t.status,
                      t.assigned_to,t.created_at,t.updated_at,
                      latest.body AS latest_public_message,
                      latest.created_at AS latest_message_at
               FROM tickets t
               LEFT JOIN LATERAL (
                 SELECT m.body,m.created_at FROM ticket_messages m
                 WHERE m.ticket_id=t.id AND m.is_internal_note=FALSE
                 ORDER BY m.created_at DESC,m.id DESC LIMIT 1
               ) latest ON TRUE
               WHERE t.user_id=%s AND (%s IS NULL OR t.ticket_number<%s)
               ORDER BY t.ticket_number DESC LIMIT %s""",
            (user_id,before_ticket_number,before_ticket_number,limit),
        )
        return cur.fetchall()


def get_ticket(conn, *, ticket_id: UUID, user_id: UUID | None = None):
    sql="""SELECT id,ticket_number,user_id,assigned_to,subject,category,priority,status,
                  created_at,updated_at FROM tickets WHERE id=%s"""
    params=[ticket_id]
    if user_id is not None:
        sql += " AND user_id=%s"
        params.append(user_id)
    with conn.cursor() as cur:
        cur.execute(sql,params)
        return cur.fetchone()


def list_ticket_messages(conn, *, ticket_id: UUID, include_internal: bool):
    with conn.cursor() as cur:
        if include_internal:
            cur.execute(
                """SELECT id,ticket_id,sender_user_id,body,is_internal_note,created_at
                   FROM ticket_messages WHERE ticket_id=%s ORDER BY created_at,id""",(ticket_id,)
            )
        else:
            cur.execute(
                """SELECT id,ticket_id,sender_user_id,body,FALSE AS is_internal_note,created_at
                   FROM ticket_messages
                   WHERE ticket_id=%s AND is_internal_note=FALSE ORDER BY created_at,id""",(ticket_id,)
            )
        return cur.fetchall()


def add_public_reply(conn, *, ticket_id: UUID, sender_user_id: UUID,
                     body: str, staff_reply: bool):
    body=clean_text(body,field="message",minimum=2,maximum=10_000)
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM tickets WHERE id=%s FOR UPDATE",(ticket_id,))
            ticket=cur.fetchone()
            if not ticket:
                raise LookupError("ticket not found")
            if staff_reply:
                if ticket["status"] == "closed":
                    raise ValueError("closed ticket must be reopened before replying")
                next_status="answered"
            else:
                # A customer reply reopens a closed ticket and returns it to the queue.
                next_status="open"
            cur.execute(
                """INSERT INTO ticket_messages(ticket_id,sender_user_id,body,is_internal_note)
                   VALUES (%s,%s,%s,FALSE)
                   RETURNING id,ticket_id,sender_user_id,body,is_internal_note,created_at""",
                (ticket_id,sender_user_id,body),
            )
            message=cur.fetchone()
            cur.execute("UPDATE tickets SET status=%s,updated_at=now() WHERE id=%s",
                        (next_status,ticket_id))
            return message


def add_internal_note(conn, *, ticket_id: UUID, sender_user_id: UUID, body: str):
    body=clean_text(body,field="internal note",minimum=2,maximum=10_000)
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM tickets WHERE id=%s FOR UPDATE",(ticket_id,))
            if not cur.fetchone():
                raise LookupError("ticket not found")
            cur.execute(
                """INSERT INTO ticket_messages(ticket_id,sender_user_id,body,is_internal_note)
                   VALUES (%s,%s,%s,TRUE)
                   RETURNING id,ticket_id,sender_user_id,body,is_internal_note,created_at""",
                (ticket_id,sender_user_id,body),
            )
            message=cur.fetchone()
            cur.execute("UPDATE tickets SET updated_at=now() WHERE id=%s",(ticket_id,))
            return message


def list_staff_tickets(conn, *, status: str | None, assigned_to: UUID | None,
                        limit: int = 50, before_ticket_number: int | None = None):
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if status is not None and status not in TICKET_STATUSES:
        raise ValueError("invalid ticket status")
    clauses=[]
    params=[]
    if status:
        clauses.append("t.status=%s")
        params.append(status)
    if assigned_to is not None:
        clauses.append("t.assigned_to=%s")
        params.append(assigned_to)
    if before_ticket_number is not None:
        clauses.append("t.ticket_number<%s")
        params.append(before_ticket_number)
    where=("WHERE "+" AND ".join(clauses)) if clauses else ""
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT t.id,t.ticket_number,t.user_id,u.username,t.subject,t.category,
                       t.priority,t.status,t.assigned_to,t.created_at,t.updated_at,
                       latest.body AS latest_message,latest.is_internal_note AS latest_is_internal
                FROM tickets t JOIN users u ON u.id=t.user_id
                LEFT JOIN LATERAL (
                  SELECT m.body,m.is_internal_note FROM ticket_messages m
                  WHERE m.ticket_id=t.id ORDER BY m.created_at DESC,m.id DESC LIMIT 1
                ) latest ON TRUE
                {where}
                ORDER BY CASE t.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                         WHEN 'normal' THEN 2 ELSE 3 END,
                         t.updated_at DESC,t.ticket_number DESC LIMIT %s""",
            (*params,limit),
        )
        return cur.fetchall()


def assign_ticket(conn, *, ticket_id: UUID, actor_user_id: UUID, assigned_to: UUID | None):
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT assigned_to,status FROM tickets WHERE id=%s FOR UPDATE",(ticket_id,))
            ticket=cur.fetchone()
            if not ticket:
                raise LookupError("ticket not found")
            if assigned_to is not None:
                cur.execute(
                    """SELECT EXISTS(
                         SELECT 1 FROM users u JOIN user_roles ur ON ur.user_id=u.id
                         JOIN roles r ON r.id=ur.role_id
                         WHERE u.id=%s AND u.status='active'
                           AND r.code IN ('support','admin','superadmin')
                       ) AS eligible""",(assigned_to,)
                )
                if not cur.fetchone()["eligible"]:
                    raise ValueError("assignee must be an active support or administrator user")
            cur.execute(
                """UPDATE tickets SET assigned_to=%s,updated_at=now()
                   WHERE id=%s RETURNING id,ticket_number,assigned_to,status""",
                (assigned_to,ticket_id),
            )
            updated=cur.fetchone()
            _audit(conn,actor_user_id=actor_user_id,action="support.ticket.assign",
                   target_type="ticket",target_id=str(ticket_id),
                   before={"assigned_to":ticket["assigned_to"]},
                   after={"assigned_to":assigned_to})
            return updated


def update_ticket_status(conn, *, ticket_id: UUID, actor_user_id: UUID, status: str):
    if status not in TICKET_STATUSES:
        raise ValueError("invalid ticket status")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM tickets WHERE id=%s FOR UPDATE",(ticket_id,))
            row=cur.fetchone()
            if not row:
                raise LookupError("ticket not found")
            cur.execute(
                """UPDATE tickets SET status=%s,updated_at=now()
                   WHERE id=%s RETURNING id,ticket_number,status,updated_at""",
                (status,ticket_id),
            )
            updated=cur.fetchone()
            _audit(conn,actor_user_id=actor_user_id,action="support.ticket.status",
                   target_type="ticket",target_id=str(ticket_id),
                   before={"status":row["status"]},after={"status":status})
            return updated


def list_faq(conn, *, published_only: bool = True):
    with conn.cursor() as cur:
        if published_only:
            cur.execute(
                """SELECT id,slug,question,answer,sort_order,updated_at
                   FROM faq_articles WHERE published=TRUE ORDER BY sort_order,question"""
            )
        else:
            cur.execute(
                """SELECT id,slug,question,answer,sort_order,published,updated_at
                   FROM faq_articles ORDER BY sort_order,question"""
            )
        return cur.fetchall()


def create_faq_article(conn, *, actor_user_id: UUID, slug: str, question: str,
                       answer: str, sort_order: int = 0, published: bool = False):
    slug=clean_text(slug,field="slug",minimum=2,maximum=120).lower()
    if any(not (ch.isalnum() or ch=="-") for ch in slug):
        raise ValueError("slug may contain only letters, numbers and hyphens")
    question=clean_text(question,field="question",minimum=3,maximum=300)
    answer=clean_text(answer,field="answer",minimum=2,maximum=20_000)
    if isinstance(sort_order,bool) or not isinstance(sort_order,int) or not -10_000 <= sort_order <= 10_000:
        raise ValueError("sort_order is out of range")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO faq_articles(slug,question,answer,sort_order,published)
                   VALUES (%s,%s,%s,%s,%s)
                   RETURNING id,slug,question,answer,sort_order,published,updated_at""",
                (slug,question,answer,sort_order,published),
            )
            row=cur.fetchone()
            _audit(conn,actor_user_id=actor_user_id,action="support.faq.create",
                   target_type="faq_article",target_id=str(row["id"]),
                   after={"slug":slug,"published":published})
            return row


def update_faq_article(conn, *, actor_user_id: UUID, article_id: UUID, changes: dict):
    allowed={"slug","question","answer","sort_order","published"}
    if not changes or set(changes)-allowed:
        raise ValueError("provide supported FAQ fields")
    if "slug" in changes:
        slug=clean_text(changes["slug"],field="slug",minimum=2,maximum=120).lower()
        if any(not (ch.isalnum() or ch=="-") for ch in slug):
            raise ValueError("slug may contain only letters, numbers and hyphens")
        changes["slug"]=slug
    if "question" in changes:
        changes["question"]=clean_text(changes["question"],field="question",minimum=3,maximum=300)
    if "answer" in changes:
        changes["answer"]=clean_text(changes["answer"],field="answer",minimum=2,maximum=20_000)
    if "sort_order" in changes:
        value=changes["sort_order"]
        if isinstance(value,bool) or not isinstance(value,int) or not -10_000 <= value <= 10_000:
            raise ValueError("sort_order is out of range")
    if "published" in changes and not isinstance(changes["published"],bool):
        raise TypeError("published must be boolean")
    columns=sorted(changes)
    assignments=", ".join(f"{name}=%s" for name in columns)
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT slug,published FROM faq_articles WHERE id=%s FOR UPDATE",(article_id,))
            before=cur.fetchone()
            if not before:
                raise LookupError("FAQ article not found")
            cur.execute(
                f"""UPDATE faq_articles SET {assignments},updated_at=now()
                    WHERE id=%s
                    RETURNING id,slug,question,answer,sort_order,published,updated_at""",
                (*[changes[name] for name in columns],article_id),
            )
            row=cur.fetchone()
            _audit(conn,actor_user_id=actor_user_id,action="support.faq.update",
                   target_type="faq_article",target_id=str(article_id),
                   before={"slug":before["slug"],"published":before["published"]},
                   after={"slug":row["slug"],"published":row["published"]})
            return row
