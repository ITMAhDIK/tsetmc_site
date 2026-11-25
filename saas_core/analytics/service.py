"""Consent-aware product analytics for the isolated SaaS database.

Only authenticated, explicitly opted-in users can submit events. The server owns
user/session identity; event payloads contain no free-form text or raw URLs.
"""
from __future__ import annotations
import re

from urllib.parse import urlsplit
from uuid import UUID

ALLOWED_EVENT_NAMES = {
    "page_view", "click", "navigation", "scroll", "active_time", "feature_view"
}
MIN_ANALYTICS_COHORT = 5
DEFAULT_RETENTION_DAYS = 365
_PAGE_PATH_RE = re.compile(r"^[\w./-]{1,300}$", re.UNICODE)
_UUID_SEGMENT_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)


def normalize_page_path(value: str) -> str:
    """Accept only an origin-relative pathname; never store query strings or fragments."""
    if not isinstance(value, str):
        raise TypeError("page_path must be text")
    value = value.strip()
    parsed = urlsplit(value)
    if (not value.startswith("/") or parsed.scheme or parsed.netloc
            or parsed.query or parsed.fragment or not _PAGE_PATH_RE.fullmatch(parsed.path)):
        raise ValueError("page_path must be a clean origin-relative path without query or fragment")
    segments=[]
    for segment in parsed.path.split("/"):
        if segment in {".",".."}:
            raise ValueError("page_path contains an invalid path segment")
        if segment.isdigit() and len(segment) >= 4 or _UUID_SEGMENT_RE.fullmatch(segment):
            segments.append(":id")
        else:
            segments.append(segment)
    normalized="/".join(segments) or "/"
    return normalized[:300]


def validate_event(event: dict) -> dict:
    """Validate and minimize a browser event before it reaches PostgreSQL."""
    event_name=event.get("event_name")
    if event_name not in ALLOWED_EVENT_NAMES:
        raise ValueError("unsupported analytics event")
    page_path=normalize_page_path(event.get("page_path"))
    component_id=event.get("component_id")
    element_role=event.get("element_role")
    for field,value in (("component_id",component_id),("element_role",element_role)):
        if value is not None and (
            not isinstance(value,str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}",value)
        ):
            raise ValueError(f"{field} must be a short non-personal identifier")
    width=event.get("viewport_width")
    height=event.get("viewport_height")
    for field,value in (("viewport_width",width),("viewport_height",height)):
        if value is not None and (
            isinstance(value,bool) or not isinstance(value,int) or not 1 <= value <= 10_000
        ):
            raise ValueError(f"{field} is out of range")
    scroll=event.get("scroll_depth_pct")
    if scroll is not None and (
        isinstance(scroll,bool) or not isinstance(scroll,(int,float)) or not 0 <= scroll <= 100
    ):
        raise ValueError("scroll_depth_pct must be between 0 and 100")
    if scroll is not None and event_name != "scroll":
        raise ValueError("scroll_depth_pct is only valid for scroll events")
    active=event.get("active_duration_ms")
    if active is not None and (
        isinstance(active,bool) or not isinstance(active,int) or not 0 <= active <= 180_000
    ):
        raise ValueError("active_duration_ms must be between 0 and 180000")
    if active is not None and event_name != "active_time":
        raise ValueError("active_duration_ms is only valid for active_time events")
    x_pct=event.get("x_pct")
    y_pct=event.get("y_pct")
    if (x_pct is None) != (y_pct is None):
        raise ValueError("x_pct and y_pct must be supplied together")
    for field,value in (("x_pct",x_pct),("y_pct",y_pct)):
        if value is not None and (
            isinstance(value,bool) or not isinstance(value,(int,float)) or not 0 <= value <= 100
        ):
            raise ValueError(f"{field} must be between 0 and 100")
    if x_pct is not None and event_name != "click":
        raise ValueError("x_pct and y_pct are only valid for click events")
    # Keep only the declared, low-risk fields. Ignore all other browser properties.
    return {
        "client_event_id":event["client_event_id"],
        "event_name":event_name,
        "page_path":page_path,
        "component_id":component_id,
        "element_role":element_role,
        "viewport_width":width,
        "viewport_height":height,
        "scroll_depth_pct":scroll,
        "active_duration_ms":active,
        "x_pct":x_pct,
        "y_pct":y_pct,
    }


