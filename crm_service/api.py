"""Independent CRM API: customer projections, interactions and follow-ups."""
from __future__ import annotations
import hmac,os
from contextlib import asynccontextmanager
from datetime import datetime,timedelta,timezone
from typing import Literal
from uuid import UUID
from fastapi import FastAPI,Depends,HTTPException,Header,Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel,Field
from crm_service.db import get_conn,lifespan_pool
from crm_service.identity import current_user

APP_ENV=os.getenv("APP_ENV","development").lower()
def _mfa(principal):
    verified=principal.get("mfa_verified_at")
    if not principal.get("mfa_enabled") or not verified: raise HTTPException(403,detail="recent MFA verification required")
    try:
        stamp=datetime.fromisoformat(verified.replace("Z","+00:00")) if isinstance(verified,str) else verified
        if stamp.tzinfo is None or datetime.now(timezone.utc)-stamp.astimezone(timezone.utc)>timedelta(minutes=15) or stamp>datetime.now(timezone.utc)+timedelta(seconds=120): raise ValueError()
    except (TypeError,ValueError): raise HTTPException(403,detail="recent MFA verification required") from None
def _permission(principal,code):
    if code not in principal.get("permissions",[]): raise HTTPException(403,detail="CRM permission required")
    _mfa(principal)
def _audit(conn,actor,action,kind,target,metadata=None):
    conn.execute("INSERT INTO crm_audit_log(actor_user_id,action,target_type,target_id,metadata) VALUES(%s,%s,%s,%s,%s)",(actor,action,kind,str(target),metadata or {}))
class Projection(BaseModel):
    user_id:UUID; username:str=Field(min_length=1,max_length=80); display_name:str=Field(default="",max_length=160)
    account_status:Literal["active","suspended","pending","deleted"]; phone_verified:bool=False; email_verified:bool=False; created_at:datetime
class Interaction(BaseModel):
    channel:Literal["call","email","meeting","note","support","other"]; subject:str=Field(min_length=3,max_length=180); summary:str=Field(min_length=2,max_length=4000); happened_at:datetime|None=None
class Followup(BaseModel):
    customer_id:UUID; assigned_to:UUID; title:str=Field(min_length=3,max_length=180); details:str=Field(default="",max_length=2000); due_at:datetime
class FollowupStatus(BaseModel):
    status:Literal["open","completed","cancelled"]
@asynccontextmanager
async def lifespan(app):
    async with lifespan_pool(app): yield
app=FastAPI(title="Tloomberg CRM Service",version="1.0.0",lifespan=lifespan)
hosts=[x.strip() for x in os.getenv("CRM_TRUSTED_HOSTS","").split(",") if x.strip()]
origins=[x.strip() for x in os.getenv("CRM_ALLOWED_ORIGINS","").split(",") if x.strip()]
if hosts: app.add_middleware(TrustedHostMiddleware,allowed_hosts=hosts)
if origins: app.add_middleware(CORSMiddleware,allow_origins=origins,allow_credentials=True,allow_methods=["GET","POST","PATCH","PUT"],allow_headers=["Authorization","Content-Type","X-CRM-Projection-Token"],max_age=600)
@app.get("/health")
def health(): return {"status":"ok","service":"crm"}
@app.get("/ready")
def ready(conn=Depends(get_conn)):
    expected=os.getenv("CRM_EXPECTED_DATABASE_NAME","").strip()
    actual=conn.execute("SELECT current_database() AS name").fetchone()["name"]
    if expected and actual!=expected: raise HTTPException(503,detail="unexpected CRM database")
    row=conn.execute("SELECT to_regclass('public.crm_customers') IS NOT NULL AS customers,to_regclass('public.crm_interactions') IS NOT NULL AS interactions,to_regclass('public.crm_followups') IS NOT NULL AS followups").fetchone()
    if not all(row.values()): raise HTTPException(503,detail="CRM schema is not ready")
    return {"status":"ready","service":"crm"}
def _projection_auth(token:str|None):
    expected=os.getenv("CRM_PROJECTION_TOKEN","").strip()
    if len(expected)<32 or not token or not hmac.compare_digest(expected,token): raise HTTPException(401,detail="projection authentication required")
