#!/usr/bin/env python3
"""Checksum-free, explicit-name guarded legacy SaaS CRM -> independent CRM import.

The source connection is opened READ ONLY and is never modified. Stable user UUIDs,
interaction UUIDs and follow-up UUIDs are preserved. Run first with --dry-run.
"""
from __future__ import annotations
import argparse,os,sys
from urllib.parse import urlsplit
import psycopg
from psycopg.rows import dict_row

def connect(name_env,url_env):
    dsn=os.getenv(url_env,"").strip(); expected=os.getenv(name_env,"").strip()
    if not dsn or not expected: raise RuntimeError(f"{url_env} and {name_env} are required")
    conn=psycopg.connect(dsn,row_factory=dict_row,connect_timeout=5)
    actual=conn.execute("SELECT current_database() AS name").fetchone()["name"]
    if actual!=expected: conn.close(); raise RuntimeError(f"Refusing connection: {actual!r} != expected database")
    return conn

def main():
    p=argparse.ArgumentParser()
    mode=p.add_mutually_exclusive_group(required=True); mode.add_argument("--dry-run",action="store_true"); mode.add_argument("--apply",action="store_true")
    a=p.parse_args()
    source=connect("CRM_IMPORT_SOURCE_DATABASE_NAME","CRM_IMPORT_SOURCE_DATABASE_URL")
    target=connect("CRM_MIGRATION_DATABASE_NAME","CRM_MIGRATION_DATABASE_URL")
    try:
        with source.transaction():
            source.execute("SET TRANSACTION READ ONLY")
            users=source.execute("""SELECT id,username,COALESCE(display_name,'') AS display_name,status,
                phone_verified_at IS NOT NULL AS phone_verified,email_verified_at IS NOT NULL AS email_verified,created_at
                FROM users ORDER BY id""").fetchall()
            profiles=source.execute("SELECT user_id,lifecycle_stage,assigned_to FROM crm_customer_profiles").fetchall()
            interactions=source.execute("""SELECT id,user_id,actor_user_id,channel,subject,summary,happened_at,created_at
                FROM crm_interactions ORDER BY created_at,id""").fetchall()
            followups=source.execute("""SELECT id,user_id,assigned_to,created_by,title,details,due_at,status,completed_at,created_at,updated_at
                FROM crm_followups ORDER BY created_at,id""").fetchall()
        print(f"Source read-only snapshot: {len(users)} users, {len(profiles)} profiles, {len(interactions)} interactions, {len(followups)} follow-ups")
        if a.dry_run: return 0
        stage={r["user_id"]:r for r in profiles}
        with target.transaction():
            for u in users:
                p=stage.get(u["id"],{})
                target.execute("""INSERT INTO crm_customers(user_id,username,display_name,account_status,phone_verified,email_verified,lifecycle_stage,assigned_to,created_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(user_id) DO UPDATE SET
                    username=EXCLUDED.username,display_name=EXCLUDED.display_name,account_status=EXCLUDED.account_status,
                    phone_verified=EXCLUDED.phone_verified,email_verified=EXCLUDED.email_verified""",
                    (u["id"],u["username"],u["display_name"],u["status"],u["phone_verified"],u["email_verified"],p.get("lifecycle_stage","new"),p.get("assigned_to"),u["created_at"]))
            for r in interactions:
                target.execute("""INSERT INTO crm_interactions(id,customer_id,actor_user_id,channel,subject,summary,happened_at,created_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING""",
                    (r["id"],r["user_id"],r["actor_user_id"],r["channel"],r["subject"],r["summary"],r["happened_at"],r["created_at"]))
            for r in followups:
                target.execute("""INSERT INTO crm_followups(id,customer_id,assigned_to,created_by,title,details,due_at,status,completed_at,created_at,updated_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING""",
                    (r["id"],r["user_id"],r["assigned_to"],r["created_by"],r["title"],r["details"],r["due_at"],r["status"],r["completed_at"],r["created_at"],r["updated_at"]))
        print("Import committed. Source SaaS records were not changed.")
        return 0
    finally:
        source.close(); target.close()
if __name__=="__main__":
    try: raise SystemExit(main())
    except Exception as exc:
        print(f"CRM import failed: {exc}",file=sys.stderr); raise SystemExit(1)