def get_consent(conn, *, user_id: UUID):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT essential,product_analytics,consent_version,updated_at
               FROM analytics_consents WHERE user_id=%s""",(user_id,)
        )
        row=cur.fetchone()
    if row is None:
        return {"essential":True,"product_analytics":False,
                "consent_version":None,"updated_at":None}
    return row


def update_consent(conn, *, user_id: UUID, product_analytics: bool, consent_version: str):
    if not isinstance(product_analytics,bool):
        raise TypeError("product_analytics must be boolean")
    if not consent_version or len(consent_version)>24:
        raise ValueError("invalid consent version")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO analytics_consents
                   (user_id,essential,product_analytics,consent_version,updated_at)
                   VALUES (%s,TRUE,%s,%s,now())
                   ON CONFLICT(user_id) DO UPDATE SET
                     essential=TRUE,product_analytics=EXCLUDED.product_analytics,
                     consent_version=EXCLUDED.consent_version,updated_at=now()
                   RETURNING essential,product_analytics,consent_version,updated_at""",
                (user_id,product_analytics,consent_version),
            )
            consent=cur.fetchone()
            if not product_analytics:
                # Withdrawal of consent also removes previously collected identifiable events.
                cur.execute("DELETE FROM analytics_events WHERE user_id=%s",(user_id,))
            return consent


def record_events(conn, *, user_id: UUID, session_id: UUID, events: list[dict]):
    if not 1 <= len(events) <= 50:
        raise ValueError("a batch must contain between 1 and 50 events")
    clean=[validate_event(item) for item in events]
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "SELECT product_analytics FROM analytics_consents WHERE user_id=%s FOR SHARE",
                (user_id,),
            )
            consent=cur.fetchone()
            if not consent or not consent["product_analytics"]:
                raise PermissionError("product analytics consent is required")
            inserted=0
            for event in clean:
                cur.execute(
                    """INSERT INTO analytics_events
                       (user_id,anonymous_id,session_id,event_name,page_path,component_id,
                        element_role,occurred_at,viewport_width,viewport_height,
                        scroll_depth_pct,active_duration_ms,client_event_id,x_pct,y_pct)
                       VALUES (%s,NULL,%s,%s,%s,%s,%s,now(),%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (user_id,client_event_id)
                         WHERE user_id IS NOT NULL AND client_event_id IS NOT NULL
                       DO NOTHING
                       RETURNING id""",
                    (user_id,session_id,event["event_name"],event["page_path"],
                     event["component_id"],event["element_role"],event["viewport_width"],
                     event["viewport_height"],event["scroll_depth_pct"],
                     event["active_duration_ms"],event["client_event_id"],
                     event["x_pct"],event["y_pct"]),
                )
                if cur.fetchone():
                    inserted += 1
            return {"received":len(clean),"inserted":inserted,
                    "duplicates":len(clean)-inserted}


def _window_days(days: int):
    if isinstance(days,bool) or not isinstance(days,int) or not 1 <= days <= 730:
        raise ValueError("days must be between 1 and 730")
    return days


def overview(conn, *, days: int):
    days=_window_days(days)
    with conn.cursor() as cur:
        cur.execute(
            """SELECT COUNT(*)::bigint AS event_count,
                      COUNT(DISTINCT user_id)::bigint AS active_users,
                      COUNT(DISTINCT session_id)::bigint AS sessions,
                      COUNT(*) FILTER (WHERE event_name='page_view')::bigint AS page_views,
                      COUNT(*) FILTER (WHERE event_name='click')::bigint AS clicks,
                      COUNT(*) FILTER (WHERE event_name='navigation')::bigint AS navigations,
                      COUNT(*) FILTER (WHERE event_name='feature_view')::bigint AS feature_views,
                      COALESCE(AVG(scroll_depth_pct),0)::numeric(7,2) AS avg_scroll_depth_pct,
                      COALESCE(SUM(active_duration_ms),0)::bigint AS active_duration_ms
               FROM analytics_events
               WHERE user_id IS NOT NULL AND occurred_at >= now()-(%s * interval '1 day')""",
            (days,),
        )
        row=cur.fetchone()
    if row["active_users"] < MIN_ANALYTICS_COHORT:
        return {"suppressed":True,"minimum_cohort":MIN_ANALYTICS_COHORT}
    return {"suppressed":False,**row}