@app.put("/internal/customers/{user_id}",include_in_schema=False)
def upsert_customer_projection(user_id:UUID,body:Projection,x_crm_projection_token:str|None=Header(default=None,alias="X-CRM-Projection-Token"),conn=Depends(get_conn)):
    _projection_auth(x_crm_projection_token)
    if user_id!=body.user_id: raise HTTPException(400,detail="customer ID mismatch")
    conn.execute("""INSERT INTO crm_customers(user_id,username,display_name,account_status,phone_verified,email_verified,created_at)
      VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(user_id) DO UPDATE SET username=EXCLUDED.username,display_name=EXCLUDED.display_name,account_status=EXCLUDED.account_status,phone_verified=EXCLUDED.phone_verified,email_verified=EXCLUDED.email_verified""",
      (body.user_id,body.username,body.display_name,body.account_status,body.phone_verified,body.email_verified,body.created_at))
    return {"status":"projected"}
@app.get("/customers")
def customers(q:str|None=Query(default=None,max_length=80),lifecycle_stage:str|None=None,assigned_to:UUID|None=None,limit:int=Query(50,ge=1,le=100),offset:int=Query(0,ge=0,le=10000),principal=Depends(current_user),conn=Depends(get_conn)):
    _permission(principal,"crm.read")
    if lifecycle_stage and lifecycle_stage not in {"new","active","at_risk","churned"}: raise HTTPException(422,detail="invalid lifecycle stage")
    where=[]; args=[]
    if q is not None:
        q=q.strip()
        if not q: raise HTTPException(422,detail="query must contain text")
        where.append("(username ILIKE %s OR display_name ILIKE %s)"); args += [f"%{q}%",f"%{q}%"]
    if lifecycle_stage: where.append("lifecycle_stage=%s"); args.append(lifecycle_stage)
    if assigned_to: where.append("assigned_to=%s"); args.append(assigned_to)
    clause=" WHERE "+" AND ".join(where) if where else ""
    rows=conn.execute("SELECT user_id,username,display_name,account_status,phone_verified,email_verified,lifecycle_stage,assigned_to,created_at FROM crm_customers"+clause+" ORDER BY created_at DESC,user_id LIMIT %s OFFSET %s",(*args,limit,offset)).fetchall()
    total=conn.execute("SELECT count(*)::bigint AS total FROM crm_customers"+clause,args).fetchone()["total"]
    _audit(conn,principal["id"],"crm.customers.list","customer_list","all",{"total":total})
    return {"customers":rows,"total":total,"limit":limit,"offset":offset}
@app.get("/customers/{customer_id}")
def customer(customer_id:UUID,principal=Depends(current_user),conn=Depends(get_conn)):
    _permission(principal,"crm.read")
    row=conn.execute("SELECT * FROM crm_customers WHERE user_id=%s",(customer_id,)).fetchone()
    if not row: raise HTTPException(404,detail="customer not found")
    _audit(conn,principal["id"],"crm.customer.read","customer",customer_id)
    return {"customer":row}
@app.patch("/customers/{customer_id}/profile")
def profile(customer_id:UUID,lifecycle_stage:Literal["new","active","at_risk","churned"],assigned_to:UUID|None=None,principal=Depends(current_user),conn=Depends(get_conn)):
    _permission(principal,"crm.manage")
    cur=conn.execute("UPDATE crm_customers SET lifecycle_stage=%s,assigned_to=%s WHERE user_id=%s RETURNING user_id,lifecycle_stage,assigned_to",(lifecycle_stage,assigned_to,customer_id))
    row=cur.fetchone()
    if not row: raise HTTPException(404,detail="customer not found")
    _audit(conn,principal["id"],"crm.customer.profile.update","customer",customer_id,{"lifecycle_stage":lifecycle_stage,"assigned_to":str(assigned_to) if assigned_to else None})
    return {"profile":row}
