"""PostgreSQL integration tests for consent-aware Phase 7 analytics."""
import os
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from saas_core.analytics.service import get_funnels,get_overview,get_retention,get_user_activity,record_events,set_consent
from saas_core.auth.authorization import AuthorizationDenied

DSN=os.getenv("SAAS_TEST_DATABASE_URL")
pytestmark=pytest.mark.skipif(not DSN,reason="SAAS_TEST_DATABASE_URL is not configured")

@pytest.fixture(scope="session",autouse=True)
def migrated_analytics_database():
    root=os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    schema=os.path.join(root,"saas_core","schema")
    with psycopg.connect(DSN,autocommit=True,row_factory=dict_row) as conn:
        for name in ("001_initial.sql","002_payment_lifecycle.sql","003_wallet_referrals.sql",
                     "004_support.sql","005_analytics.sql","006_order_terms_snapshot.sql","005_analytics.sql","006_order_terms_snapshot.sql"):
            with open(os.path.join(schema,name),encoding="utf-8") as stream:
                conn.execute(stream.read())
    yield

def connect():
    return psycopg.connect(DSN,row_factory=dict_row)

def make_user(conn,code="analytics"):
    uid=uuid4()
    conn.execute("INSERT INTO users(id,public_id,username,status) VALUES (%s,%s,%s,'active')",
                 (uid,str(uid)[:32],f"{code}-{uid.hex[:16]}"))
    return uid

def grant(conn,user_id,role):
    role_id=conn.execute("SELECT id FROM roles WHERE code=%s",(role,)).fetchone()["id"]
    conn.execute("INSERT INTO user_roles(user_id,role_id) VALUES (%s,%s)",(user_id,role_id))

def event(name="page_view",**overrides):
    data={"event_id":uuid4(),"session_id":uuid4(),"event_name":name,"page_path":"/dashboard",
          "occurred_at":datetime.now(timezone.utc),"metadata":{}}
    data.update(overrides)
    return data

def test_consent_is_opt_in_and_revocation_blocks_future_ingestion(migrated_analytics_database):
    with connect() as conn:
        user=make_user(conn)
        with pytest.raises(PermissionError):
            record_events(conn,user_id=user,events=[event()])
        granted=set_consent(conn,user_id=user,enabled=True)
        assert granted["product_analytics"] is True and granted["essential"] is True
        set_consent(conn,user_id=user,enabled=True)
        assert conn.execute("SELECT count(*) AS n FROM analytics_consent_history WHERE user_id=%s",(user,)).fetchone()["n"]==1
        assert record_events(conn,user_id=user,events=[event()])["accepted"]==1
        revoked=set_consent(conn,user_id=user,enabled=False)
        assert revoked["product_analytics"] is False
        assert conn.execute("SELECT count(*) AS n FROM analytics_consent_history WHERE user_id=%s",(user,)).fetchone()["n"]==2
        with pytest.raises(psycopg.errors.RaiseException):
            with conn.transaction():
                conn.execute("UPDATE analytics_consent_history SET product_analytics=TRUE WHERE user_id=%s",(user,))
        with pytest.raises(PermissionError):
            record_events(conn,user_id=user,events=[event()])

def test_event_validation_rejects_unapproved_payload_and_query_string(migrated_analytics_database):
    with connect() as conn:
        user=make_user(conn);set_consent(conn,user_id=user,enabled=True)
        with pytest.raises(ValueError,match="unsupported fields"):
            record_events(conn,user_id=user,events=[event(metadata={"password":"secret"})])
        with pytest.raises(ValueError,match="normalized path"):
            record_events(conn,user_id=user,events=[event(page_path="/profile?email=private@example.com")])
        with pytest.raises(ValueError,match="timezone"):
            record_events(conn,user_id=user,events=[event(occurred_at=datetime.now())])
        assert conn.execute("SELECT count(*) AS n FROM analytics_events WHERE user_id=%s",(user,)).fetchone()["n"]==0

def test_batch_is_idempotent_and_user_identity_is_server_bound(migrated_analytics_database):
    with connect() as conn:
        user=make_user(conn);set_consent(conn,user_id=user,enabled=True)
        item=event("scroll",scroll_depth_pct=75.0)
        first=record_events(conn,user_id=user,events=[item,item])
        assert first=={"accepted":1,"duplicates":1}
        assert conn.execute("SELECT count(*) AS n FROM analytics_events WHERE user_id=%s",(user,)).fetchone()["n"]==1
        stored=conn.execute("SELECT user_id,consent_version FROM analytics_events WHERE client_event_id=%s",(item["event_id"],)).fetchone()
        assert stored["user_id"]==user and stored["consent_version"]=="product-analytics-v1"
        changed={**item,"page_path":"/different-page"}
        with pytest.raises(ValueError,match="reused with different event data"):
            record_events(conn,user_id=user,events=[changed])