def top_pages(conn, *, days: int, limit: int = 20):
    days=_window_days(days)
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    with conn.cursor() as cur:
        cur.execute(
            """SELECT page_path,COUNT(*)::bigint AS views,
                      COUNT(DISTINCT user_id)::bigint AS users,
                      COUNT(DISTINCT session_id)::bigint AS sessions
               FROM analytics_events
               WHERE user_id IS NOT NULL AND event_name='page_view'
                 AND occurred_at >= now()-(%s * interval '1 day')
               GROUP BY page_path
               HAVING COUNT(DISTINCT user_id) >= %s
               ORDER BY views DESC,page_path LIMIT %s""",
            (days,MIN_ANALYTICS_COHORT,limit),
        )
        return cur.fetchall()


def heatmap(conn, *, page_path: str, days: int):
    days=_window_days(days)
    page_path=normalize_page_path(page_path)
    with conn.cursor() as cur:
        cur.execute(
            """SELECT (floor(x_pct/5)*5)::integer AS x_bin,
                      (floor(y_pct/5)*5)::integer AS y_bin,
                      COUNT(*)::bigint AS clicks,
                      COUNT(DISTINCT user_id)::bigint AS users
               FROM analytics_events
               WHERE user_id IS NOT NULL AND event_name='click'
                 AND page_path=%s AND x_pct IS NOT NULL AND y_pct IS NOT NULL
                 AND occurred_at >= now()-(%s * interval '1 day')
               GROUP BY x_bin,y_bin
               HAVING COUNT(DISTINCT user_id) >= %s
               ORDER BY clicks DESC""",
            (page_path,days,MIN_ANALYTICS_COHORT),
        )
        return cur.fetchall()


def funnel(conn, *, steps: list[str], days: int):
    """Measure an ordered user journey while suppressing small step counts."""
    days=_window_days(days)
    if not 2 <= len(steps) <= 5 or any(step not in ALLOWED_EVENT_NAMES for step in steps):
        raise ValueError("funnel must contain 2-5 supported event names")
    if len(set(steps)) != len(steps):
        raise ValueError("funnel steps must be unique")

    # Build only the fixed CTE aliases; every event name remains a bound parameter.
    ctes=[
        """requested_steps AS (
             SELECT event_name,ordinality::integer AS step_no
             FROM unnest(%s::text[]) WITH ORDINALITY AS s(event_name,ordinality)
           )""",
        """step_1 AS (
             SELECT DISTINCT ON (e.user_id) e.user_id,e.occurred_at AS step_at
             FROM analytics_events e
             WHERE e.user_id IS NOT NULL
               AND e.event_name=(SELECT event_name FROM requested_steps WHERE step_no=1)
               AND e.occurred_at >= now()-(%s * interval '1 day')
             ORDER BY e.user_id,e.occurred_at
           )""",
    ]
    for step_no in range(2,len(steps)+1):
        previous=f"step_{step_no-1}"
        current=f"step_{step_no}"
        ctes.append(
            f"""{current} AS (
                 SELECT prior.user_id,next_event.occurred_at AS step_at
                 FROM {previous} prior
                 JOIN LATERAL (
                   SELECT e.occurred_at FROM analytics_events e
                   WHERE e.user_id=prior.user_id
                     AND e.event_name=(SELECT event_name FROM requested_steps WHERE step_no={step_no})
                     AND e.occurred_at>=prior.step_at
                     AND e.occurred_at<prior.step_at+interval '30 days'
                   ORDER BY e.occurred_at LIMIT 1
                 ) next_event ON TRUE
               )"""
        )
    outputs=[]
    for step_no in range(1,len(steps)+1):
        outputs.append(
            f"""SELECT {step_no}::integer AS step_no,
                       (SELECT event_name FROM requested_steps WHERE step_no={step_no}) AS event_name,
                       (SELECT COUNT(DISTINCT user_id) FROM step_{step_no})::bigint AS users"""
        )
    query="WITH "+",\n".join(ctes)+"\n"+" UNION ALL ".join(outputs)+" ORDER BY step_no"
    with conn.cursor() as cur:
        cur.execute(query,(steps,days))
        rows=cur.fetchall()
    cohort_size=rows[0]["users"] if rows else 0
    if cohort_size < MIN_ANALYTICS_COHORT:
        return {"suppressed":True,"minimum_cohort":MIN_ANALYTICS_COHORT,"steps":[]}
    safe_steps=[]
    for row in rows:
        if row["users"] < MIN_ANALYTICS_COHORT:
            safe_steps.append({"step_no":row["step_no"],"event_name":row["event_name"],
                               "users":None,"suppressed":True})
        else:
            safe_steps.append({**row,"suppressed":False})
    return {"suppressed":False,"minimum_cohort":MIN_ANALYTICS_COHORT,"steps":safe_steps}


