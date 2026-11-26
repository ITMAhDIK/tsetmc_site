"""Consent-gated product analytics for the independent SaaS data domain.

This module deliberately has no HTTP server or market-database dependency.
The caller must supply identity from a verified server-side session and an
anonymous/session identifier minted by the first-party application.
"""
from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

_ALLOWED_EVENT_NAMES = frozenset({
    "page_view",
    "navigation",
    "click",
    "scroll_depth",
    "section_view",
    "active_time",
    "product_action",
})
_EVENT_METADATA_KEYS = {
    "page_view": frozenset({"source", "page_kind"}),
    "navigation": frozenset({"navigation_type", "destination_section", "source"}),
    "click": frozenset({"action", "target_component_id", "feature_code"}),
    "scroll_depth": frozenset({"milestone"}),
    "section_view": frozenset({"section_id"}),
    "active_time": frozenset({"section_id"}),
    "product_action": frozenset({"action", "feature_code", "funnel", "step", "variant"}),
}
_ALLOWED_ELEMENT_ROLES = frozenset({
    "button", "link", "tab", "menuitem", "navigation", "chart", "section", "other",
})
_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,79}$")
_COMPONENT_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,159}$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_SCROLL_MILESTONES = frozenset({25, 50, 75, 90, 100})
_MAX_BATCH_SIZE = 50
_MAX_ACTIVE_DURATION_MS = 15 * 60 * 1000
_MAX_EVENT_AGE = timedelta(days=30)
_MAX_FUTURE_SKEW = timedelta(minutes=5)