def test_overview_funnel_and_retention_reports_use_aggregates(migrated_analytics_database):
    with connect() as conn:
        user=make_user(conn);admin=make_user(conn,"analytics-admin");grant(conn,admin,"admin")
        set_consent(conn,user_id=user,enabled=True)
        now=datetime.now(timezone.utc)
        record_events(conn,user_id=user,events=[
            event("page_view",occurred_at=now,page_path="/pricing"),
            event("click",occurred_at=now,page_path="/pricing",component_id="pricing-plan-card",element_role="card"),
            event("funnel_step",occurred_at=now,page_path="/pricing",metadata={"funnel_name":"subscribe","funnel_step":1}),
            event("funnel_step",occurred_at=now,page_path="/checkout",metadata={"funnel_name":"subscribe","funnel_step":2}),
        ])
        today=date.today()
        overview=get_overview(conn,actor_id=admin,start=today,end=today)
        assert sum(row["events"] for row in overview["daily"])>=4
        assert "/pricing" in {row["page_path"] for row in overview["top_pages"]}
        assert any(row["component_id"]=="pricing-plan-card" for row in overview["component_activity"])
        funnel=get_funnels(conn,actor_id=admin,start=today,end=today)
        assert [(row["step"],row["users"]) for row in funnel]==[(1,1),(2,1)]
        retention=get_retention(conn,actor_id=admin,start=today-timedelta(days=1),end=today)
        assert retention and any(row["retained_users"]>=1 for row in retention)

def test_user_level_activity_requires_separate_permission_and_is_audited(migrated_analytics_database):
    with connect() as conn:
        target=make_user(conn);admin=make_user(conn,"admin")
        grant(conn,admin,"admin");set_consent(conn,user_id=target,enabled=True)
        record_events(conn,user_id=target,events=[event("page_view",page_path="/portfolio")])
        with pytest.raises(AuthorizationDenied):
            get_user_activity(conn,actor_id=admin,user_id=target,start=date.today(),end=date.today())
        grant(conn,admin,"superadmin")
        report=get_user_activity(conn,actor_id=admin,user_id=target,start=date.today(),end=date.today())
        assert report["by_event"][0]["event_name"]=="page_view"
        assert conn.execute("SELECT count(*) AS n FROM admin_audit_logs WHERE action='analytics.user_activity.viewed' AND actor_user_id=%s",(admin,)).fetchone()["n"]==1

def test_analytics_routes_and_phase_six_permissions_remain_registered(migrated_analytics_database):
    from saas_core.auth.api import app
    paths=app.openapi()["paths"]
    assert "/analytics/consent" in paths and "/analytics/events" in paths
    assert "/analytics/admin/overview" in paths and "/analytics/admin/funnels" in paths
    assert "/analytics/admin/retention" in paths and "/analytics/admin/users/{user_id}" in paths
    with connect() as conn:
        support=make_user(conn);grant(conn,support,"support")
        from saas_core.auth.authorization import has_permission
        assert has_permission(conn,support,"tickets.manage")
        assert not has_permission(conn,support,"analytics.read")


def test_aggregate_report_service_requires_analytics_permission(migrated_analytics_database):
    with connect() as conn:
        ordinary=make_user(conn)
        today=date.today()
        with pytest.raises(AuthorizationDenied):
            get_overview(conn,actor_id=ordinary,start=today,end=today)
        with pytest.raises(AuthorizationDenied):
            get_funnels(conn,actor_id=ordinary,start=today,end=today)
        with pytest.raises(AuthorizationDenied):
            get_retention(conn,actor_id=ordinary,start=today,end=today)

def test_scroll_and_active_time_events_require_their_measurement(migrated_analytics_database):
    with connect() as conn:
        user=make_user(conn);set_consent(conn,user_id=user,enabled=True)
        with pytest.raises(ValueError,match="require scroll_depth_pct"):
            record_events(conn,user_id=user,events=[event("scroll")])
        with pytest.raises(ValueError,match="require active_duration_ms"):
            record_events(conn,user_id=user,events=[event("active_time")])
        with pytest.raises(ValueError,match="normalized path"):
            record_events(conn,user_id=user,events=[event(page_path="/account/./profile")])
        with pytest.raises(ValueError,match="unsupported fields"):
            record_events(conn,user_id=user,events=[event(metadata=["secret"])])
        assert conn.execute("SELECT count(*) AS n FROM analytics_events WHERE user_id=%s",(user,)).fetchone()["n"]==0
