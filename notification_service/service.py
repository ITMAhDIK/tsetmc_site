"""Idempotent notification enqueue, inbox, and leased delivery queue."""
from __future__ import annotations
import hashlib,json,os,re,uuid
from datetime import datetime,timezone,timedelta
from uuid import UUID
from cryptography.fernet import Fernet
from fastapi import HTTPException
from notification_service.identity import verified_contact
from notification_service.providers import send_email,send_sms,DeliveryError
TEMPLATES={
 "welcome":("Welcome to Tloomberg","Your account is ready.","Your account is ready.", "transactional"),
 "security_alert":("Security alert","A security event was detected on your account.","A security event was detected on your account.", "security"),
 "payment_receipt":("Payment received","Your payment was received successfully.","Your payment was received successfully.", "transactional"),
 "subscription_expiry":("Subscription reminder","Your subscription is approaching its expiry date.","Your subscription is approaching its expiry date.", "transactional"),
 "product_update":("Product update","There is an update in your Tloomberg account.","There is an update in your Tloomberg account.", "product"),
}
def _hash(value): return hashlib.sha256(value.encode()).hexdigest()
def _audit(conn,actor,action,nid=None,meta=None):
 conn.execute("INSERT INTO notification_audit_log(actor,action,notification_id,metadata) VALUES(%s,%s,%s,%s::jsonb)",(actor,action,nid,json.dumps(meta or {})))
def enqueue(conn,user_ids,template_key,payload,channels,idempotency_key,actor="service"):
 if template_key not in TEMPLATES: raise HTTPException(422,detail="unknown notification template")
 if not isinstance(payload,dict) or len(json.dumps(payload))>4096: raise HTTPException(422,detail="invalid notification payload")
 if not re.fullmatch(r"[A-Za-z0-9._:-]{8,160}",idempotency_key or ""): raise HTTPException(422,detail="invalid idempotency key")
 if not channels or not set(channels)<= {"email","sms","in_app"}: raise HTTPException(422,detail="invalid channels")
 canonical=json.dumps({"users":[str(x) for x in user_ids],"template":template_key,"payload":payload,"channels":sorted(set(channels))},sort_keys=True,separators=(",",":"))
 digest=_hash(canonical)
 with conn.transaction():
  existing=conn.execute("SELECT request_hash,notification_ids FROM notification_idempotency WHERE idempotency_key=%s FOR UPDATE",(idempotency_key,)).fetchone()
  if existing:
   if existing["request_hash"]!=digest: raise HTTPException(409,detail="idempotency key reused with different request")
   return {"notification_ids":existing["notification_ids"],"duplicate":True}
  ids=[]
  title,body,_,category=TEMPLATES[template_key]
  for uid in dict.fromkeys(user_ids):
   pref=conn.execute("SELECT * FROM notification_preferences WHERE user_id=%s",(uid,)).fetchone() or {"email_enabled":True,"sms_enabled":True,"in_app_enabled":True,"marketing_enabled":False}
   allowed=[c for c in set(channels) if (c=="email" and pref["email_enabled"]) or (c=="sms" and pref["sms_enabled"]) or (c=="in_app" and pref["in_app_enabled"])]
   if category=="marketing" and not pref["marketing_enabled"]: allowed=[]
   row=conn.execute("INSERT INTO notifications(user_id,template_key,payload,category,in_app_visible) VALUES(%s,%s,%s::jsonb,%s,%s) RETURNING id",(uid,template_key,json.dumps(payload),category,"in_app" in allowed)).fetchone()
   nid=row["id"]; ids.append(nid)
   for channel in allowed:
    conn.execute("INSERT INTO notification_deliveries(notification_id,channel) VALUES(%s,%s)",(nid,channel))
   _audit(conn,actor,"notification.enqueue",nid,{"channels":allowed})
  conn.execute("INSERT INTO notification_idempotency(idempotency_key,request_hash,notification_ids) VALUES(%s,%s,%s)",(idempotency_key,digest,ids))
 return {"notification_ids":ids,"duplicate":False}
