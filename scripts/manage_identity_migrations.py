#!/usr/bin/env python3
"""Apply checksum-verified migrations to the dedicated Identity PostgreSQL database."""
from __future__ import annotations
import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

ROOT=Path(__file__).resolve().parents[1]
MIGRATION_DIR=ROOT/"identity_service"/"schema"
NAME=re.compile(r"^[0-9]{3}_[a-z0-9_]+\.sql$")
ROLE_NAME=re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

class MigrationError(RuntimeError): pass

def _dsn():
    value=os.getenv("IDENTITY_MIGRATION_DATABASE_URL","").strip()
    if not value: raise MigrationError("IDENTITY_MIGRATION_DATABASE_URL is required")
    params=conninfo_to_dict(value)
    hosts=[v.strip().lower() for v in params.get("host","localhost").split(",") if v.strip()]
    if any(h not in {"localhost","127.0.0.1","::1"} for h in hosts) and params.get("sslmode","prefer").lower()!="verify-full":
        raise MigrationError("Remote Identity migration connections require sslmode=verify-full")
    return value

def load():
    result=[]
    for path in sorted(MIGRATION_DIR.glob("*.sql")):
        if not NAME.fullmatch(path.name): raise MigrationError(f"Unexpected migration filename: {path.name}")
        raw=path.read_bytes()
        result.append((path.stem,hashlib.sha256(raw).hexdigest(),raw.decode("utf-8")))
    if not result: raise MigrationError("No Identity migrations found")
    return result

def _grant(conn,role_name):
    if not ROLE_NAME.fullmatch(role_name): raise MigrationError("Invalid Identity runtime role name")
    role=sql.Identifier(role_name)
    if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s",(role_name,)).fetchone():
        raise MigrationError("Create the Identity runtime role out-of-band before granting privileges")
    conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
    rows=conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename<>'identity_schema_migrations'").fetchall()
    for row in rows:
        conn.execute(sql.SQL("GRANT SELECT,INSERT,UPDATE,DELETE ON TABLE public.{} TO {}").format(sql.Identifier(row["tablename"]),role))
    rows=conn.execute("SELECT sequencename FROM pg_sequences WHERE schemaname='public'").fetchall()
    for row in rows:
        conn.execute(sql.SQL("GRANT USAGE,SELECT ON SEQUENCE public.{} TO {}").format(sql.Identifier(row["sequencename"]),role))
    owner=conn.execute("SELECT current_user AS name").fetchone()["name"]
    conn.execute(sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO {}").format(sql.Identifier(owner),role))
    conn.execute(sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public GRANT USAGE,SELECT ON SEQUENCES TO {}").format(sql.Identifier(owner),role))

def run(apply:bool,grant_role:str|None):
    migrations=load()
    conn=psycopg.connect(_dsn(),autocommit=True,row_factory=dict_row,connect_timeout=5)
    try:
        actual=conn.execute("SELECT current_database() AS name").fetchone()["name"]
        expected=os.getenv("IDENTITY_EXPECTED_DATABASE_NAME","").strip()
        if not expected or actual!=expected: raise MigrationError("Connected database does not match IDENTITY_EXPECTED_DATABASE_NAME")
        conn.execute("SELECT pg_advisory_lock(20261127,1)")
        exists=conn.execute("SELECT to_regclass('public.identity_schema_migrations') IS NOT NULL AS ok").fetchone()["ok"]
        if apply and not exists:
            objects=conn.execute("""SELECT count(*) AS n FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                                    WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','S','f')""").fetchone()["n"]
            if objects: raise MigrationError("Identity schema contains untracked objects; review before adoption")
            conn.execute("""CREATE TABLE public.identity_schema_migrations(
                version VARCHAR(120) PRIMARY KEY,sha256 CHAR(64) NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),applied_by TEXT NOT NULL DEFAULT current_user)""")
        recorded={}
        if exists:
            recorded={r["version"]:r["sha256"].strip() for r in conn.execute("SELECT version,sha256 FROM identity_schema_migrations").fetchall()}
        expected_hash={v:d for v,d,_ in migrations}
        unknown=set(recorded)-set(expected_hash)
        if unknown: raise MigrationError("Unknown Identity migration versions: "+", ".join(sorted(unknown)))
        for version,digest in recorded.items():
            if expected_hash[version]!=digest: raise MigrationError(f"Migration {version} checksum changed")
        pending=[x for x in migrations if x[0] not in recorded]
        if not apply:
            print(f"{len(pending)} Identity migration(s) pending" if pending else f"Identity schema current ({len(recorded)} migrations)")
            return 1 if pending else 0
        for version,digest,contents in pending:
            with conn.transaction():
                conn.execute(contents,prepare=False)
                conn.execute("INSERT INTO identity_schema_migrations(version,sha256) VALUES (%s,%s)",(version,digest))
            print("APPLIED",version)
        if grant_role: _grant(conn,grant_role)
        print(f"Identity schema current ({len(migrations)} migrations)")
        return 0
    finally:
        try: conn.execute("SELECT pg_advisory_unlock(20261127,1)")
        except psycopg.Error: pass
        conn.close()

def main():
    p=argparse.ArgumentParser(description=__doc__)
    g=p.add_mutually_exclusive_group(required=True)
    g.add_argument("--check",action="store_true")
    g.add_argument("--apply",action="store_true")
    p.add_argument("--grant-role")
    args=p.parse_args()
    try:return run(args.apply,args.grant_role)
    except (MigrationError,psycopg.Error) as exc:
        print(f"Identity migration error: {exc}",file=sys.stderr);return 2
if __name__=="__main__":raise SystemExit(main())
