"""Privacy-conscious analytics ingestion and reporting for the SaaS database."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

from saas_core.auth.authorization import require_permission

CONSENT_VERSION = "product-analytics-v1"
EVENT_NAMES = {"page_view", "click", "navigation", "scroll", "active_time", "funnel_step"}
ELEMENT_ROLES = {"button", "link", "tab", "menuitem", "navigation", "section", "card", "input"}
_METADATA_KEYS = {"feature", "funnel_name", "funnel_step"}
_PATH = re.compile(r"^/[A-Za-z0-9_./~-]{0,299}$")
_COMPONENT = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def set_consent(conn, *, user_id: UUID, enabled: bool):
    if not isinstance(enabled, bool):
        raise ValueError("product_analytics consent must be boolean")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO analytics_consents(user_id,essential,product_analytics,consent_version)
                   VALUES (%s,TRUE,%s,%s)
                   ON CONFLICT(user_id) DO UPDATE SET essential=TRUE,
                     product_analytics=EXCLUDED.product_analytics,
                     consent_version=EXCLUDED.consent_version,updated_at=now()
                   RETURNING user_id,essential,product_analytics,consent_version,updated_at""",
                (user_id, enabled, CONSENT_VERSION),
            )
            return cur.fetchone()


def _validate_event(event):
    if event["event_name"] not in EVENT_NAMES:
        raise ValueError("unsupported analytics event")
    path = event["page_path"]
    if not isinstance(path, str) or not _PATH.fullmatch(path) or ".." in path or "//" in path:
        raise ValueError("page_path must be a normalized path without query or fragment")
    component = event.get("component_id")
    if component is not None and (not isinstance(component, str) or not _COMPONENT.fullmatch(component)):
        raise ValueError("invalid component_id")
    role = event.get("element_role")
    if role is not None and role not in ELEMENT_ROLES:
        raise ValueError("unsupported element_role")
    occurred = event["occurred_at"]
    if occurred.tzinfo is None or occurred.utcoffset() is None:
        raise ValueError("occurred_at must include a timezone")
    now = datetime.now(timezone.utc)
    if occurred < now - timedelta(days=30) or occurred > now + timedelta(minutes=5):
        raise ValueError("occurred_at is outside the accepted time window")
    width, height = event.get("viewport_width"), event.get("viewport_height")
    for value in (width, height):
        if value is not None and (isinstance(value, bool) or not 1 <= value <= 10000):
            raise ValueError("viewport dimensions are out of range")
    depth = event.get("scroll_depth_pct")
    if depth is not None and not 0 <= float(depth) <= 100:
        raise ValueError("scroll depth must be between 0 and 100")
    duration = event.get("active_duration_ms")
    if duration is not None and (isinstance(duration, bool) or not 0 <= duration <= 300000):
        raise ValueError("active duration must be between 0 and 300000 ms")
    if depth is not None and event["event_name"] != "scroll":
        raise ValueError("scroll_depth_pct is only valid for scroll events")
    if duration is not None and event["event_name"] != "active_time":
        raise ValueError("active_duration_ms is only valid for active_time events")
    metadata = event.get("metadata") or {}
    if not isinstance(metadata, dict) or set(metadata) - _METADATA_KEYS:
        raise ValueError("metadata contains unsupported fields")
    normalized = {}
    for key, value in metadata.items():
        if key == "funnel_step":
            if event["event_name"] != "funnel_step" or isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 20:
                raise ValueError("funnel_step must be an integer from 1 to 20 on funnel events")
            normalized[key] = value
        else:
            if not isinstance(value, str) or not _SLUG.fullmatch(value.strip()):
                raise ValueError(f"metadata {key} must be a short lowercase identifier")
            normalized[key] = value.strip()
    if event["event_name"] == "funnel_step" and not {"funnel_name", "funnel_step"}.issubset(normalized):
        raise ValueError("funnel events require funnel_name and funnel_step")
    if event["event_name"] != "funnel_step" and ({"funnel_name", "funnel_step"} & set(normalized)):
        raise ValueError("funnel metadata is only valid for funnel events")
    return normalized


def record_events(conn, *, user_id: UUID, events: list[dict]):
    if not 1 <= len(events) <= 50:
        raise ValueError("event batch must contain 1 to 50 events")
    prepared = [(event, _validate_event(event)) for event in events]
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT product_analytics FROM analytics_consents WHERE user_id=%s", (user_id,))
            consent = cur.fetchone()
            if not consent or not consent["product_analytics"]:
                raise PermissionError("product analytics consent is required")
            inserted = 0
            for event, metadata in prepared:
                cur.execute(
                    """INSERT INTO analytics_events
                       (user_id,anonymous_id,session_id,client_event_id,event_name,page_path,
                        component_id,element_role,occurred_at,viewport_width,viewport_height,
                        scroll_depth_pct,active_duration_ms,metadata)
                       VALUES (%s,NULL,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                       ON CONFLICT (user_id,client_event_id) WHERE client_event_id IS NOT NULL
                       DO NOTHING RETURNING id""",
                    (user_id,event.get("session_id"),event["event_id"],event["event_name"],
                     event["page_path"],event.get("component_id"),event.get("element_role"),
                     event["occurred_at"],event.get("viewport_width"),event.get("viewport_height"),
                     event.get("scroll_depth_pct"),event.get("active_duration_ms"),json.dumps(metadata)),
                )
                if cur.fetchone():
                    inserted += 1
            return {"accepted": inserted, "duplicates": len(events) - inserted}


def _range(start: date, end: date):
    if not isinstance(start, date) or not isinstance(end, date) or start > end:
        raise ValueError("invalid analytics date range")
    if (end - start).days > 366:
        raise ValueError("analytics date range cannot exceed 367 days")
    return datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc), datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)