def inbox(conn,user_id,limit,offset):
 rows=conn.execute("""SELECT id,template_key,payload,category,created_at,read_at FROM notifications WHERE user_id=%s AND in_app_visible=TRUE
 ORDER BY created_at DESC,id LIMIT %s OFFSET %s""",(user_id,limit,offset)).fetchall()
 return rows
def claim(conn,limit=20):
 token=uuid.uuid4()
 with conn.transaction():
  rows=conn.execute("""WITH due AS (
   SELECT id FROM notification_deliveries WHERE (status IN ('pending','retry') AND next_attempt_at<=now())
    OR (status='processing' AND locked_at<now()-interval '5 minutes')
   ORDER BY next_attempt_at,created_at FOR UPDATE SKIP LOCKED LIMIT %s)
   UPDATE notification_deliveries d SET status='processing',locked_at=now(),lock_token=%s
   FROM due WHERE d.id=due.id RETURNING d.id,d.notification_id,d.channel,d.attempts,d.max_attempts,d.lock_token""",(limit,token)).fetchall()
 return rows
def _render(template,payload):
 title,body,html,_=TEMPLATES[template]
 safe={k:str(v)[:160] for k,v in payload.items() if k in {"name","reference","amount","date"} and isinstance(v,(str,int,float))}
 for key,value in safe.items():
  title=title.replace("{"+key+"}",value); body=body.replace("{"+key+"}",value); html=html.replace("{"+key+"}",value)
 return title,body,html
def deliver_one(conn,job):
 row=conn.execute("""SELECT d.*,n.user_id,n.template_key,n.payload FROM notification_deliveries d JOIN notifications n ON n.id=d.notification_id WHERE d.id=%s AND d.status='processing' AND d.lock_token=%s""",(job["id"],job["lock_token"])).fetchone()
 if not row: return
 try:
  if row["channel"]=="in_app":
   conn.execute("UPDATE notification_deliveries SET status='sent',sent_at=now(),attempts=attempts+1,locked_at=NULL,lock_token=NULL WHERE id=%s AND lock_token=%s",(row["id"],row["lock_token"])); return
  contact=verified_contact(row["user_id"]); title,body,html=_render(row["template_key"],row["payload"])
  if row["channel"]=="email":
   dest=contact.get("email") if contact.get("email_verified") else None
   if not dest: raise DeliveryError("verified_email_unavailable",False)
   msg=send_email(dest,title,body,html); dh=_hash(dest)
  else:
   dest=contact.get("phone_e164") if contact.get("phone_verified") else None
   if not dest: raise DeliveryError("verified_phone_unavailable",False)
   msg=send_sms(dest,body); dh=_hash(dest)
  conn.execute("UPDATE notification_deliveries SET status='sent',sent_at=now(),attempts=attempts+1,locked_at=NULL,lock_token=NULL,destination_hash=%s,provider_message_id=%s,last_error_code=NULL WHERE id=%s AND lock_token=%s",(dh,msg,row["id"],row["lock_token"]))
 except DeliveryError as e:
  attempts=row["attempts"]+1; status="retry" if e.retryable and attempts<row["max_attempts"] else "dead"
  delay=min(3600,30*(2**min(attempts-1,7)))
  conn.execute("UPDATE notification_deliveries SET status=%s,attempts=%s,next_attempt_at=now()+(%s*interval '1 second'),locked_at=NULL,lock_token=NULL,last_error_code=%s WHERE id=%s AND lock_token=%s",(status,attempts,delay,e.code[:80],row["id"],row["lock_token"]))
 except Exception:
  attempts=row["attempts"]+1; status="retry" if attempts<row["max_attempts"] else "dead"
  conn.execute("UPDATE notification_deliveries SET status=%s,attempts=%s,next_attempt_at=now()+(LEAST(3600,30*power(2,LEAST(%s,7)))*interval '1 second'),locked_at=NULL,lock_token=NULL,last_error_code='provider_internal_error' WHERE id=%s AND lock_token=%s",(status,attempts,attempts-1,row["id"],row["lock_token"]))
def run_worker_once(conn,batch_size=20):
 jobs=claim(conn,batch_size)
 for job in jobs: deliver_one(conn,job)
 return len(jobs)
