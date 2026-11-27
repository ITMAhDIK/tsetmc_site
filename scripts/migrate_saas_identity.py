#!/usr/bin/env python3
"""One-time, read-only-source copy of existing SaaS identity facts into Identity.

Default execution is a dry run. Use --apply only after reviewing counts and
confirming the dedicated target Identity database is empty.
"""
from __future__ import annotations
import argparse
import os
import sys
from psycopg import connect, sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from cryptography.fernet import Fernet

TABLES=("users","auth_identities","email_verifications","sessions","user_admin_mfa",
        "legal_acceptances","admin_audit_logs")

class ImportErrorSafe(RuntimeError): pass

def _validate_dsn(value,name):
    if not value: raise ImportErrorSafe(f"{name} is required")
    params=conninfo_to_dict(value)
    hosts=[v.strip().lower() for v in params.get("host","localhost").split(",") if v.strip()]
    if any(h not in {"localhost","127.0.0.1","::1"} for h in hosts) and params.get("sslmode","prefer").lower()!="verify-full":
        raise ImportErrorSafe(f"Remote {name} requires sslmode=verify-full")

def _reencrypt_mfa_secret(encrypted_secret: str, source_cipher: Fernet, target_cipher: Fernet) -> str:
    """Move an MFA seed between independent encryption keys without exposing it."""
    cleartext = source_cipher.decrypt(encrypted_secret.encode("ascii"))
    return target_cipher.encrypt(cleartext).decode("ascii")

def _copy_values(table, row, columns):
    values=[]
    for column in columns:
        value=row[column]
        if table=="notification_outbox" and column=="payload" and value is not None:
            value=Jsonb(value)
        elif table=="admin_audit_logs" and column in {"before_state","after_state"} and value is not None:
            value=Jsonb(value)
        values.append(value)
    return values

def _count(conn,table):
    return int(conn.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(table))).fetchone()["n"])

