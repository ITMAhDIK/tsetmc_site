from __future__ import annotations
import os
from uuid import uuid4
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

DSN=os.getenv("IDENTITY_TEST_DATABASE_URL","").strip()
EXPECTED=os.getenv("IDENTITY_TEST_DATABASE_NAME","").strip()
pytestmark=pytest.mark.skipif(not DSN or not EXPECTED,reason="requires a disposable Identity PostgreSQL database")

@pytest.fixture(scope="module",autouse=True)
def verify_identity_db():
    with psycopg.connect(DSN,row_factory=dict_row) as conn:
        name=conn.execute("SELECT current_database() AS name").fetchone()["name"]
        if name!=EXPECTED: pytest.fail("Identity tests refuse a non-disposable or unexpected database")
        missing=conn.execute("""SELECT count(*) AS n FROM unnest(%s::text[]) t(name)
                               WHERE to_regclass('public.'||t.name) IS NULL""",
                             (["users","sessions","roles","permissions","user_roles","legal_acceptances","notification_outbox"],)).fetchone()["n"]
        if missing: pytest.fail("Identity migrations are incomplete")

@pytest.fixture()
def db(verify_identity_db):
    conn=psycopg.connect(DSN,row_factory=dict_row)
    conn.execute("TRUNCATE TABLE users CASCADE")
    conn.execute("TRUNCATE TABLE phone_verifications,notification_outbox")
    conn.commit()
    try: yield conn
    finally:
        conn.rollback()
        conn.close()

@pytest.fixture()
def client(monkeypatch):
    from identity_service.api import app,get_conn
    monkeypatch.setenv("IDENTITY_SERVICE_TOKEN","identity-test-service-token-"+"x"*24)
    def override():
        with psycopg.connect(DSN,row_factory=dict_row) as conn: yield conn
    app.dependency_overrides[get_conn]=override
    try:
        with TestClient(app) as c: yield c
    finally:
        app.dependency_overrides.pop(get_conn,None)

def headers(token): return {"Authorization":f"Bearer {token}"}

def create_user(conn,username):
    from saas_core.auth.service import hash_password
    row=conn.execute("""INSERT INTO users(public_id,username,password_hash)
                        VALUES (%s,%s,%s) RETURNING id""",
                     ("usr_"+uuid4().hex[:24],username,hash_password("identity-test-password-2026"))).fetchone()
    conn.execute("INSERT INTO user_roles(user_id,role_id) SELECT %s,id FROM roles WHERE code='user'",(row["id"],))
    token="session-"+uuid4().hex
    from saas_core.auth.service import hash_session_token
    conn.execute("INSERT INTO sessions(user_id,token_hash,expires_at) VALUES (%s,%s,now()+interval '1 day')",
                 (row["id"],hash_session_token(token)))
    conn.commit()
    return row["id"],token

def test_health_and_readiness_are_separate(client):
    assert client.get("/health").json()=={"status":"ok","service":"identity"}
    assert client.get("/ready").status_code==200

def test_introspection_returns_stable_user_roles_and_permissions_but_not_secrets(client,db,monkeypatch):
    user_id,token=create_user(db,"identity_customer")
    service="identity-test-service-token-"+"x"*24
    response=client.post("/internal/introspect",headers={"X-Identity-Service-Token":service},
                         json={"token":token})
    assert response.status_code==200,response.text
    principal=response.json()["principal"]
    assert principal["user"]["id"]==str(user_id)
    assert principal["roles"]==["user"]
    assert "sessions" not in principal and "token_hash" not in principal
    assert "password_hash" not in principal["user"]
    denied=client.post("/internal/introspect",json={"token":token})
    assert denied.status_code==401

def test_expired_and_revoked_sessions_fail_closed(client,db):
    user_id,token=create_user(db,"identity_revoked")
    db.execute("UPDATE sessions SET revoked_at=now() WHERE user_id=%s",(user_id,));db.commit()
    response=client.post("/internal/introspect",headers={"X-Identity-Service-Token":"identity-test-service-token-"+"x"*24},
                         json={"token":token})
    assert response.status_code==401

def test_user_cannot_assign_roles_or_change_another_users_status_via_public_api(client):
    assert client.put("/internal/users/"+str(uuid4())+"/roles/admin",json={"grant":True}).status_code==401
    assert client.post("/internal/users/"+str(uuid4())+"/status",json={"status":"suspended","reason":"policy review"}).status_code==401

def test_login_uses_argon2_and_sets_http_only_cookie(client,db):
    from saas_core.auth.service import hash_password
    db.execute("INSERT INTO users(public_id,username,password_hash) VALUES ('usr_identitylogin000000000000','identity_login',%s)",
               (hash_password("identity-login-password-2026"),))
    db.commit()
    response=client.post("/auth/login",json={"username":"identity_login","password":"identity-login-password-2026"})
    assert response.status_code==200,response.text
    assert "tloomberg_session" in response.cookies
    bad=client.post("/auth/login",json={"username":"identity_login","password":"wrong-password"})
    assert bad.status_code==401

def test_signup_fails_closed_without_legal_versions(client,monkeypatch):
    monkeypatch.delenv("IDENTITY_TERMS_VERSION",raising=False)
    monkeypatch.delenv("IDENTITY_PRIVACY_VERSION",raising=False)
    response=client.post("/auth/register",json={"username":"new_identity_user",
        "password":"identity-signup-password-2026","terms_accepted":True,
        "terms_version":"v1","privacy_version":"v1"})
    assert response.status_code==503

def test_session_listing_and_revocation_are_scoped_to_owner(client,db):
    user_id,token=create_user(db,"identity_sessions")
    other_id,other_token=create_user(db,"identity_other")
    service="identity-test-service-token-"+"x"*24
    h={"X-Identity-Service-Token":service,"X-Identity-User-Token":token}
    rows=client.get("/internal/sessions",headers=h)
    assert rows.status_code==200
    assert len(rows.json()["sessions"])==1
    session_id=rows.json()["sessions"][0]["id"]
    assert client.delete("/internal/sessions/"+session_id,headers=h).status_code==409
    other_session=db.execute("SELECT id FROM sessions WHERE user_id=%s",(other_id,)).fetchone()["id"]
    assert client.delete("/internal/sessions/"+str(other_session),headers=h).status_code==404
    assert client.post("/internal/sessions/revoke-others",headers=h).status_code==200