class ProductAnalyticsError(ValueError):
    """Validated analytics operation error with an HTTP-friendly status."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class AnalyticsConsentRequired(ProductAnalyticsError):
    """Raised when an event arrives without current explicit opt-in."""

    def __init__(self):
        super().__init__("Product analytics consent is not active.", status=403)


class AnalyticsPermissionDenied(ProductAnalyticsError):
    """Raised when a caller lacks the server-side analytics permission."""

    def __init__(self):
        super().__init__("Permission analytics.read is required.", status=403)


def _as_uuid(value, field: str, *, optional: bool = False):
    if value is None and optional:
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise ProductAnalyticsError("%s must be a valid UUID." % field) from None


def _as_utc_datetime(value, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ProductAnalyticsError("%s must be an ISO-8601 timestamp." % field) from None
    else:
        raise ProductAnalyticsError("%s must be an ISO-8601 timestamp." % field)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProductAnalyticsError("%s must include a timezone." % field)
    return parsed.astimezone(timezone.utc)


def _row_value(row, key: str, index: int):
    if isinstance(row, Mapping):
        return row[key]
    return row[index]


class ProductAnalyticsService:
    """Consent-aware event ingestion and privacy-scoped product reports.

    connection_factory must return a DB-API connection to the dedicated SaaS
    PostgreSQL database. permission_checker(actor_uuid, permission_code) must
    evaluate server-side roles; it must not trust a browser-supplied role.
    """

    def __init__(
        self,
        *,
        connection_factory,
        permission_checker,
        published_consent_version: str,
        clock=None,
        retention_days: int = 365,
    ):
        version = str(published_consent_version or "").strip()
        if not version or len(version) > 24:
            raise ValueError("A published consent version (1–24 chars) is required.")
        if not isinstance(retention_days, int) or isinstance(retention_days, bool):
            raise ValueError("retention_days must be an integer.")
        if retention_days < 1 or retention_days > 3650:
            raise ValueError("retention_days must be between 1 and 3650.")
        if not callable(connection_factory) or not callable(permission_checker):
            raise ValueError("Database and permission dependencies must be callable.")
        self._connection_factory = connection_factory
        self._permission_checker = permission_checker
        self.published_consent_version = version
        self.retention_days = retention_days
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise RuntimeError("The analytics clock must return a timezone-aware datetime.")
        return now.astimezone(timezone.utc)

    @contextmanager
    def _transaction(self):
        connection = self._connection_factory()
        cursor = None
        try:
            cursor = connection.cursor()
            yield cursor
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            if cursor is not None:
                cursor.close()
            connection.close()

    def set_consent(
        self,
        *,
        product_analytics: bool,
        consent_version: str,
        user_id=None,
        anonymous_id=None,
    ) -> dict:
        """Record the current opt-in/out and append an immutable consent history row."""
        if type(product_analytics) is not bool:
            raise ProductAnalyticsError("product_analytics must be a boolean.")
        if consent_version != self.published_consent_version:
            raise ProductAnalyticsError("The consent version is not the currently published version.")
        uid = _as_uuid(user_id, "user_id", optional=True)
        aid = _as_uuid(anonymous_id, "anonymous_id", optional=True)
        if (uid is None) == (aid is None):
            raise ProductAnalyticsError("Supply exactly one of user_id or anonymous_id.")

        with self._transaction() as cursor:
            if uid is not None:
                cursor.execute(
                    """
                    INSERT INTO analytics_consents
                        (user_id, essential, product_analytics, consent_version, updated_at)
                    VALUES (%s, TRUE, %s, %s, now())
                    ON CONFLICT (user_id) DO UPDATE SET
                        essential = TRUE,
                        product_analytics = EXCLUDED.product_analytics,
                        consent_version = EXCLUDED.consent_version,
                        updated_at = now()
                    """,
                    (uid, product_analytics, self.published_consent_version),
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO analytics_anonymous_consents
                        (anonymous_id, product_analytics, consent_version, updated_at)
                    VALUES (%s, %s, %s, now())
                    ON CONFLICT (anonymous_id) DO UPDATE SET
                        product_analytics = EXCLUDED.product_analytics,
                        consent_version = EXCLUDED.consent_version,
                        updated_at = now()
                    """,
                    (aid, product_analytics, self.published_consent_version),
                )
            cursor.execute(
                """
                INSERT INTO analytics_consent_history
                    (user_id, anonymous_id, product_analytics, consent_version, changed_at)
                VALUES (%s, %s, %s, %s, now())
                """,
                (uid, aid, product_analytics, self.published_consent_version),
            )
        return {
            "product_analytics": product_analytics,
            "consent_version": self.published_consent_version,
        }

    @staticmethod
    def _validate_page_path(value: str) -> str:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 300
            or not value.startswith("/")
            or value.startswith("//")
            or any(token in value for token in ("?", "#", "\\"))
            or any(ord(char) < 32 for char in value)
        ):
            raise ProductAnalyticsError("page_path must be a clean same-origin path without query or fragment.")
        return value

    @staticmethod
    def _validate_metadata(event_name: str, value) -> dict:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise ProductAnalyticsError("metadata must be an object.")
        allowed = _EVENT_METADATA_KEYS[event_name]
        result = {}
        for key, item in value.items():
            if not isinstance(key, str) or key not in allowed:
                raise ProductAnalyticsError("metadata contains a field that is not allowed for this event.")
            if isinstance(item, str):
                if not _IDENTIFIER_RE.fullmatch(item):
                    raise ProductAnalyticsError("metadata values must be short identifiers, not free text.")
            elif isinstance(item, bool):
                pass
            elif isinstance(item, int) and 0 <= item <= 1000000:
                pass
            else:
                raise ProductAnalyticsError("metadata values must be safe identifiers, booleans or bounded integers.")
            result[key] = item
        if event_name == "product_action" and not isinstance(result.get("action"), str):
            raise ProductAnalyticsError("product_action requires a stable action identifier.")
        return result

    def _validate_event(self, raw, now: datetime) -> dict:
        if not isinstance(raw, Mapping):
            raise ProductAnalyticsError("Each analytics event must be an object.")
        allowed_fields = {
            "event_id", "event_name", "page_path", "component_id", "element_role",
            "occurred_at", "viewport_width", "viewport_height", "scroll_depth_pct",
            "active_duration_ms", "metadata",
        }
        if set(raw) - allowed_fields:
            raise ProductAnalyticsError("Event contains unapproved fields; identity and form values are never accepted.")
        event_name = raw.get("event_name")
        if not isinstance(event_name, str) or event_name not in _ALLOWED_EVENT_NAMES:
            raise ProductAnalyticsError("event_name is not in the approved event vocabulary.")
        event_id = _as_uuid(raw.get("event_id"), "event_id")
        page_path = self._validate_page_path(raw.get("page_path"))

        component_id = raw.get("component_id")
        if component_id is not None and (
            not isinstance(component_id, str) or not _COMPONENT_ID_RE.fullmatch(component_id)
        ):
            raise ProductAnalyticsError("component_id must be a stable component identifier.")
        element_role = raw.get("element_role")
        if element_role is not None and (not isinstance(element_role, str) or element_role not in _ALLOWED_ELEMENT_ROLES):
            raise ProductAnalyticsError("element_role is not eligible for analytics.")
        if event_name == "click" and (component_id is None or element_role is None):
            raise ProductAnalyticsError("click events require a stable component_id and eligible element_role.")
        if event_name == "section_view" and component_id is None:
            raise ProductAnalyticsError("section_view events require a stable component_id.")

        scroll_depth = raw.get("scroll_depth_pct")
        if event_name == "scroll_depth":
            if isinstance(scroll_depth, bool) or not isinstance(scroll_depth, int) or scroll_depth not in _SCROLL_MILESTONES:
                raise ProductAnalyticsError("scroll_depth_pct must be one of 25, 50, 75, 90 or 100.")
        elif scroll_depth is not None:
            raise ProductAnalyticsError("scroll_depth_pct is valid only for scroll_depth events.")

        active_duration = raw.get("active_duration_ms")
        if event_name == "active_time":
            if (
                not isinstance(active_duration, int)
                or isinstance(active_duration, bool)
                or active_duration < 1
                or active_duration > _MAX_ACTIVE_DURATION_MS
            ):
                raise ProductAnalyticsError("active_duration_ms must be between 1 and 900000.")
        elif active_duration is not None:
            raise ProductAnalyticsError("active_duration_ms is valid only for active_time events.")

        viewport = {}
        for key in ("viewport_width", "viewport_height"):
            value = raw.get(key)
            if value is not None:
                if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 10000:
                    raise ProductAnalyticsError("%s must be an integer between 1 and 10000." % key)
            viewport[key] = value

        occurred_at = _as_utc_datetime(raw["occurred_at"], "occurred_at") if raw.get("occurred_at") is not None else now
        if occurred_at < now - _MAX_EVENT_AGE or occurred_at > now + _MAX_FUTURE_SKEW:
            raise ProductAnalyticsError("occurred_at is outside the accepted event time window.")

        metadata = self._validate_metadata(event_name, raw.get("metadata"))
        return {
            "event_id": event_id,
            "event_name": event_name,
            "page_path": page_path,
            "component_id": component_id,
            "element_role": element_role,
            "occurred_at": occurred_at,
            "viewport_width": viewport["viewport_width"],
            "viewport_height": viewport["viewport_height"],
            "scroll_depth_pct": scroll_depth,
            "active_duration_ms": active_duration,
            "metadata": metadata,
        }

    @staticmethod
    def _has_consent(cursor, user_id, anonymous_id) -> bool:
        if user_id is not None:
            cursor.execute(
                "SELECT product_analytics FROM analytics_consents WHERE user_id = %s",
                (user_id,),
            )
        else:
            cursor.execute(
                "SELECT product_analytics FROM analytics_anonymous_consents WHERE anonymous_id = %s",
                (anonymous_id,),
            )
        row = cursor.fetchone()
        return bool(_row_value(row, "product_analytics", 0)) if row is not None else False

    def ingest_events(
        self,
        events,
        *,
        user_id=None,
        anonymous_id=None,
        session_id=None,
    ) -> dict:
        """Insert a bounded event batch only while the trusted identity has opted in.

        event_id is client-generated UUID idempotency. Identity values are separate
        trusted arguments and are never accepted from an event body.
        """
        uid = _as_uuid(user_id, "user_id", optional=True)
        aid = _as_uuid(anonymous_id, "anonymous_id", optional=True)
        sid = _as_uuid(session_id, "session_id", optional=True)
        if uid is None and aid is None:
            raise ProductAnalyticsError("A verified user_id or pseudonymous anonymous_id is required.")
        if events is None or not isinstance(events, (list, tuple)) or not events:
            raise ProductAnalyticsError("events must be a non-empty array.")
        if len(events) > _MAX_BATCH_SIZE:
            raise ProductAnalyticsError("At most 50 events can be ingested per request.")

        now = self._now()
        normalized = [self._validate_event(event, now) for event in events]
        accepted = 0
        duplicates = 0
        insert_sql = """
            INSERT INTO analytics_events (
                event_id, user_id, anonymous_id, session_id, event_name, page_path,
                component_id, element_role, occurred_at, viewport_width,
                viewport_height, scroll_depth_pct, active_duration_ms, metadata
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb
            )
            ON CONFLICT (event_id) WHERE event_id IS NOT NULL DO NOTHING
        """
        with self._transaction() as cursor:
            if not self._has_consent(cursor, uid, aid):
                raise AnalyticsConsentRequired()
            for event in normalized:
                cursor.execute(
                    insert_sql,
                    (
                        event["event_id"], uid, aid, sid, event["event_name"],
                        event["page_path"], event["component_id"], event["element_role"],
                        event["occurred_at"], event["viewport_width"],
                        event["viewport_height"], event["scroll_depth_pct"],
                        event["active_duration_ms"],
                        json.dumps(event["metadata"], ensure_ascii=False, separators=(",", ":")),
                    ),
                )
                if cursor.rowcount > 0:
                    accepted += 1
                else:
                    duplicates += 1
        return {"accepted": accepted, "duplicates": duplicates}

    def _require_permission(self, actor_user_id):
        actor = _as_uuid(actor_user_id, "actor_user_id")
        if not self._permission_checker(actor, "analytics.read"):
            raise AnalyticsPermissionDenied()
        return actor

    @staticmethod
    def _date_window(start_at, end_at, *, max_days: int = 366):
        start = _as_utc_datetime(start_at, "start_at")
        end = _as_utc_datetime(end_at, "end_at")
        if start >= end:
            raise ProductAnalyticsError("start_at must be earlier than end_at.")
        if end - start > timedelta(days=max_days):
            raise ProductAnalyticsError("The requested analytics window is too large.")
        return start, end

    @staticmethod
    def _limit(value, default: int, maximum: int) -> int:
        if value is None:
            return default
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise ProductAnalyticsError("limit must be an integer.") from None
        if isinstance(value, bool) or parsed < 1 or parsed > maximum:
            raise ProductAnalyticsError("limit must be between 1 and %d." % maximum)
        return parsed

    @staticmethod
    def _row_dict(row, columns):
        if isinstance(row, Mapping):
            return {column: row[column] for column in columns}
        return dict(zip(columns, row))

    def component_heatmap(
        self,
        *,
        actor_user_id,
        start_at,
        end_at,
        page_path=None,
        limit=200,
    ) -> dict:
        """Return component-level engagement counts, never pointer coordinates."""
        actor = self._require_permission(actor_user_id)
        start, end = self._date_window(start_at, end_at)
        limit_value = self._limit(limit, 200, 1000)
        sql = """
            SELECT page_path, component_id,
                   COUNT(*) FILTER (WHERE event_name = 'click') AS clicks,
                   COUNT(*) FILTER (WHERE event_name = 'section_view') AS section_views,
                   COALESCE(SUM(active_duration_ms)
                       FILTER (WHERE event_name = 'active_time'), 0) AS active_duration_ms
            FROM analytics_events
            WHERE occurred_at >= %s AND occurred_at < %s
        """
        params = [start, end]
        if page_path is not None:
            sql += " AND page_path = %s"
            params.append(self._validate_page_path(page_path))
        sql += """
            GROUP BY page_path, component_id
            ORDER BY clicks DESC, section_views DESC, active_duration_ms DESC
            LIMIT %s
        """
        params.append(limit_value)
        with self._transaction() as cursor:
            cursor.execute(sql, tuple(params))
            rows = cursor.fetchall()
        columns = ("page_path", "component_id", "clicks", "section_views", "active_duration_ms")
        return {
            "rows": [self._row_dict(row, columns) for row in rows],
            "window": {"start_at": start.isoformat(), "end_at": end.isoformat()},
        }

    def funnel_report(self, *, actor_user_id, steps, start_at, end_at) -> dict:
        """Count visitors that completed an ordered sequence of approved event types."""
        actor = self._require_permission(actor_user_id)
        start, end = self._date_window(start_at, end_at, max_days=90)
        if (
            not isinstance(steps, (list, tuple))
            or not 2 <= len(steps) <= 8
            or any(not isinstance(step, str) or step not in _ALLOWED_EVENT_NAMES for step in steps)
        ):
            raise ProductAnalyticsError("steps must contain 2–8 approved event names.")
        ctes = [
            """
            step_0 AS (
                SELECT COALESCE(user_id::text, anonymous_id::text) AS visitor_id,
                       MIN(occurred_at) AS step_time
                FROM analytics_events
                WHERE event_name = %s AND occurred_at >= %s AND occurred_at < %s
                  AND (user_id IS NOT NULL OR anonymous_id IS NOT NULL)
                GROUP BY COALESCE(user_id::text, anonymous_id::text)
            )
            """
        ]
        params = [steps[0], start, end]
        for index, step in enumerate(steps[1:], start=1):
            previous = index - 1
            ctes.append(
                """
                step_%d AS (
                    SELECT prior.visitor_id, MIN(events.occurred_at) AS step_time
                    FROM step_%d prior
                    JOIN analytics_events events
                      ON COALESCE(events.user_id::text, events.anonymous_id::text) = prior.visitor_id
                    WHERE events.event_name = %%s
                      AND events.occurred_at > prior.step_time
                      AND events.occurred_at < %%s
                    GROUP BY prior.visitor_id
                )
                """ % (index, previous)
            )
            params.extend([step, end])
        counts = ", ".join(
            "(SELECT COUNT(*) FROM step_%d) AS step_%d_count" % (index, index)
            for index in range(len(steps))
        )
        sql = "WITH " + ", ".join(ctes) + " SELECT " + counts
        with self._transaction() as cursor:
            cursor.execute(sql, tuple(params))
            row = cursor.fetchone()
        values = [
            int(_row_value(row, "step_%d_count" % index, index) or 0)
            for index in range(len(steps))
        ]
        return {
            "steps": [
                {"event_name": event_name, "visitors": values[index]}
                for index, event_name in enumerate(steps)
            ],
            "window": {"start_at": start.isoformat(), "end_at": end.isoformat()},
        }

    def cohort_retention(
        self,
        *,
        actor_user_id,
        start_at,
        end_at,
        activity_event="page_view",
        max_weeks=12,
    ) -> dict:
        """Return weekly cohort activity counts without exposing visitor identities."""
        self._require_permission(actor_user_id)
        start, end = self._date_window(start_at, end_at)
        if not isinstance(activity_event, str) or activity_event not in _ALLOWED_EVENT_NAMES:
            raise ProductAnalyticsError("activity_event is not approved.")
        weeks = self._limit(max_weeks, 12, 26)
        sql = """
            WITH activity AS (
                SELECT COALESCE(user_id::text, anonymous_id::text) AS visitor_id,
                       date_trunc('week', occurred_at) AS active_week
                FROM analytics_events
                WHERE event_name = %s AND occurred_at >= %s AND occurred_at < %s
                  AND (user_id IS NOT NULL OR anonymous_id IS NOT NULL)
                GROUP BY COALESCE(user_id::text, anonymous_id::text),
                         date_trunc('week', occurred_at)
            ),
            cohorts AS (
                SELECT visitor_id, MIN(active_week) AS cohort_week
                FROM activity
                GROUP BY visitor_id
            )
            SELECT cohorts.cohort_week::date,
                   FLOOR(EXTRACT(EPOCH FROM (activity.active_week - cohorts.cohort_week))
                         / 604800)::INTEGER AS period,
                   COUNT(DISTINCT activity.visitor_id) AS active_visitors
            FROM cohorts
            JOIN activity ON activity.visitor_id = cohorts.visitor_id
            WHERE activity.active_week >= cohorts.cohort_week
              AND FLOOR(EXTRACT(EPOCH FROM (activity.active_week - cohorts.cohort_week))
                        / 604800) BETWEEN 0 AND %s
            GROUP BY cohorts.cohort_week, period
            ORDER BY cohorts.cohort_week, period
        """
        with self._transaction() as cursor:
            cursor.execute(sql, (activity_event, start, end, weeks))
            rows = cursor.fetchall()
        columns = ("cohort_week", "period", "active_visitors")
        return {"rows": [self._row_dict(row, columns) for row in rows]}

    @staticmethod
    def _request_id(value) -> str:
        if not isinstance(value, str) or not _REQUEST_ID_RE.fullmatch(value):
            raise ProductAnalyticsError("request_id must be a short non-empty identifier.")
        return value

    def user_events(
        self,
        *,
        actor_user_id,
        target_user_id,
        request_id,
        start_at,
        end_at,
        limit=200,
    ) -> dict:
        """Read a single user's limited event stream with an immutable audit row."""
        actor = self._require_permission(actor_user_id)
        target = _as_uuid(target_user_id, "target_user_id")
        request = self._request_id(request_id)
        start, end = self._date_window(start_at, end_at, max_days=90)
        limit_value = self._limit(limit, 200, 500)
        columns = (
            "event_name", "page_path", "component_id", "element_role",
            "occurred_at", "viewport_width", "viewport_height",
            "scroll_depth_pct", "active_duration_ms", "metadata",
        )
        with self._transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO admin_audit_logs
                    (actor_user_id, action, target_type, target_id, request_id)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (actor, "analytics.user_events.read", "user", str(target), request),
            )
            cursor.execute(
                """
                SELECT event_name, page_path, component_id, element_role,
                       occurred_at, viewport_width, viewport_height,
                       scroll_depth_pct, active_duration_ms, metadata
                FROM analytics_events
                WHERE user_id = %s AND occurred_at >= %s AND occurred_at < %s
                ORDER BY occurred_at DESC
                LIMIT %s
                """,
                (target, start, end, limit_value),
            )
            rows = cursor.fetchall()
        return {"user_id": str(target), "events": [self._row_dict(row, columns) for row in rows]}

    def purge_expired_events(self, *, actor_user_id, request_id) -> dict:
        """Delete event-level records beyond the configured retention window."""
        actor = self._require_permission(actor_user_id)
        request = self._request_id(request_id)
        cutoff = self._now() - timedelta(days=self.retention_days)
        with self._transaction() as cursor:
            cursor.execute(
                "DELETE FROM analytics_events WHERE occurred_at < %s",
                (cutoff,),
            )
            deleted = max(0, int(cursor.rowcount or 0))
            cursor.execute(
                """
                INSERT INTO admin_audit_logs
                    (actor_user_id, action, target_type, target_id, request_id)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (actor, "analytics.events.purge", "retention", str(self.retention_days), request),
            )
        return {"deleted": deleted, "retention_days": self.retention_days}