def get_overview(conn, *, start: date, end: date):
    start_at, end_at = _range(start, end)
    with conn.cursor() as cur:
        cur.execute(
            """SELECT date_trunc('day',occurred_at)::date AS day,
                      count(*)::int AS events,count(DISTINCT user_id)::int AS active_users
               FROM analytics_events WHERE user_id IS NOT NULL AND occurred_at >= %s AND occurred_at < %s
               GROUP BY 1 ORDER BY 1""", (start_at,end_at),
        )
        daily = cur.fetchall()
        cur.execute(
            """SELECT event_name,count(*)::int AS events,count(DISTINCT user_id)::int AS users
               FROM analytics_events WHERE user_id IS NOT NULL AND occurred_at >= %s AND occurred_at < %s
               GROUP BY event_name ORDER BY events DESC,event_name""", (start_at,end_at),
        )
        event_mix = cur.fetchall()
        cur.execute(
            """SELECT page_path,count(*)::int AS views,count(DISTINCT user_id)::int AS users
               FROM analytics_events WHERE user_id IS NOT NULL AND event_name='page_view'
                 AND occurred_at >= %s AND occurred_at < %s
               GROUP BY page_path ORDER BY views DESC,page_path LIMIT 20""", (start_at,end_at),
        )
        top_pages = cur.fetchall()
        cur.execute(
            """SELECT page_path,component_id,element_role,count(*)::int AS interactions,
                      count(DISTINCT user_id)::int AS users
               FROM analytics_events WHERE user_id IS NOT NULL AND component_id IS NOT NULL
                 AND event_name IN ('click','navigation') AND occurred_at >= %s AND occurred_at < %s
               GROUP BY page_path,component_id,element_role
               ORDER BY interactions DESC,page_path,component_id LIMIT 100""", (start_at,end_at),
        )
        return {"start": start.isoformat(),"end": end.isoformat(),"daily":daily,
                "event_mix":event_mix,"top_pages":top_pages,"component_activity":cur.fetchall()}


def get_funnels(conn, *, start: date, end: date):
    start_at, end_at = _range(start, end)
    with conn.cursor() as cur:
        cur.execute(
            """SELECT metadata->>'funnel_name' AS funnel,
                      (metadata->>'funnel_step')::int AS step,
                      count(*)::int AS events,count(DISTINCT user_id)::int AS users
               FROM analytics_events WHERE event_name='funnel_step'
                 AND occurred_at >= %s AND occurred_at < %s
               GROUP BY 1,2 ORDER BY 1,2""", (start_at,end_at),
        )
        return cur.fetchall()


def get_retention(conn, *, start: date, end: date):
    start_at, end_at = _range(start, end)
    with conn.cursor() as cur:
        cur.execute(
            """WITH first_seen AS (
                 SELECT user_id,min(occurred_at)::date AS first_day FROM analytics_events
                 WHERE user_id IS NOT NULL GROUP BY user_id
               ), cohorts AS (
                 SELECT user_id,date_trunc('week',first_day::timestamp)::date AS cohort
                 FROM first_seen WHERE first_day >= %s::date AND first_day <= %s::date
               ), activity AS (
                 SELECT DISTINCT user_id,date_trunc('week',occurred_at)::date AS active_week
                 FROM analytics_events WHERE user_id IS NOT NULL AND occurred_at >= %s AND occurred_at < %s
               )
               SELECT c.cohort,((a.active_week-c.cohort)/7)::int AS week_number,
                      count(DISTINCT c.user_id)::int AS retained_users,
                      (SELECT count(*)::int FROM cohorts c2 WHERE c2.cohort=c.cohort) AS cohort_size
               FROM cohorts c JOIN activity a ON a.user_id=c.user_id
               WHERE a.active_week >= c.cohort
               GROUP BY c.cohort,week_number ORDER BY c.cohort,week_number""",
            (start,end,start_at,end_at),
        )
        rows=cur.fetchall()
        for row in rows:
            row["retention_pct"] = round(100 * row["retained_users"] / row["cohort_size"], 2) if row["cohort_size"] else 0
        return rows


def get_user_activity(conn, *, actor_id: UUID, user_id: UUID, start: date, end: date):
    require_permission(conn,actor_id,"analytics.users.read")
    start_at,end_at=_range(start,end)
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE id=%s",(user_id,))
            if not cur.fetchone():
                raise LookupError("user not found")
            cur.execute(
                """SELECT event_name,count(*)::int AS events,
                          min(occurred_at) AS first_seen,max(occurred_at) AS last_seen
                   FROM analytics_events WHERE user_id=%s AND occurred_at >= %s AND occurred_at < %s
                   GROUP BY event_name ORDER BY events DESC,event_name""",
                (user_id,start_at,end_at),
            )
            by_event=cur.fetchall()
            cur.execute(
                """SELECT page_path,count(*)::int AS events
                   FROM analytics_events WHERE user_id=%s AND occurred_at >= %s AND occurred_at < %s
                   GROUP BY page_path ORDER BY events DESC,page_path LIMIT 50""",
                (user_id,start_at,end_at),
            )
            pages=cur.fetchall()
            cur.execute(
                """INSERT INTO admin_audit_logs(actor_user_id,action,target_type,target_id,after_state)
                   VALUES (%s,'analytics.user_activity.viewed','user',%s,%s::jsonb)""",
                (actor_id,str(user_id),json.dumps({"start":start.isoformat(),"end":end.isoformat()})),
            )
            return {"user_id":str(user_id),"start":start.isoformat(),"end":end.isoformat(),
                    "by_event":by_event,"pages":pages}
