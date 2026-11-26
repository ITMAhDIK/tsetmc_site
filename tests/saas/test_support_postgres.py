"""PostgreSQL integration tests for Phase 6 support and FAQ."""
import os
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from saas_core.auth.authorization import AuthorizationDenied
from saas_core.support.service import (
    add_internal_note, assign_ticket, change_ticket_status, create_faq,
    create_ticket, get_staff_ticket, get_user_ticket, list_faq,
    list_staff_tickets, list_user_tickets, reply_user_ticket,
    staff_reply_ticket, update_faq,
)

DSN=os.getenv("SAAS_TEST_DATABASE_URL")
pytestmark=pytest.mark.skipif(not DSN,reason="SAAS_TEST_DATABASE_URL is not configured")


@pytest.fixture(scope="session",autouse=True)
def migrated_support_database():
    root=os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    schema=os.path.join(root,"saas_core","schema")
    with psycopg.connect(DSN,autocommit=True,row_factory=dict_row) as conn:
        for name in ("001_initial.sql","002_payment_lifecycle.sql","003_wallet_referrals.sql",
                     "004_support.sql","004_support.sql"):
            with open(os.path.join(schema,name),encoding="utf-8") as stream:
                conn.execute(stream.read())
    yield


def connect():
    return psycopg.connect(DSN,row_factory=dict_row)


def make_user(conn):
    uid=uuid4()
    conn.execute(
        """INSERT INTO users(id,public_id,username,status)
           VALUES (%s,%s,%s,'active')""",
        (uid,str(uid)[:32],f"support-{uid.hex[:16]}"),
    )
    return uid


def grant_role(conn,user_id,role_code):
    row=conn.execute("SELECT id FROM roles WHERE code=%s",(role_code,)).fetchone()
    conn.execute("INSERT INTO user_roles(user_id,role_id) VALUES (%s,%s)",(user_id,row["id"]))


def new_ticket(conn,user_id,subject="Need help with account"):
    return create_ticket(
        conn,user_id=user_id,subject=subject,category="account",message="Please help me",
    )


def test_ticket_creation_and_user_ownership_isolation(migrated_support_database):
    with connect() as conn:
        owner=make_user(conn)
        other=make_user(conn)
        ticket=new_ticket(conn,owner)
        assert ticket["status"]=="open"
        assert ticket["ticket_number"] is not None
        detail=get_user_ticket(conn,user_id=owner,ticket_id=ticket["id"])
        assert len(detail["messages"])==1
        assert detail["messages"][0]["body"]=="Please help me"
        assert [row["id"] for row in list_user_tickets(conn,user_id=owner)]==[ticket["id"]]
        assert list_user_tickets(conn,user_id=other)==[]
        with pytest.raises(LookupError,match="ticket not found"):
            get_user_ticket(conn,user_id=other,ticket_id=ticket["id"])


def test_internal_notes_are_visible_to_staff_but_never_to_ticket_owner(migrated_support_database):
    with connect() as conn:
        owner=make_user(conn)
        staff=make_user(conn)
        grant_role(conn,staff,"support")
        ticket=new_ticket(conn,owner)
        note=add_internal_note(
            conn,ticket_id=ticket["id"],actor_id=staff,note="Escalate after checking logs",
        )
        assert note["is_internal_note"] is True
        owner_view=get_user_ticket(conn,user_id=owner,ticket_id=ticket["id"])
        staff_view=get_staff_ticket(conn,ticket_id=ticket["id"])
        assert len(owner_view["messages"])==1
        assert all(not m["is_internal_note"] for m in owner_view["messages"])
        assert len(staff_view["messages"])==2
        internal=[m for m in staff_view["messages"] if m["is_internal_note"]]
        assert len(internal)==1
        assert internal[0]["body"]=="Escalate after checking logs"


def test_assignment_requires_active_support_agent_and_is_audited(migrated_support_database):
    with connect() as conn:
        owner=make_user(conn)
        admin=make_user(conn)
        agent=make_user(conn)
        ordinary_user=make_user(conn)
        grant_role(conn,admin,"admin")
        grant_role(conn,agent,"support")
        ticket=new_ticket(conn,owner)
        with pytest.raises(AuthorizationDenied):
            assign_ticket(conn,ticket_id=ticket["id"],actor_id=ordinary_user,assignee_id=agent)
        assigned=assign_ticket(conn,ticket_id=ticket["id"],actor_id=admin,assignee_id=agent)
        assert assigned["assigned_to"]==agent
        with pytest.raises(AuthorizationDenied):
            assign_ticket(conn,ticket_id=ticket["id"],actor_id=admin,assignee_id=ordinary_user)
        assert conn.execute(
            """SELECT count(*) AS n FROM admin_audit_logs
               WHERE action='support.ticket.assigned' AND target_id=%s""",(str(ticket["id"]),)
        ).fetchone()["n"]==1