@app.get("/customers/{customer_id}/interactions")
def interactions(customer_id:UUID,limit:int=Query(50,ge=1,le=100),offset:int=Query(0,ge=0,le=10000),principal=Depends(current_user),conn=Depends(get_conn)):
    _permission(principal,"crm.read")
    if not conn.execute("SELECT 1 FROM crm_customers WHERE user_id=%s",(customer_id,)).fetchone(): raise HTTPException(404,detail="customer not found")
    rows=conn.execute("SELECT id,customer_id,actor_user_id,channel,subject,summary,happened_at,created_at FROM crm_interactions WHERE customer_id=%s ORDER BY happened_at DESC,created_at DESC LIMIT %s OFFSET %s",(customer_id,limit,offset)).fetchall()
    return {"interactions":rows,"limit":limit,"offset":offset}
@app.post("/customers/{customer_id}/interactions",status_code=201)
def add_interaction(customer_id:UUID,body:Interaction,principal=Depends(current_user),conn=Depends(get_conn)):
    _permission(principal,"crm.manage")
    if not conn.execute("SELECT 1 FROM crm_customers WHERE user_id=%s",(customer_id,)).fetchone(): raise HTTPException(404,detail="customer not found")
    happened=body.happened_at or datetime.now(timezone.utc)
    if happened.tzinfo is None: raise HTTPException(422,detail="happened_at must include timezone")
    row=conn.execute("INSERT INTO crm_interactions(customer_id,actor_user_id,channel,subject,summary,happened_at) VALUES(%s,%s,%s,%s,%s,%s) RETURNING *",(customer_id,principal["id"],body.channel,body.subject.strip(),body.summary.strip(),happened)).fetchone()
    _audit(conn,principal["id"],"crm.interaction.create","interaction",row["id"])
    return {"interaction":row}
@app.get("/followups")
def followups(status:Literal["open","completed","cancelled"]|None="open",assigned_to:UUID|None=None,customer_id:UUID|None=None,limit:int=Query(50,ge=1,le=100),offset:int=Query(0,ge=0,le=10000),principal=Depends(current_user),conn=Depends(get_conn)):
    _permission(principal,"crm.read")
    clauses=[]; args=[]
    if status: clauses.append("status=%s"); args.append(status)
    if assigned_to: clauses.append("assigned_to=%s"); args.append(assigned_to)
    if customer_id: clauses.append("customer_id=%s"); args.append(customer_id)
    clause=" WHERE "+" AND ".join(clauses) if clauses else ""
    rows=conn.execute("SELECT * FROM crm_followups"+clause+" ORDER BY due_at,created_at DESC LIMIT %s OFFSET %s",(*args,limit,offset)).fetchall()
    return {"followups":rows}
@app.post("/followups",status_code=201)
def create_followup(body:Followup,principal=Depends(current_user),conn=Depends(get_conn)):
    _permission(principal,"crm.manage")
    if body.due_at.tzinfo is None: raise HTTPException(422,detail="due_at must include timezone")
    if not conn.execute("SELECT 1 FROM crm_customers WHERE user_id=%s",(body.customer_id,)).fetchone(): raise HTTPException(404,detail="customer not found")
    row=conn.execute("INSERT INTO crm_followups(customer_id,assigned_to,created_by,title,details,due_at) VALUES(%s,%s,%s,%s,%s,%s) RETURNING *",(body.customer_id,body.assigned_to,principal["id"],body.title.strip(),body.details.strip(),body.due_at)).fetchone()
    _audit(conn,principal["id"],"crm.followup.create","followup",row["id"])
    return {"followup":row}
@app.patch("/followups/{followup_id}/status")
def update_followup(followup_id:UUID,body:FollowupStatus,principal=Depends(current_user),conn=Depends(get_conn)):
    _permission(principal,"crm.manage")
    row=conn.execute("""UPDATE crm_followups SET status=%s,completed_at=CASE WHEN %s='completed' THEN now() ELSE NULL END,updated_at=now() WHERE id=%s RETURNING *""",(body.status,body.status,followup_id)).fetchone()
    if not row: raise HTTPException(404,detail="follow-up not found")
    _audit(conn,principal["id"],"crm.followup.status","followup",followup_id,{"status":body.status})
    return {"followup":row}
