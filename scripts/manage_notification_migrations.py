#!/usr/bin/env python3
"""Apply checksum-protected Notification PostgreSQL migrations."""
from __future__ import annotations
import argparse,hashlib,os,re,sys
from pathlib import Path
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
ROOT=Path(__file__).resolve().parents[1]; DIR=ROOT/"notification_service"/"schema"
PAT=re.compile(r"^[0-9]{3}_[a-z0-9_]+\.sql$")
def main():
 p=argparse.ArgumentParser(); g=p.add_mutually_exclusive_group(required=True); g.add_argument("--apply",action="store_true"); g.add_argument("--check",action="store_true"); p.add_argument("--grant-role")
 a=p.parse_args(); dsn=os.getenv("NOTIFICATION_MIGRATION_DATABASE_URL","").strip(); expected=os.getenv("NOTIFICATION_EXPECTED_DATABASE_NAME","").strip()
 if not dsn or not expected: raise RuntimeError("Notification migration URL and expected database name required")
 params=conninfo_to_dict(dsn); hosts=[x.strip().lower() for x in params.get("host","localhost").split(",") if x.strip()]
 if any(x not in {"localhost","127.0.0.1","::1"} for x in hosts) and params.get("sslmode","prefer").lower()!="verify-full": raise RuntimeError("Remote migration connections require sslmode=verify-full")
 migrations=[]
 for f in sorted(DIR.glob("*.sql")):
  if not PAT.fullmatch(f.name): raise RuntimeError("Invalid migration filename: "+f.name)
  raw=f.read_bytes(); migrations.append((f.stem,hashlib.sha256(raw).hexdigest(),raw.decode()))
 if not migrations: raise RuntimeError("No Notification migrations found")
 if a.grant_role and not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}",a.grant_role): raise RuntimeError("Invalid runtime role")
 with psycopg.connect(dsn,autocommit=True,row_factory=dict_row) as c:
  if c.execute("SELECT current_database() AS n").fetchone()["n"]!=expected: raise RuntimeError("Unexpected Notification database")
  c.execute("SELECT pg_advisory_lock(20261127,11)")
  try:
   exists=c.execute("SELECT to_regclass('public.notification_schema_migrations') IS NOT NULL AS ok").fetchone()["ok"]
   if not exists and a.check:
    print(f"{len(migrations)} Notification migration(s) pending"); return 1
   if not exists:
    objects=c.execute("""SELECT count(*) AS n FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','S','f')""").fetchone()["n"]
    if objects: raise RuntimeError("Notification schema contains untracked objects; review before adoption")
    c.execute("CREATE TABLE notification_schema_migrations(version varchar(120) PRIMARY KEY,sha256 char(64) NOT NULL,applied_at timestamptz NOT NULL DEFAULT now())")
   recorded={r["version"]:r["sha256"].strip() for r in c.execute("SELECT version,sha256 FROM notification_schema_migrations").fetchall()}
   expected_hash={v:h for v,h,_ in migrations}
   if set(recorded)-set(expected_hash): raise RuntimeError("Unknown Notification migration version")
   for v,h in recorded.items():
    if expected_hash[v]!=h: raise RuntimeError("Migration checksum mismatch: "+v)
   pending=[x for x in migrations if x[0] not in recorded]
   if a.check:
    if pending: raise RuntimeError("Pending migrations: "+", ".join(x[0] for x in pending))
    print(f"Notification schema current ({len(recorded)} migrations)"); return 0
   for v,h,contents in pending:
    with c.transaction():
     c.execute(contents,prepare=False); c.execute("INSERT INTO notification_schema_migrations(version,sha256) VALUES(%s,%s)",(v,h))
   if a.grant_role:
    if not c.execute("SELECT 1 FROM pg_roles WHERE rolname=%s",(a.grant_role,)).fetchone(): raise RuntimeError("Runtime role must be pre-created")
    role=sql.Identifier(a.grant_role); c.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
    for r in c.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename<>'notification_schema_migrations'").fetchall():
     c.execute(sql.SQL("GRANT SELECT,INSERT,UPDATE,DELETE ON TABLE public.{} TO {}").format(sql.Identifier(r["tablename"]),role))
    for r in c.execute("SELECT sequencename FROM pg_sequences WHERE schemaname='public'").fetchall():
     c.execute(sql.SQL("GRANT USAGE,SELECT ON SEQUENCE public.{} TO {}").format(sql.Identifier(r["sequencename"]),role))
   print(f"Applied {len(pending)} Notification migrations")
  finally: c.execute("SELECT pg_advisory_unlock(20261127,11)")
 return 0
if __name__=="__main__":
 try: raise SystemExit(main())
 except Exception as e: print(f"Notification migration failed: {e}",file=sys.stderr); raise SystemExit(1)
