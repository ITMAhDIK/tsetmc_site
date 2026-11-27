#!/usr/bin/env python3
"""Apply checksum-protected CRM PostgreSQL migrations."""
from __future__ import annotations
import argparse,hashlib,os,re,sys
from pathlib import Path
import psycopg
from psycopg.rows import dict_row
from psycopg.conninfo import conninfo_to_dict
ROOT=Path(__file__).resolve().parents[1]; DIR=ROOT/"crm_service"/"schema"
PAT=re.compile(r"^(?P<version>[0-9]{3}_[a-z0-9_]+)\.sql$")
def main():
    p=argparse.ArgumentParser(); g=p.add_mutually_exclusive_group(required=True); g.add_argument("--apply",action="store_true"); g.add_argument("--check",action="store_true")
    p.add_argument("--grant-role"); a=p.parse_args()
    dsn=os.getenv("CRM_MIGRATION_DATABASE_URL","").strip(); expected=os.getenv("CRM_EXPECTED_DATABASE_NAME","").strip()
    if not dsn or not expected: raise RuntimeError("CRM_MIGRATION_DATABASE_URL and CRM_EXPECTED_DATABASE_NAME are required")
    params=conninfo_to_dict(dsn)
    hosts=[x.strip().lower() for x in params.get("host","localhost").split(",") if x.strip()]
    if any(x not in {"localhost","127.0.0.1","::1"} for x in hosts) and params.get("sslmode","prefer").lower()!="verify-full": raise RuntimeError("Remote CRM migration connections require sslmode=verify-full")
    migrations=[]
    for f in sorted(DIR.glob("*.sql")):
        m=PAT.fullmatch(f.name)
        if not m: raise RuntimeError(f"Unexpected migration name: {f.name}")
        raw=f.read_bytes(); migrations.append((m.group("version"),hashlib.sha256(raw).hexdigest(),raw.decode("utf-8")))
    if not migrations: raise RuntimeError("No CRM migrations found")
    with psycopg.connect(dsn,autocommit=True,row_factory=dict_row) as c:
        if c.execute("SELECT current_database() AS n").fetchone()["n"]!=expected: raise RuntimeError("Unexpected CRM database; refusing migration")
        c.execute("SELECT pg_advisory_lock(20261127,10)")
        try:
            c.execute("""CREATE TABLE IF NOT EXISTS crm_schema_migrations(version varchar(120) PRIMARY KEY,sha256 char(64) NOT NULL,applied_at timestamptz NOT NULL DEFAULT now())""")
            recorded={r["version"]:r["sha256"].strip() for r in c.execute("SELECT version,sha256 FROM crm_schema_migrations").fetchall()}
            expected_hash={v:h for v,h,_ in migrations}
            if set(recorded)-set(expected_hash): raise RuntimeError("Unknown CRM migration versions recorded")
            for v,h in recorded.items():
                if expected_hash[v]!=h: raise RuntimeError(f"Migration {v} checksum mismatch")
            pending=[x for x in migrations if x[0] not in recorded]
            if a.check:
                if pending: raise RuntimeError("CRM migrations are pending: "+", ".join(x[0] for x in pending))
                print(f"CRM schema current ({len(recorded)} migrations)"); return 0
            if a.grant_role and not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}",a.grant_role): raise RuntimeError("Invalid runtime role name")
            for v,h,sql in pending:
                with c.transaction():
                    c.execute(sql); c.execute("INSERT INTO crm_schema_migrations(version,sha256) VALUES(%s,%s)",(v,h))
            if a.grant_role:
                if not c.execute("SELECT 1 FROM pg_roles WHERE rolname=%s",(a.grant_role,)).fetchone(): raise RuntimeError("CRM runtime role must be pre-created")
                from psycopg import sql as pgsql
                role=pgsql.Identifier(a.grant_role)
                c.execute(pgsql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
                for r in c.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename <> 'crm_schema_migrations'").fetchall():
                    c.execute(pgsql.SQL("GRANT SELECT,INSERT,UPDATE,DELETE ON TABLE public.{} TO {}").format(pgsql.Identifier(r["tablename"]),role))
                c.execute(pgsql.SQL("GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(role))
            print(f"Applied {len(pending)} CRM migrations")
        finally: c.execute("SELECT pg_advisory_unlock(20261127,10)")
    return 0
if __name__=="__main__":
    try: raise SystemExit(main())
    except Exception as exc: print(f"CRM migration failed: {exc}",file=sys.stderr); raise SystemExit(1)