def test_staff_reply_answers_ticket_and_user_reply_reopens_it(migrated_support_database):
    with connect() as conn:
        owner=make_user(conn)
        staff=make_user(conn)
        grant_role(conn,staff,"support")
        ticket=new_ticket(conn,owner)
        reply=staff_reply_ticket(
            conn,ticket_id=ticket["id"],actor_id=staff,message="We have reviewed your request",
        )
        assert reply["is_internal_note"] is False
        assert get_user_ticket(conn,user_id=owner,ticket_id=ticket["id"])["ticket"]["status"]=="answered"
        reply_user_ticket(conn,user_id=owner,ticket_id=ticket["id"],message="Thank you, one more detail")
        assert get_user_ticket(conn,user_id=owner,ticket_id=ticket["id"])["ticket"]["status"]=="open"
        assert len(get_user_ticket(conn,user_id=owner,ticket_id=ticket["id"])["messages"])==3


def test_closed_ticket_cannot_be_replied_to_until_staff_reopens(migrated_support_database):
    with connect() as conn:
        owner=make_user(conn)
        staff=make_user(conn)
        grant_role(conn,staff,"support")
        ticket=new_ticket(conn,owner)
        change_ticket_status(
            conn,ticket_id=ticket["id"],actor_id=staff,new_status="closed",note="Resolved",
        )
        with pytest.raises(ValueError,match="closed ticket"):
            reply_user_ticket(conn,user_id=owner,ticket_id=ticket["id"],message="Follow-up")
        with pytest.raises(ValueError,match="invalid ticket status transition"):
            change_ticket_status(
                conn,ticket_id=ticket["id"],actor_id=staff,new_status="waiting",
            )
        change_ticket_status(conn,ticket_id=ticket["id"],actor_id=staff,new_status="open")
        reply_user_ticket(conn,user_id=owner,ticket_id=ticket["id"],message="Reopened follow-up")
        assert get_user_ticket(conn,user_id=owner,ticket_id=ticket["id"])["ticket"]["status"]=="open"


def test_staff_queue_filters_status_and_assignee(migrated_support_database):
    with connect() as conn:
        owner=make_user(conn)
        staff=make_user(conn)
        grant_role(conn,staff,"support")
        first=new_ticket(conn,owner,"First request")
        second=new_ticket(conn,owner,"Second request")
        assign_ticket(conn,ticket_id=first["id"],actor_id=staff,assignee_id=staff)
        change_ticket_status(conn,ticket_id=second["id"],actor_id=staff,new_status="waiting")
        queue=list_staff_tickets(conn,status="open",assigned_to=staff)
        assert [row["id"] for row in queue]==[first["id"]]
        waiting=list_staff_tickets(conn,status="waiting")
        assert [row["id"] for row in waiting]==[second["id"]]


def test_faq_drafts_are_private_until_published_and_admin_actions_are_audited(migrated_support_database):
    with connect() as conn:
        admin=make_user(conn)
        support=make_user(conn)
        grant_role(conn,admin,"admin")
        grant_role(conn,support,"support")
        with pytest.raises(AuthorizationDenied):
            create_faq(
                conn,actor_id=support,slug="billing-help",question="How billing works?",
                answer="Billing details",published=True,
            )
        draft=create_faq(
            conn,actor_id=admin,slug="billing-help",question="How billing works?",
            answer="Billing details",published=False,sort_order=10,
        )
        assert list_faq(conn)==[]
        published=update_faq(
            conn,actor_id=admin,faq_id=draft["id"],
            changes={"published":True,"answer":"Updated billing details"},
        )
        assert published["published"] is True
        visible=list_faq(conn)
        assert len(visible)==1
        assert visible[0]["answer"]=="Updated billing details"
        assert conn.execute(
            "SELECT count(*) AS n FROM admin_audit_logs WHERE target_type='faq' AND target_id=%s",
            (str(draft["id"]),),
        ).fetchone()["n"]==2


def test_support_api_routes_are_registered():
    from saas_core.auth.api import app
    paths=app.openapi()["paths"]
    assert "/support/faqs" in paths
    assert "/support/tickets" in paths
    assert "/support/tickets/{ticket_id}" in paths
    assert "/support/tickets/{ticket_id}/messages" in paths
    assert "/support/admin/tickets" in paths
    assert "/support/admin/tickets/{ticket_id}/assign" in paths
    assert "/support/admin/tickets/{ticket_id}/notes" in paths
    assert "/support/admin/tickets/{ticket_id}/status" in paths
    assert "/support/admin/faqs" in paths
