#!/usr/bin/env python3
"""Verify the Identity copy and retire SaaS-side credential/session secrets.

Dry-run by default. --apply is an explicit production cutover operation and
must be run only during a controlled maintenance window with both databases
backed up and application writes paused.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

import psycopg
from psycopg.conninfo import conninfo_to_dict
from cryptography.fernet import Fernet
import hmac

from psycopg.rows import dict_row

TABLES = (
    "users", "auth_identities", "email_verifications",
    "sessions", "user_admin_mfa", "legal_acceptances", "admin_audit_logs",
)

class CutoverError(RuntimeError):
    pass

def _connection_settings():
    source = os.getenv("IDENTITY_CUTOVER_SOURCE_DATABASE_URL", "").strip()
    identity = os.getenv("IDENTITY_DATABASE_URL", "").strip()
    source_name = os.getenv("IDENTITY_IMPORT_SOURCE_DATABASE_NAME", "").strip()
    identity_name = os.getenv("IDENTITY_EXPECTED_DATABASE_NAME", "").strip()
    if not source or not identity or source == identity:
        raise CutoverError("Distinct SaaS source and Identity database URLs are required")
    if not source_name or not identity_name or source_name == identity_name:
        raise CutoverError("Distinct expected SaaS and Identity database names are required")
    for value, label in ((source, "SaaS"), (identity, "Identity")):
        params = conninfo_to_dict(value)
        hosts = [v.strip().lower() for v in params.get("host", "localhost").split(",") if v.strip()]
        if any(h not in {"localhost", "127.0.0.1", "::1"} for h in hosts) and params.get("sslmode", "prefer").lower() != "verify-full":
            raise CutoverError(f"Remote {label} database connections require sslmode=verify-full")
    return source, identity, source_name, identity_name

def _actual_database(conn, expected: str, label: str):
    actual = conn.execute("SELECT current_database() AS name").fetchone()["name"]
    if actual != expected:
        raise CutoverError(f"Connected {label} database does not match its configured name")
    return actual

def _digest(conn, query: str) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    for row in conn.execute(query):
        values = []
        for value in (row.values() if isinstance(row, dict) else row):
            if isinstance(value, (datetime, date, Decimal, UUID)):
                value = str(value)
            values.append(value)
        digest.update(json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        digest.update(b"\n")
        count += 1
    return count, digest.hexdigest()

def _verify(source, identity):
    checks = {
        "users": "SELECT id,public_id,username,password_hash,status FROM users ORDER BY id",
        "sessions": "SELECT id,user_id,token_hash,expires_at,revoked_at,mfa_verified_at FROM sessions ORDER BY id",
        "auth_identities": "SELECT id,user_id,provider,provider_subject FROM auth_identities ORDER BY id",
        "email_verifications": "SELECT id,user_id,token_hash,expires_at,consumed_at FROM email_verifications ORDER BY id",
        "user_admin_mfa": "SELECT user_id,enabled_at,last_verified_counter FROM user_admin_mfa ORDER BY user_id",
        "legal_acceptances": "SELECT id,user_id,terms_version,privacy_version,accepted_at,source FROM legal_acceptances ORDER BY id",
        "admin_audit_logs": "SELECT id,actor_user_id,action,target_type,target_id,request_id,ip_hash,before_state,after_state,created_at FROM admin_audit_logs ORDER BY id",
    }
    results = {}
    for name, query in checks.items():
        left = _digest(source, query)
        right = _digest(identity, query)
        results[name] = {"source_rows": left[0], "identity_rows": right[0], "match": left == right}
        if left != right:
            raise CutoverError(f"Identity copy verification failed for {name}: {results[name]}")
    source_mfa_key=os.getenv("IDENTITY_IMPORT_SOURCE_MFA_FERNET_KEY","").strip()
    target_mfa_key=os.getenv("IDENTITY_ADMIN_MFA_FERNET_KEY","").strip()
    mfa_source_count=_digest(source,"SELECT user_id FROM user_admin_mfa ORDER BY user_id")[0]
    mfa_target_count=_digest(identity,"SELECT user_id FROM user_admin_mfa ORDER BY user_id")[0]
    if mfa_source_count or mfa_target_count:
        if not source_mfa_key or not target_mfa_key:
            raise CutoverError("Source and target MFA keys are required to verify encrypted MFA migration")
        try:
            source_cipher=Fernet(source_mfa_key.encode("ascii"))
            target_cipher=Fernet(target_mfa_key.encode("ascii"))
            source_digest=hashlib.sha256()
            target_digest=hashlib.sha256()
            source_rows=source.execute("""SELECT user_id,encrypted_secret,enabled_at,last_verified_counter
                                          FROM user_admin_mfa ORDER BY user_id""").fetchall()
            target_rows=identity.execute("""SELECT user_id,encrypted_secret,enabled_at,last_verified_counter
                                            FROM user_admin_mfa ORDER BY user_id""").fetchall()
            for row,cipher,digest in (
                *((row,source_cipher,source_digest) for row in source_rows),
                *((row,target_cipher,target_digest) for row in target_rows),
            ):
                secret=cipher.decrypt(row["encrypted_secret"].encode("ascii"))
                values=[str(row["user_id"]),secret.decode("utf-8"),
                        str(row["enabled_at"]),str(row["last_verified_counter"])]
                digest.update(json.dumps(values,separators=(",",":")).encode("utf-8"))
                digest.update(b"\\n")
            same=(len(source_rows)==len(target_rows)
                  and hmac.compare_digest(source_digest.digest(),target_digest.digest()))
            results["user_admin_mfa"]={"source_rows":len(source_rows),"identity_rows":len(target_rows),"match_after_reencryption":same}
            if not same:
                raise CutoverError("Identity copy verification failed for MFA secrets after re-encryption")
        except CutoverError:
            raise
        except Exception as exc:
            raise CutoverError("MFA copy could not be decrypted for parity verification") from exc
    role_query = """SELECT ur.user_id,r.code,ur.granted_by,ur.granted_at
                    FROM user_roles ur JOIN roles r ON r.id=ur.role_id
                    ORDER BY ur.user_id,r.code"""
    if _digest(source, role_query) != _digest(identity, role_query):
        raise CutoverError("Identity copy verification failed for user-role assignments")
    permission_query = """SELECT r.code,p.code FROM role_permissions rp
                          JOIN roles r ON r.id=rp.role_id
                          JOIN permissions p ON p.id=rp.permission_id
                          ORDER BY r.code,p.code"""
    if _digest(source, permission_query) != _digest(identity, permission_query):
        raise CutoverError("Identity copy verification failed for role-permission assignments")
    return results

def run(apply: bool):
    source_dsn, identity_dsn, source_name, identity_name = _connection_settings()
    with psycopg.connect(source_dsn, row_factory=dict_row, connect_timeout=5) as source, psycopg.connect(
        identity_dsn, row_factory=dict_row, connect_timeout=5
    ) as identity:
        _actual_database(source, source_name, "SaaS")
        _actual_database(identity, identity_name, "Identity")
        report = _verify(source, identity)
        print("IDENTITY COPY VERIFIED (OTP challenges and SaaS notifications are intentionally excluded)", report)
        if not apply:
            print("DRY RUN: no credentials or sessions were changed. Review backup and cutover readiness, then rerun with --apply.")
            return 0
        with source.transaction():
            source.execute("UPDATE users SET password_hash=NULL WHERE password_hash IS NOT NULL")
            source.execute("""UPDATE sessions
                              SET token_hash='retired:' || id::text,
                                  revoked_at=COALESCE(revoked_at,now()),
                                  mfa_verified_at=NULL""")
            source.execute("""UPDATE phone_verifications
                              SET otp_hash='retired',consumed_at=COALESCE(consumed_at,now())""")
            source.execute("DELETE FROM email_verifications")
            source.execute("DELETE FROM user_admin_mfa")
            source.execute("""UPDATE notification_outbox
                              SET status=CASE WHEN status='pending' THEN 'failed' ELSE status END,
                                  destination_token='retired',payload='{}'::jsonb,locked_at=NULL,
                                  last_error=CASE WHEN status='pending' THEN 'identity_cutover' ELSE last_error END
                              WHERE channel='sms' AND template_code='phone_otp'""")
        # Do not deliver stale OTPs copied during the maintenance-window snapshot.
        with identity.transaction():
            identity.execute("UPDATE phone_verifications SET consumed_at=COALESCE(consumed_at,now())")
            identity.execute("""UPDATE notification_outbox
                                 SET status=CASE WHEN status='pending' THEN 'failed' ELSE status END,
                                     destination_token='retired',payload='{}'::jsonb,locked_at=NULL,
                                     last_error=CASE WHEN status='pending' THEN 'identity_cutover' ELSE last_error END
                                 WHERE channel='sms' AND template_code='phone_otp'""")
        print("SaaS credential and session secrets retired. Identity remains the authentication source of truth.")
        return 0

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="retire SaaS-side credentials after parity verification")
    args = parser.parse_args()
    try:
        return run(args.apply)
    except Exception as exc:
        print(f"Identity cutover refused or failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
