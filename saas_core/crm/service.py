"""CRM customer relationship services for the isolated SaaS database.

CRM stores references to SaaS user UUIDs and short staff-authored summaries.
It deliberately does not copy phone/email values, ticket bodies, payment
credentials, bank details, OTPs or any Market database records.
"""
from __future__ import annotations

import json
from datetime import datetime
from uuid import UUID

LIFECYCLE_STAGES = {"new", "active", "at_risk", "churned"}
INTERACTION_CHANNELS = {"call", "email", "meeting", "note", "support", "other"}
FOLLOWUP_STATUSES = {"open", "completed", "cancelled"}


def _clean_text(value: str, *, field: str, minimum: int, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be text")
    cleaned = value.strip()
    if not minimum <= len(cleaned) <= maximum:
        raise ValueError(f"{field} must contain between {minimum} and {maximum} characters")
    return cleaned


def _audit(
    conn,
    *,
    actor_user_id: UUID,
    action: str,
    target_type: str,
    target_id: str,
    before: dict | None = None,
    after: dict | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO admin_audit_logs
               (actor_user_id,action,target_type,target_id,before_state,after_state)
               VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb)""",
            (
                actor_user_id,
                action,
                target_type,
                target_id,
                json.dumps(before, default=str) if before is not None else None,
                json.dumps(after, default=str) if after is not None else None,
            ),
        )


def _require_customer(conn, user_id: UUID) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id,public_id,username,display_name,status,created_at
               FROM users WHERE id=%s""",
            (user_id,),
        )
        row = cur.fetchone()
    if not row:
        raise LookupError("customer not found")
    return row


def _require_crm_assignee(conn, user_id: UUID) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT EXISTS(
                 SELECT 1 FROM users u
                 JOIN user_roles ur ON ur.user_id=u.id
                 JOIN role_permissions rp ON rp.role_id=ur.role_id
                 JOIN permissions p ON p.id=rp.permission_id
                 WHERE u.id=%s AND u.status='active' AND p.code='crm.manage'
               ) AS allowed""",
            (user_id,),
        )
        row = cur.fetchone()
    if not row or not row["allowed"]:
        raise ValueError("assignee must be an active CRM staff member")


def list_customers(
    conn,
    *,
    actor_user_id: UUID,
    query: str | None = None,
    account_status: str | None = None,
    lifecycle_stage: str | None = None,
    assigned_to: UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    if not 1 <= limit <= 100 or not 0 <= offset <= 10_000:
        raise ValueError("invalid pagination")
    if account_status is not None and account_status not in {
        "active", "suspended", "pending", "deleted"
    }:
        raise ValueError("invalid account status")
    if lifecycle_stage is not None and lifecycle_stage not in LIFECYCLE_STAGES:
        raise ValueError("invalid lifecycle stage")
    if query is not None:
        query = _clean_text(query, field="query", minimum=1, maximum=80)

    clauses: list[str] = []
    params: list[object] = []
    if account_status:
        clauses.append("u.status=%s")
        params.append(account_status)
    if lifecycle_stage:
        clauses.append("COALESCE(p.lifecycle_stage,'new')=%s")
        params.append(lifecycle_stage)
    if assigned_to is not None:
        clauses.append("p.assigned_to=%s")
        params.append(assigned_to)
    if query:
        clauses.append(
            "(u.username ILIKE %s OR u.public_id ILIKE %s OR u.display_name ILIKE %s)"
        )
        pattern = f"%{query}%"
        params.extend((pattern, pattern, pattern))
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT u.id,u.public_id,u.username,u.display_name,u.status,
                           u.created_at,u.last_login_at,
                           (u.phone_verified_at IS NOT NULL) AS phone_verified,
                           (u.email_verified_at IS NOT NULL) AS email_verified,
                           COALESCE(p.lifecycle_stage,'new') AS lifecycle_stage,
                           p.assigned_to,assignee.username AS assigned_username
                    FROM users u
                    LEFT JOIN crm_customer_profiles p ON p.user_id=u.id
                    LEFT JOIN users assignee ON assignee.id=p.assigned_to
                    {where}
                    ORDER BY u.created_at DESC,u.id
                    LIMIT %s OFFSET %s""",
                (*params, limit, offset),
            )
            customers = cur.fetchall()
            cur.execute(
                f"""SELECT COUNT(*)::bigint AS total
                    FROM users u
                    LEFT JOIN crm_customer_profiles p ON p.user_id=u.id
                    {where}""",
                params,
            )
            total = cur.fetchone()["total"]
        _audit(
            conn,
            actor_user_id=actor_user_id,
            action="crm.customers.list",
            target_type="customer_list",
            target_id="all",
            after={
                "total": total,
                "limit": limit,
                "offset": offset,
                "account_status": account_status,
                "lifecycle_stage": lifecycle_stage,
                "assigned_to": str(assigned_to) if assigned_to else None,
                "query_used": bool(query),
            },
        )
    return {"customers": customers, "total": total, "limit": limit, "offset": offset}