def retention(conn, *, days: int):
    """Return weekly cohorts; suppress cohort or maturity counts below threshold."""
    days=_window_days(days)
    with conn.cursor() as cur:
        cur.execute(
            """WITH first_seen AS (
                 SELECT user_id,MIN(occurred_at) AS first_at
                 FROM analytics_events
                 WHERE user_id IS NOT NULL AND event_name='page_view'
                   AND occurred_at >= now()-(%s * interval '1 day')
                 GROUP BY user_id
               ),
               cohort_users AS (
                 SELECT user_id,first_at,date_trunc('week',first_at)::date AS cohort_week
                 FROM first_seen
               )
               SELECT cohort_week,COUNT(*)::bigint AS cohort_size,
                 COUNT(*) FILTER (
                   WHERE first_at <= now()-interval '8 days'
                 )::bigint AS eligible_7d,
                 COUNT(*) FILTER (
                   WHERE first_at <= now()-interval '8 days'
                     AND EXISTS (
                       SELECT 1 FROM analytics_events e
                       WHERE e.user_id=cohort_users.user_id
                         AND e.occurred_at >= first_at+interval '6 days'
                         AND e.occurred_at < first_at+interval '8 days'
                     )
                 )::bigint AS retained_7d,
                 COUNT(*) FILTER (
                   WHERE first_at <= now()-interval '31 days'
                 )::bigint AS eligible_30d,
                 COUNT(*) FILTER (
                   WHERE first_at <= now()-interval '31 days'
                     AND EXISTS (
                       SELECT 1 FROM analytics_events e
                       WHERE e.user_id=cohort_users.user_id
                         AND e.occurred_at >= first_at+interval '29 days'
                         AND e.occurred_at < first_at+interval '31 days'
                     )
                 )::bigint AS retained_30d
               FROM cohort_users
               GROUP BY cohort_week ORDER BY cohort_week DESC""",
            (days,),
        )
        rows=cur.fetchall()
    result=[]
    for row in rows:
        if row["cohort_size"] < MIN_ANALYTICS_COHORT:
            result.append({"cohort_week":row["cohort_week"],"suppressed":True})
            continue
        eligible_7d=row["eligible_7d"]
        eligible_30d=row["eligible_30d"]
        show_7d=eligible_7d >= MIN_ANALYTICS_COHORT
        show_30d=eligible_30d >= MIN_ANALYTICS_COHORT
        result.append({
            "cohort_week":row["cohort_week"],
            "cohort_size":row["cohort_size"],
            "suppressed":False,
            "eligible_7d":eligible_7d if show_7d else None,
            "retained_7d":row["retained_7d"] if show_7d else None,
            "retention_7d_rate":(
                round(row["retained_7d"]/eligible_7d,4) if show_7d else None
            ),
            "eligible_30d":eligible_30d if show_30d else None,
            "retained_30d":row["retained_30d"] if show_30d else None,
            "retention_30d_rate":(
                round(row["retained_30d"]/eligible_30d,4) if show_30d else None
            ),
        })
    return result

def user_event_history(conn, *, user_id: UUID, limit: int = 100):
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    with conn.cursor() as cur:
        cur.execute(
            """SELECT event_name,page_path,component_id,element_role,occurred_at,
                      viewport_width,viewport_height,scroll_depth_pct,active_duration_ms
               FROM analytics_events
               WHERE user_id=%s ORDER BY occurred_at DESC,id DESC LIMIT %s""",
            (user_id,limit),
        )
        return cur.fetchall()


def purge_expired_events(conn, *, retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
    """Maintenance helper; invoke from a trusted scheduled worker, never a public route."""
    if isinstance(retention_days,bool) or not isinstance(retention_days,int) or not 30 <= retention_days <= 730:
        raise ValueError("retention_days must be between 30 and 730")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """DELETE FROM analytics_events
                   WHERE occurred_at < now()-(%s * interval '1 day')""",
                (retention_days,),
            )
            return cur.rowcount
