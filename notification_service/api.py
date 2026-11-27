"""Notification API: private enqueue, user inbox and preferences."""
from __future__ import annotations
import hmac,os
from contextlib import asynccontextmanager
from uuid import UUID
from fastapi import FastAPI,Depends,Header,HTTPException,Query
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel,Field
from notification_service.db import get_conn,lifespan_pool
from notification_service.identity import current_user
from notification_service.service import enqueue,inbox,_audit
class EnqueueBody(BaseModel):
 user_ids:list[UUID]=Field(min_length=1,max_length=100)
 template_key:str=Field(min_length=2,max_length=64)
 payload:dict=Field(default_factory=dict)
 channels:list[str]=Field(min_length=1,max_length=3)
 idempotency_key:str=Field(min_length=8,max_length=160)
class Preferences(BaseModel):
 email_enabled:bool=True
 sms_enabled:bool=True
 in_app_enabled:bool=True
 marketing_enabled:bool=False
@asynccontextmanager
async def lifespan(app):
 async with lifespan_pool(app): yield
app=FastAPI(title="Tloomberg Notification Service",version="1.0.0",lifespan=lifespan)
hosts=[x.strip() for x in os.getenv("NOTIFICATION_TRUSTED_HOSTS","").split(",") if x.strip()]
if hosts: app.add_middleware(TrustedHostMiddleware,allowed_hosts=hosts)
@app.get("/health")
def health(): return {"status":"ok","service":"notifications"}
@app.get("/ready")
def ready(conn=Depends(get_conn)):
 expected=os.getenv("NOTIFICATION_EXPECTED_DATABASE_NAME","").strip()
 actual=conn.execute("SELECT current_database() AS name").fetchone()["name"]
 if expected and expected!=actual: raise HTTPException(503,detail="unexpected Notification database")
 row=conn.execute("SELECT to_regclass('public.notifications') IS NOT NULL AS n,to_regclass('public.notification_deliveries') IS NOT NULL AS d").fetchone()
 if not row["n"] or not row["d"]: raise HTTPException(503,detail="Notification schema not ready")
 return {"status":"ready","service":"notifications"}
def _ingest(token):
 expected=os.getenv("NOTIFICATION_INGEST_TOKEN","").strip()
 if len(expected)<32 or not token or not hmac.compare_digest(expected,token): raise HTTPException(401,detail="service authentication required")
@app.post("/internal/notifications",include_in_schema=False,status_code=202)
def create_notification(body:EnqueueBody,x_notification_token:str|None=Header(default=None,alias="X-Notification-Token"),conn=Depends(get_conn)):
 _ingest(x_notification_token)
 return enqueue(conn,body.user_ids,body.template_key,body.payload,body.channels,body.idempotency_key)
@app.get("/notifications")
def get_inbox(limit:int=Query(30,ge=1,le=100),offset:int=Query(0,ge=0,le=10000),user=Depends(current_user),conn=Depends(get_conn)):
 rows=inbox(conn,user["id"],limit,offset)
 return {"notifications":rows,"limit":limit,"offset":offset}
@app.patch("/notifications/{notification_id}/read")
def mark_read(notification_id:UUID,user=Depends(current_user),conn=Depends(get_conn)):
 row=conn.execute("UPDATE notifications SET read_at=COALESCE(read_at,now()) WHERE id=%s AND user_id=%s RETURNING id,read_at",(notification_id,user["id"])).fetchone()
 if not row: raise HTTPException(404,detail="notification not found")
 _audit(conn,str(user["id"]),"notification.read",notification_id)
 return {"notification":row}
@app.get("/notifications/preferences")
def get_preferences(user=Depends(current_user),conn=Depends(get_conn)):
 row=conn.execute("SELECT email_enabled,sms_enabled,in_app_enabled,marketing_enabled,updated_at FROM notification_preferences WHERE user_id=%s",(user["id"],)).fetchone()
 return {"preferences":row or {"email_enabled":True,"sms_enabled":True,"in_app_enabled":True,"marketing_enabled":False}}
@app.put("/notifications/preferences")
def update_preferences(body:Preferences,user=Depends(current_user),conn=Depends(get_conn)):
 conn.execute("""INSERT INTO notification_preferences(user_id,email_enabled,sms_enabled,in_app_enabled,marketing_enabled)
 VALUES(%s,%s,%s,%s,%s) ON CONFLICT(user_id) DO UPDATE SET email_enabled=EXCLUDED.email_enabled,sms_enabled=EXCLUDED.sms_enabled,in_app_enabled=EXCLUDED.in_app_enabled,marketing_enabled=EXCLUDED.marketing_enabled,updated_at=now()""",(user["id"],body.email_enabled,body.sms_enabled,body.in_app_enabled,body.marketing_enabled))
 _audit(conn,str(user["id"]),"notification.preferences.update")
 return {"preferences":body.model_dump()}
