"""Long-running notification delivery worker with bounded polling."""
from __future__ import annotations
import os,time,signal,sys
import psycopg
from notification_service.service import run_worker_once
from psycopg.conninfo import conninfo_to_dict
STOP=False
def _stop(*_): 
 global STOP
 STOP=True
def main():
 signal.signal(signal.SIGTERM,_stop); signal.signal(signal.SIGINT,_stop)
 dsn=os.getenv("NOTIFICATION_DATABASE_URL","").strip()
 if not dsn: raise RuntimeError("NOTIFICATION_DATABASE_URL required")
 if len(os.getenv("NOTIFICATION_IDENTITY_SERVICE_TOKEN","").strip())<32: raise RuntimeError("NOTIFICATION_IDENTITY_SERVICE_TOKEN required")
 p=conninfo_to_dict(dsn)
 if os.getenv("APP_ENV","development").lower() in {"prod","production"} and p.get("sslmode","prefer").lower()!="verify-full": raise RuntimeError("Notification worker requires verified TLS")
 with psycopg.connect(dsn,connect_timeout=5,autocommit=True) as conn:
  while not STOP:
   try:
    count=run_worker_once(conn,int(os.getenv("NOTIFICATION_WORKER_BATCH_SIZE","20")))
    if not count: time.sleep(max(.2,min(10,float(os.getenv("NOTIFICATION_WORKER_IDLE_SECONDS","2")))))
   except psycopg.Error:
    conn.rollback(); time.sleep(2)
 return 0
if __name__=="__main__":
 try: raise SystemExit(main())
 except Exception as e: print(f"Notification worker stopped: {type(e).__name__}",file=sys.stderr); raise SystemExit(1)