def run(apply:bool):
    source_dsn=os.getenv("IDENTITY_IMPORT_SOURCE_DATABASE_URL","").strip()
    target_dsn=os.getenv("IDENTITY_MIGRATION_DATABASE_URL","").strip()
    source_expected=os.getenv("IDENTITY_IMPORT_SOURCE_DATABASE_NAME","").strip()
    target_expected=os.getenv("IDENTITY_EXPECTED_DATABASE_NAME","").strip()
    _validate_dsn(source_dsn,"IDENTITY_IMPORT_SOURCE_DATABASE_URL")
    _validate_dsn(target_dsn,"IDENTITY_MIGRATION_DATABASE_URL")
    if source_dsn==target_dsn: raise ImportErrorSafe("Source and Identity target must be different databases")
    if not source_expected or not target_expected or source_expected==target_expected:
        raise ImportErrorSafe("Expected source and target database names must be set and different")
    with connect(source_dsn,row_factory=dict_row,connect_timeout=5) as source, connect(target_dsn,row_factory=dict_row,connect_timeout=5) as target:
        src=source.execute("SELECT current_database() AS name").fetchone()["name"]
        dst=target.execute("SELECT current_database() AS name").fetchone()["name"]
        if src!=source_expected or dst!=target_expected or src==dst:
            raise ImportErrorSafe("Connected source/target database identity does not match configured names")
        source.execute("SET default_transaction_read_only=on")
        protected_target_tables=("users","auth_identities","email_verifications","phone_verifications",
                                 "sessions","user_admin_mfa","legal_acceptances","notification_outbox",
                                 "admin_audit_logs","user_roles")
        occupied={table:_count(target,table) for table in protected_target_tables}
        occupied={table:count for table,count in occupied.items() if count}
        if occupied:
            raise ImportErrorSafe("Identity target already contains user-owned data; refusing import: "+repr(occupied))
        report={table:_count(source,table) for table in TABLES}
        report["retired_phone_verifications"]=_count(source,"phone_verifications")
        report["not_migrated_saas_notifications"]=_count(source,"notification_outbox")
        report["roles"]=_count(source,"roles")
        report["permissions"]=_count(source,"permissions")
        report["user_roles"]=_count(source,"user_roles")
        report["role_permissions"]=_count(source,"role_permissions")
        print("SOURCE COUNTS",report)
        if not apply:
            print("DRY RUN: no target rows were written. Review counts, then rerun with --apply.")
            return 0
        mfa_count=report["user_admin_mfa"]
        source_mfa_key=os.getenv("IDENTITY_IMPORT_SOURCE_MFA_FERNET_KEY","").strip()
        target_mfa_key=os.getenv("IDENTITY_ADMIN_MFA_FERNET_KEY","").strip()
        source_mfa_cipher=target_mfa_cipher=None
        if mfa_count:
            if not source_mfa_key or not target_mfa_key or source_mfa_key==target_mfa_key:
                raise ImportErrorSafe("Distinct source and target MFA Fernet keys are required to migrate enrolled MFA devices")
            try:
                source_mfa_cipher=Fernet(source_mfa_key.encode("ascii"))
                target_mfa_cipher=Fernet(target_mfa_key.encode("ascii"))
            except Exception as exc:
                raise ImportErrorSafe("Source and target MFA Fernet keys must be valid") from exc
        with target.transaction():
            # Copy principals first. Hashes and existing opaque sessions are
            # retained, allowing users to keep their login after the cutover.
            for row in source.execute("SELECT * FROM users ORDER BY id").fetchall():
                columns=list(row)
                target.execute(sql.SQL("INSERT INTO users ({}) VALUES ({})").format(
                    sql.SQL(",").join(map(sql.Identifier,columns)),
                    sql.SQL(",").join(sql.Placeholder() for _ in columns)),
                    _copy_values("users",row,columns))
            for table in ("roles","permissions"):
                rows=source.execute(sql.SQL("SELECT * FROM {} ORDER BY id").format(sql.Identifier(table))).fetchall()
                for row in rows:
                    # Identity seeds may contain additional permissions; align by stable code,
                    # not by local serial IDs.
                    cols=("code","title") if table=="roles" else ("code","description")
                    target.execute(sql.SQL("INSERT INTO {} ({}) VALUES (%s,%s) ON CONFLICT(code) DO UPDATE SET {}=EXCLUDED.{}").format(
                        sql.Identifier(table),sql.SQL(",").join(map(sql.Identifier,cols)),
                        sql.Identifier(cols[1]),sql.Identifier(cols[1])),
                        (row[cols[0]],row[cols[1]]))
            # Resolve role/permission relationships by natural code.
            role_links=source.execute("""SELECT r.code AS role_code,p.code AS permission_code
                                         FROM role_permissions rp JOIN roles r ON r.id=rp.role_id
                                         JOIN permissions p ON p.id=rp.permission_id""").fetchall()
            for link in role_links:
                target.execute("""INSERT INTO role_permissions(role_id,permission_id)
                                  SELECT r.id,p.id FROM roles r CROSS JOIN permissions p
                                  WHERE r.code=%s AND p.code=%s ON CONFLICT DO NOTHING""",
                               (link["role_code"],link["permission_code"]))
            users_roles=source.execute("""SELECT ur.user_id,ur.granted_by,ur.granted_at,r.code AS role_code
                                          FROM user_roles ur JOIN roles r ON r.id=ur.role_id""").fetchall()
            for row in users_roles:
                target.execute("""INSERT INTO user_roles(user_id,role_id,granted_by,granted_at)
                                  SELECT %s,r.id,%s,%s FROM roles r WHERE r.code=%s
                                  ON CONFLICT DO NOTHING""",
                               (row["user_id"],row["granted_by"],row["granted_at"],row["role_code"]))
            for table in TABLES[1:]:
                rows=source.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier(table))).fetchall()
                for row in rows:
                    row_to_copy=dict(row)
                    if table=="user_admin_mfa":
                        try:
                            row_to_copy["encrypted_secret"]=_reencrypt_mfa_secret(
                                row_to_copy["encrypted_secret"], source_mfa_cipher, target_mfa_cipher
                            )
                        except Exception as exc:
                            raise ImportErrorSafe("Could not re-encrypt an existing MFA secret; source data was not modified") from exc
                    columns=list(row_to_copy)
                    target.execute(sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                        sql.Identifier(table),sql.SQL(",").join(map(sql.Identifier,columns)),
                        sql.SQL(",").join(sql.Placeholder() for _ in columns)),
                        _copy_values(table,row_to_copy,columns))
            for table in ("roles","permissions"):
                target.execute("SELECT setval(pg_get_serial_sequence(%s,'id'),COALESCE((SELECT max(id) FROM "+table+"),1),true)",(table,))
        print("IMPORT APPLIED. Verify Identity readiness, account counts and sample login before enabling Edge cutover.")
        return 0

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply",action="store_true",help="apply the reviewed one-time copy; default is dry-run")
    args=p.parse_args()
    try:return run(args.apply)
    except Exception as exc:
        print(f"Identity import refused or failed: {type(exc).__name__}: {exc}",file=sys.stderr)
        return 2
if __name__=="__main__":raise SystemExit(main())