def get_customer(conn, *, actor_user_id: UUID, customer_id: UUID) -> dict | None:
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """SELECT u.id,u.public_id,u.username,u.display_name,u.status,
                          u.created_at,u.updated_at,u.last_login_at,
                          (u.phone_verified_at IS NOT NULL) AS phone_verified,
                          (u.email_verified_at IS NOT NULL) AS email_verified,
                          COALESCE(p.lifecycle_stage,'new') AS lifecycle_stage,
                          p.assigned_to,assignee.username AS assigned_username,
                          (SELECT COUNT(*)::bigint FROM subscriptions s
                           WHERE s.user_id=u.id AND s.status='active'
                             AND s.starts_at<=now() AND s.ends_at>now()) AS active_subscriptions
                   FROM users u
                   LEFT JOIN crm_customer_profiles p ON p.user_id=u.id
                   LEFT JOIN users assignee ON assignee.id=p.assigned_to
                   WHERE u.id=%s""",
                (customer_id,),
            )
            customer = cur.fetchone()
        if customer:
            _audit(
                conn,
                actor_user_id=actor_user_id,
                action="crm.customer.read",
                target_type="user",
                target_id=str(customer_id),
                after={"fields": "crm_profile"},
            )
    return customer


def update_customer_profile(
    conn,
    *,
    actor_user_id: UUID,
    customer_id: UUID,
    lifecycle_stage: str,
    assigned_to: UUID | None,
) -> dict:
    if lifecycle_stage not in LIFECYCLE_STAGES:
        raise ValueError("invalid lifecycle stage")
    _require_customer(conn, customer_id)
    if assigned_to is not None:
        _require_crm_assignee(conn, assigned_to)

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """SELECT lifecycle_stage,assigned_to
                   FROM crm_customer_profiles WHERE user_id=%s FOR UPDATE""",
                (customer_id,),
            )
            row = cur.fetchone()
            before = (
                {
                    "lifecycle_stage": row["lifecycle_stage"],
                    "assigned_to": str(row["assigned_to"]) if row["assigned_to"] else None,
                }
                if row else {"lifecycle_stage": "new", "assigned_to": None}
            )
            cur.execute(
                """INSERT INTO crm_customer_profiles(user_id,lifecycle_stage,assigned_to)
                   VALUES (%s,%s,%s)
                   ON CONFLICT(user_id) DO UPDATE SET
                     lifecycle_stage=EXCLUDED.lifecycle_stage,
                     assigned_to=EXCLUDED.assigned_to,
                     updated_at=now()
                   RETURNING user_id,lifecycle_stage,assigned_to,created_at,updated_at""",
                (customer_id, lifecycle_stage, assigned_to),
            )
            profile = cur.fetchone()
            _audit(
                conn,
                actor_user_id=actor_user_id,
                action="crm.customer.profile.update",
                target_type="user",
                target_id=str(customer_id),
                before=before,
                after={
                    "lifecycle_stage": profile["lifecycle_stage"],
                    "assigned_to": str(profile["assigned_to"]) if profile["assigned_to"] else None,
                },
            )
            return profile


