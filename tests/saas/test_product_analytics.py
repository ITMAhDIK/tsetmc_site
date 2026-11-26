"""Tests for the privacy-gated product analytics domain service."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from saas_core.domain.product_analytics import (
    AnalyticsConsentRequired,
    AnalyticsPermissionDenied,
    ProductAnalyticsError,
    ProductAnalyticsService,
)


NOW = datetime(2026, 11, 26, 18, 0, tzinfo=timezone.utc)
USER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
ANONYMOUS_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
SESSION_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
ACTOR_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")
CONSENT_VERSION = "privacy-v1"


class FakeCursor:
    def __init__(self, *, fetchone=None, fetchall=None, insert_rowcounts=(), rowcount=0):
        self.fetchone_result = fetchone
        self.fetchall_result = list(fetchall or [])
        self.insert_rowcounts = iter(insert_rowcounts)
        self.rowcount = rowcount
        self.calls = []
        self.closed = False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if "INSERT INTO analytics_events" in sql:
            self.rowcount = next(self.insert_rowcounts, 1)
        return self

    def fetchone(self):
        return self.fetchone_result

    def fetchall(self):
        return self.fetchall_result

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def make_service(cursor, *, allowed=True, factory_calls=None, retention_days=365):
    connection = FakeConnection(cursor)

    def factory():
        if factory_calls is not None:
            factory_calls.append(True)
        return connection

    service = ProductAnalyticsService(
        connection_factory=factory,
        permission_checker=lambda actor, permission: allowed and permission == "analytics.read",
        published_consent_version=CONSENT_VERSION,
        clock=lambda: NOW,
        retention_days=retention_days,
    )
    return service, connection


def click_event(event_id=None):
    return {
        "event_id": str(event_id or uuid.uuid4()),
        "event_name": "click",
        "page_path": "/stocks/فولاد",
        "component_id": "stock-shareholder-tab",
        "element_role": "tab",
        "occurred_at": NOW.isoformat(),
        "viewport_width": 1440,
        "viewport_height": 900,
        "metadata": {"action": "open_section"},
    }


def test_user_consent_upserts_current_state_and_appends_history():
    cursor = FakeCursor()
    service, connection = make_service(cursor)

    result = service.set_consent(
        user_id=USER_ID,
        product_analytics=True,
        consent_version=CONSENT_VERSION,
    )

    assert result == {"product_analytics": True, "consent_version": CONSENT_VERSION}
    assert "INSERT INTO analytics_consents" in cursor.calls[0][0]
    assert "ON CONFLICT (user_id) DO UPDATE" in cursor.calls[0][0]
    assert "INSERT INTO analytics_consent_history" in cursor.calls[1][0]
    assert cursor.calls[1][1] == (USER_ID, None, True, CONSENT_VERSION)
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_anonymous_consent_is_current_and_auditable():
    cursor = FakeCursor()
    service, connection = make_service(cursor)

    service.set_consent(
        anonymous_id=ANONYMOUS_ID,
        product_analytics=True,
        consent_version=CONSENT_VERSION,
    )

    assert "analytics_anonymous_consents" in cursor.calls[0][0]
    assert "analytics_consent_history" in cursor.calls[1][0]
    assert cursor.calls[1][1] == (None, ANONYMOUS_ID, True, CONSENT_VERSION)
    assert connection.commits == 1


def test_consent_rejects_stale_version_and_ambiguous_identity_before_database_access():
    calls = []
    cursor = FakeCursor()
    service, _ = make_service(cursor, factory_calls=calls)

    with pytest.raises(ProductAnalyticsError):
        service.set_consent(
            user_id=USER_ID,
            product_analytics=True,
            consent_version="old-version",
        )
    with pytest.raises(ProductAnalyticsError):
        service.set_consent(
            user_id=USER_ID,
            anonymous_id=ANONYMOUS_ID,
            product_analytics=True,
            consent_version=CONSENT_VERSION,
        )
    assert calls == []


def test_events_are_not_collected_without_explicit_current_consent():
    cursor = FakeCursor(fetchone=(False,))
    service, connection = make_service(cursor)

    with pytest.raises(AnalyticsConsentRequired):
        service.ingest_events(
            [click_event()],
            user_id=USER_ID,
            anonymous_id=ANONYMOUS_ID,
            session_id=SESSION_ID,
        )

    assert len(cursor.calls) == 1
    assert "SELECT product_analytics FROM analytics_consents" in cursor.calls[0][0]
    assert connection.rollbacks == 1
    assert connection.commits == 0


def test_ingestion_uses_trusted_identity_and_event_ids_for_idempotency():
    cursor = FakeCursor(fetchone=(True,), insert_rowcounts=(1, 0))
    service, connection = make_service(cursor)
    first = click_event()
    second = click_event()

    result = service.ingest_events(
        [first, second],
        user_id=USER_ID,
        anonymous_id=ANONYMOUS_ID,
        session_id=SESSION_ID,
    )

    assert result == {"accepted": 1, "duplicates": 1}
    insert_calls = [call for call in cursor.calls if "INSERT INTO analytics_events" in call[0]]
    assert len(insert_calls) == 2
    assert insert_calls[0][1][1:4] == (USER_ID, ANONYMOUS_ID, SESSION_ID)
    assert json.loads(insert_calls[0][1][-1]) == {"action": "open_section"}
    assert "ON CONFLICT (event_id)" in insert_calls[0][0]
    assert connection.commits == 1


def test_anonymous_ingestion_checks_anonymous_consent_store():
    cursor = FakeCursor(fetchone=(True,), insert_rowcounts=(1,))
    service, _ = make_service(cursor)

    result = service.ingest_events(
        [click_event()],
        anonymous_id=ANONYMOUS_ID,
        session_id=SESSION_ID,
    )

    assert result == {"accepted": 1, "duplicates": 0}
    assert "analytics_anonymous_consents" in cursor.calls[0][0]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda event: event.update({"value": "typed search text"}),
        lambda event: event.update({"phone": "+989121234567"}),
        lambda event: event.update({"page_path": "/stocks?search=private"}),
        lambda event: event.update({"metadata": {"label": "Raw user-entered text"}}),
        lambda event: event.update({"element_role": "textbox"}),
    ],
)
def test_event_validation_rejects_sensitive_or_unapproved_fields(mutate):
    cursor = FakeCursor(fetchone=(True,))
    calls = []
    service, _ = make_service(cursor, factory_calls=calls)
    event = click_event()
    mutate(event)

    with pytest.raises(ProductAnalyticsError):
        service.ingest_events([event], user_id=USER_ID)
    assert calls == []


def test_scroll_and_active_time_accept_only_bounded_measurements():
    cursor = FakeCursor(fetchone=(True,), insert_rowcounts=(1, 1))
    service, _ = make_service(cursor)
    scroll = {
        "event_id": str(uuid.uuid4()),
        "event_name": "scroll_depth",
        "page_path": "/market",
        "scroll_depth_pct": 75,
    }
    active = {
        "event_id": str(uuid.uuid4()),
        "event_name": "active_time",
        "page_path": "/market",
        "component_id": "market-overview",
        "active_duration_ms": 12000,
    }

    result = service.ingest_events([scroll, active], anonymous_id=ANONYMOUS_ID)

    assert result == {"accepted": 2, "duplicates": 0}
    assert cursor.calls[1][1][11] == 75
    assert cursor.calls[2][1][12] == 12000


def test_component_heatmap_is_aggregated_and_server_permission_gated():
    cursor = FakeCursor(
        fetchall=[("/stocks/فولاد", "shareholder-tab", 7, 12, 45000)]
    )
    service, connection = make_service(cursor)

    result = service.component_heatmap(
        actor_user_id=ACTOR_ID,
        start_at=NOW - timedelta(days=7),
        end_at=NOW,
        page_path="/stocks/فولاد",
    )

    assert result["rows"] == [{
        "page_path": "/stocks/فولاد",
        "component_id": "shareholder-tab",
        "clicks": 7,
        "section_views": 12,
        "active_duration_ms": 45000,
    }]
    assert "GROUP BY page_path, component_id" in cursor.calls[0][0]
    assert connection.commits == 1


def test_funnel_report_counts_ordered_steps():
    cursor = FakeCursor(fetchone=(100, 60, 25))
    service, _ = make_service(cursor)

    result = service.funnel_report(
        actor_user_id=ACTOR_ID,
        steps=["page_view", "click", "product_action"],
        start_at=NOW - timedelta(days=14),
        end_at=NOW,
    )

    assert result["steps"] == [
        {"event_name": "page_view", "visitors": 100},
        {"event_name": "click", "visitors": 60},
        {"event_name": "product_action", "visitors": 25},
    ]
    assert "step_2 AS" in cursor.calls[0][0]
    assert "events.occurred_at > prior.step_time" in cursor.calls[0][0]


def test_cohort_retention_is_aggregated_by_week():
    cursor = FakeCursor(fetchall=[("2026-11-02", 0, 100), ("2026-11-02", 1, 42)])
    service, _ = make_service(cursor)

    result = service.cohort_retention(
        actor_user_id=ACTOR_ID,
        start_at=NOW - timedelta(days=60),
        end_at=NOW,
    )

    assert result["rows"] == [
        {"cohort_week": "2026-11-02", "period": 0, "active_visitors": 100},
        {"cohort_week": "2026-11-02", "period": 1, "active_visitors": 42},
    ]
    assert "date_trunc('week'" in cursor.calls[0][0]


def test_user_level_events_require_permission_and_are_audited():
    cursor = FakeCursor(fetchall=[(
        "click", "/stocks/فولاد", "shareholder-tab", "tab", NOW,
        1440, 900, None, None, {"action": "open_section"},
    )])
    service, connection = make_service(cursor)

    result = service.user_events(
        actor_user_id=ACTOR_ID,
        target_user_id=USER_ID,
        request_id="req-analytics-001",
        start_at=NOW - timedelta(days=2),
        end_at=NOW,
    )

    assert result["user_id"] == str(USER_ID)
    assert result["events"][0]["event_name"] == "click"
    assert "INSERT INTO admin_audit_logs" in cursor.calls[0][0]
    assert "SELECT event_name, page_path" in cursor.calls[1][0]
    assert connection.commits == 1


def test_unauthorized_user_level_access_fails_before_database_access():
    calls = []
    cursor = FakeCursor()
    service, _ = make_service(cursor, allowed=False, factory_calls=calls)

    with pytest.raises(AnalyticsPermissionDenied):
        service.user_events(
            actor_user_id=ACTOR_ID,
            target_user_id=USER_ID,
            request_id="req-analytics-002",
            start_at=NOW - timedelta(days=1),
            end_at=NOW,
        )
    assert calls == []


def test_retention_purge_is_bounded_audited_and_configurable():
    cursor = FakeCursor(rowcount=13)
    service, connection = make_service(cursor, retention_days=180)

    result = service.purge_expired_events(
        actor_user_id=ACTOR_ID,
        request_id="req-retention-purge",
    )

    assert result == {"deleted": 13, "retention_days": 180}
    assert "DELETE FROM analytics_events WHERE occurred_at < %s" in cursor.calls[0][0]
    assert cursor.calls[0][1] == (NOW - timedelta(days=180),)
    assert "INSERT INTO admin_audit_logs" in cursor.calls[1][0]
    assert connection.commits == 1


def test_date_windows_and_event_batches_are_bounded():
    cursor = FakeCursor()
    service, _ = make_service(cursor)

    with pytest.raises(ProductAnalyticsError):
        service.component_heatmap(
            actor_user_id=ACTOR_ID,
            start_at=NOW - timedelta(days=400),
            end_at=NOW,
        )
    with pytest.raises(ProductAnalyticsError):
        service.ingest_events(
            [click_event() for _ in range(51)],
            user_id=USER_ID,
        )
