"""PostgreSQL integration tests for Phase 6 support and FAQ."""
import os
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
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
                     "004_support.sql"):
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
        staff_view=get_staff_ticket(conn,actor_id=staff,ticket_id=ticket["id"])
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
        with pytest.raises(AuthorizationDenied):
            list_staff_tickets(conn,actor_id=ordinary_user)
        with pytest.raises(AuthorizationDenied):
            get_staff_ticket(conn,actor_id=ordinary_user,ticket_id=ticket["id"])
        assert list_staff_tickets(conn,actor_id=admin)
        assert get_staff_ticket(conn,actor_id=agent,ticket_id=ticket["id"])["ticket"]["id"]==ticket["id"]


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
        queue=list_staff_tickets(conn,actor_id=staff,status="open",assigned_to=staff)
        assert [row["id"] for row in queue]==[first["id"]]
        waiting=list_staff_tickets(conn,actor_id=staff,status="waiting")
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


def test_support_api_uses_phase_two_sessions_and_phase_two_rbac(migrated_support_database):
    from saas_core.auth.api import app, get_conn
    from saas_core.auth.repository import create_session, revoke_session

    with connect() as conn:
        owner=make_user(conn)
        other=make_user(conn)
        staff=make_user(conn)
        grant_role(conn,staff,"support")
        ticket=new_ticket(conn,owner)
        owner_token,_=create_session(conn,owner)
        other_token,_=create_session(conn,other)
        staff_token,_=create_session(conn,staff)
        conn.commit()

        def database_connection():
            yield conn

        app.dependency_overrides[get_conn]=database_connection
        try:
            client=TestClient(app)
            owner_headers={"Authorization":f"Bearer {owner_token}"}
            assert client.get(f"/support/tickets/{ticket['id']}",headers=owner_headers).status_code==200
            assert client.get(f"/support/tickets/{ticket['id']}",headers={"Authorization":f"Bearer {other_token}"}).status_code==404
            assert client.get("/support/admin/tickets",headers={"Authorization":f"Bearer {other_token}"}).status_code==403
            assert client.get("/support/admin/tickets",headers={"Authorization":f"Bearer {staff_token}"}).status_code==200
            invalid_note=client.post(f"/support/admin/tickets/{ticket['id']}/notes",
                headers={"Authorization":f"Bearer {staff_token}"},json={"message":"   "})
            assert invalid_note.status_code==422
            revoke_session(conn,staff_token)
            conn.commit()
            assert client.get("/support/admin/tickets",headers={"Authorization":f"Bearer {staff_token}"}).status_code==401
        finally:
            app.dependency_overrides.pop(get_conn,None)



def test_support_complete_api_lifecycle_uses_phase_two_sessions_and_keeps_internal_notes_private(migrated_support_database):
    from saas_core.auth.api import app,get_conn
    from saas_core.auth.repository import create_session,revoke_session

    with connect() as conn:
        owner=make_user(conn)
        other=make_user(conn)
        staff=make_user(conn)
        faq_admin=make_user(conn)
        grant_role(conn,staff,"support")
        grant_role(conn,faq_admin,"admin")
        owner_token,_=create_session(conn,owner)
        other_token,_=create_session(conn,other)
        staff_token,_=create_session(conn,staff)
        faq_admin_token,_=create_session(conn,faq_admin)
        conn.commit()

        def database_connection():
            yield conn

        app.dependency_overrides[get_conn]=database_connection
        try:
            with TestClient(app) as client:
                owner_headers={"Authorization":f"Bearer {owner_token}"}
                other_headers={"Authorization":f"Bearer {other_token}"}
                staff_headers={"Authorization":f"Bearer {staff_token}"}
                admin_headers={"Authorization":f"Bearer {faq_admin_token}"}

                created=client.post("/support/tickets",headers=owner_headers,json={
                    "subject":"Subscription access issue","category":"billing","message":"My access has not updated",
                })
                assert created.status_code==201
                ticket_id=created.json()["ticket"]["id"]

                assert client.get("/support/tickets",headers=owner_headers).status_code==200
                assert client.get(f"/support/tickets/{ticket_id}",headers=other_headers).status_code==404
                assert client.get("/support/admin/tickets",headers=other_headers).status_code==403

                queue=client.get("/support/admin/tickets?status=open",headers=staff_headers)
                assert queue.status_code==200
                assigned=client.post(f"/support/admin/tickets/{ticket_id}/assign",
                    headers=staff_headers,json={"assignee_user_id":str(staff)})
                assert assigned.status_code==200

                note=client.post(f"/support/admin/tickets/{ticket_id}/notes",
                    headers=staff_headers,json={"message":"Check payment event reconciliation"})
                assert note.status_code==201
                reply=client.post(f"/support/admin/tickets/{ticket_id}/messages",
                    headers=staff_headers,json={"message":"We are reviewing the issue"})
                assert reply.status_code==201

                owner_view=client.get(f"/support/tickets/{ticket_id}",headers=owner_headers)
                assert owner_view.status_code==200
                assert all(not item["is_internal_note"] for item in owner_view.json()["messages"])
                assert any(item["body"]=="We are reviewing the issue" for item in owner_view.json()["messages"])
                assert not any(item["body"]=="Check payment event reconciliation" for item in owner_view.json()["messages"])

                user_reply=client.post(f"/support/tickets/{ticket_id}/messages",
                    headers=owner_headers,json={"message":"Thank you; please confirm when complete"})
                assert user_reply.status_code==201
                assert client.get(f"/support/tickets/{ticket_id}",headers=owner_headers).json()["ticket"]["status"]=="open"

                closed=client.patch(f"/support/admin/tickets/{ticket_id}/status",
                    headers=staff_headers,json={"status":"closed","note":"Resolved after review"})
                assert closed.status_code==200
                assert client.post(f"/support/tickets/{ticket_id}/messages",
                    headers=owner_headers,json={"message":"One more question"}).status_code==409

                faq=client.post("/support/admin/faqs",headers=admin_headers,json={
                    "slug":"subscription-access","question":"When does access activate?",
                    "answer":"After payment confirmation.","published":True,
                })
                assert faq.status_code==201
                assert client.post("/support/admin/faqs",headers=staff_headers,json={
                    "slug":"support-cannot-publish","question":"Not allowed?","answer":"No.","published":True,
                }).status_code==403
                public_faq=client.get("/support/faqs")
                assert public_faq.status_code==200
                assert any(item["slug"]=="subscription-access" for item in public_faq.json()["faqs"])

                assert conn.execute(
                    "SELECT count(*) AS n FROM admin_audit_logs WHERE target_type='ticket' AND target_id=%s",
                    (ticket_id,),
                ).fetchone()["n"]>=4
                revoke_session(conn,staff_token)
                conn.commit()
                assert client.get("/support/admin/tickets",headers=staff_headers).status_code==401
        finally:
            app.dependency_overrides.pop(get_conn,None)