def list_interactions(
    conn, *, actor_user_id: UUID, customer_id: UUID, limit: int = 50, offset: int = 0
) -> dict:
    if not 1 <= limit <= 100 or not 0 <= offset <= 10_000:
        raise ValueError("invalid pagination")
    _require_customer(conn, customer_id)
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """SELECT i.id,i.user_id,i.actor_user_id,u.username AS actor_username,
                          i.channel,i.subject,i.summary,i.happened_at,i.created_at
                   FROM crm_interactions i
                   JOIN users u ON u.id=i.actor_user_id
                   WHERE i.user_id=%s
                   ORDER BY i.happened_at DESC,i.created_at DESC,i.id DESC
                   LIMIT %s OFFSET %s""",
                (customer_id, limit, offset),
            )
            rows = cur.fetchall()
            cur.execute(
                "SELECT COUNT(*)::bigint AS total FROM crm_interactions WHERE user_id=%s",
                (customer_id,),
            )
            total = cur.fetchone()["total"]
        _audit(
            conn,
            actor_user_id=actor_user_id,
            action="crm.customer.interactions.read",
            target_type="user",
            target_id=str(customer_id),
            after={"total": total, "limit": limit, "offset": offset},
        )
    return {"interactions": rows, "total": total, "limit": limit, "offset": offset}


def create_interaction(
    conn,
    *,
    actor_user_id: UUID,
    customer_id: UUID,
    channel: str,
    subject: str,
    summary: str,
    happened_at: datetime | None = None,
) -> dict:
    if channel not in INTERACTION_CHANNELS:
        raise ValueError("invalid interaction channel")
    subject = _clean_text(subject, field="subject", minimum=3, maximum=180)
    summary = _clean_text(summary, field="summary", minimum=2, maximum=4000)
    if happened_at is not None and (
        happened_at.tzinfo is None or happened_at.utcoffset() is None
    ):
        raise ValueError("happened_at must include a timezone")
    _require_customer(conn, customer_id)

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO crm_interactions
                     (user_id,actor_user_id,channel,subject,summary,happened_at)
                   VALUES (%s,%s,%s,%s,%s,COALESCE(%s,now()))
                   RETURNING id,user_id,actor_user_id,channel,subject,summary,
                             happened_at,created_at""",
                (customer_id, actor_user_id, channel, subject, summary, happened_at),
            )
            interaction = cur.fetchone()
            _audit(
                conn,
                actor_user_id=actor_user_id,
                action="crm.customer.interaction.create",
                target_type="crm_interaction",
                target_id=str(interaction["id"]),
                after={
                    "user_id": str(customer_id),
                    "channel": channel,
                    "subject": subject,
                },
            )
            return interaction


def list_followups(
    conn,
    *,
    actor_user_id: UUID,
    status: str | None = "open",
    assigned_to: UUID | None = None,
    customer_id: UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    if not 1 <= limit <= 100 or not 0 <= offset <= 10_000:
        raise ValueError("invalid pagination")
    if status is not None and status not in FOLLOWUP_STATUSES:
        raise ValueError("invalid follow-up status")
    clauses: list[str] = []
    params: list[object] = []
    if status:
        clauses.append("f.status=%s")
        params.append(status)
    if assigned_to:
        clauses.append("f.assigned_to=%s")
        params.append(assigned_to)
    if customer_id:
        clauses.append("f.user_id=%s")
        params.append(customer_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT f.id,f.user_id,f.assigned_to,assignee.username AS assigned_username,
                           f.created_by,creator.username AS creator_username,
                           u.public_id,u.username,u.display_name,
                           f.title,f.details,f.due_at,f.status,f.completed_at,
                           f.created_at,f.updated_at
                    FROM crm_followups f
                    JOIN users u ON u.id=f.user_id
                    JOIN users assignee ON assignee.id=f.assigned_to
                    JOIN users creator ON creator.id=f.created_by
                    {where}
                    ORDER BY CASE WHEN f.status='open' THEN 0 ELSE 1 END,
                             f.due_at ASC,f.created_at DESC
                    LIMIT %s OFFSET %s""",
                (*params, limit, offset),
            )
            rows = cur.fetchall()
            cur.execute(
                f"""SELECT COUNT(*)::bigint AS total FROM crm_followups f {where}""",
                params,
            )
            total = cur.fetchone()["total"]
        _audit(
            conn,
            actor_user_id=actor_user_id,
            action="crm.followups.list",
            target_type="crm_followup_list",
            target_id="filtered",
            after={
                "total": total,
                "status": status,
                "assigned_to": str(assigned_to) if assigned_to else None,
                "customer_id": str(customer_id) if customer_id else None,
                "limit": limit,
                "offset": offset,
            },
        )
    return {"followups": rows, "total": total, "limit": limit, "offset": offset}


def create_followup(
    conn,
    *,
    actor_user_id: UUID,
    customer_id: UUID,
    assigned_to: UUID | None,
    title: str,
    details: str = "",
    due_at: datetime,
) -> dict:
    title = _clean_text(title, field="title", minimum=3, maximum=180)
    if not isinstance(details, str) or len(details) > 2000:
        raise ValueError("details must be text with at most 2000 characters")
    details = details.strip()
    if due_at.tzinfo is None or due_at.utcoffset() is None:
        raise ValueError("due_at must include a timezone")
    _require_customer(conn, customer_id)
    assignee = assigned_to or actor_user_id
    _require_crm_assignee(conn, assignee)

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO crm_followups
                     (user_id,assigned_to,created_by,title,details,due_at)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   RETURNING id,user_id,assigned_to,created_by,title,details,
                             due_at,status,completed_at,created_at,updated_at""",
                (customer_id, assignee, actor_user_id, title, details, due_at),
            )
            followup = cur.fetchone()
            _audit(
                conn,
                actor_user_id=actor_user_id,
                action="crm.followup.create",
                target_type="crm_followup",
                target_id=str(followup["id"]),
                after={
                    "user_id": str(customer_id),
                    "assigned_to": str(assignee),
                    "title": title,
                    "due_at": due_at.isoformat(),
                },
            )
            return followup


def update_followup_status(
    conn, *, actor_user_id: UUID, followup_id: UUID, status: str
) -> dict:
    if status not in FOLLOWUP_STATUSES:
        raise ValueError("invalid follow-up status")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id,user_id,assigned_to,title,status,completed_at
                   FROM crm_followups WHERE id=%s FOR UPDATE""",
                (followup_id,),
            )
            before = cur.fetchone()
            if not before:
                raise LookupError("follow-up not found")
            cur.execute(
                """UPDATE crm_followups
                   SET status=%s,
                       completed_at=CASE WHEN %s='completed' THEN COALESCE(completed_at,now())
                                         ELSE NULL END,
                       updated_at=now()
                   WHERE id=%s
                   RETURNING id,user_id,assigned_to,title,status,completed_at,updated_at""",
                (status, status, followup_id),
            )
            updated = cur.fetchone()
            _audit(
                conn,
                actor_user_id=actor_user_id,
                action="crm.followup.status.update",
                target_type="crm_followup",
                target_id=str(followup_id),
                before={
                    "status": before["status"],
                    "completed_at": before["completed_at"],
                },
                after={
                    "status": updated["status"],
                    "completed_at": updated["completed_at"],
                },
            )
            return updated
