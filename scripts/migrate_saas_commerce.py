#!/usr/bin/env python3
"""Migrate commerce-owned SaaS rows to the independent Commerce database.

Dry-run is the default. The source is forced read-only and both databases are
name-checked. --apply is intended only for a rehearsed maintenance window.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import OrderedDict
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

SOURCE_TABLES = OrderedDict([
    ("users", ("id","status","phone_e164","phone_verified_at","created_at","updated_at")),
    ("plans", ("id","code","title","duration_days","price_toman","active","created_at")),
    ("wallet_accounts", ("user_id","created_at")),
    ("payment_orders", ("id","user_id","plan_id","amount_toman","status","idempotency_key",
                        "phone_e164","currency","created_at","updated_at","expires_at","paid_at")),
    ("payment_attempts", ("id","order_id","provider","provider_order_id","redirect_url","amount_toman",
                          "currency","status","request_ref","failure_code","failure_message",
                          "created_at","verified_at","settled_at")),
    ("subscriptions", ("id","user_id","plan_id","status","starts_at","ends_at","payment_order_id","created_at")),
    ("gateway_events", ("id","provider","provider_event_id","order_id","event_type","payload_sha256",
                        "received_at","processed_at","processing_status")),
    ("payment_refunds", ("id","order_id","payment_attempt_id","amount_toman","status","idempotency_key",
                         "provider_refund_id","reason","requested_by","created_at","updated_at","completed_at")),
    ("wallet_ledger_entries", ("id","user_id","bucket","entry_type","amount_toman","reference_type",
                               "reference_id","idempotency_key","description","created_at")),
    ("wallet_bank_accounts", ("id","user_id","bank_name","account_fingerprint","encrypted_account_token",
                              "verification_status","verified_at","created_at")),
    ("withdrawal_requests", ("id","user_id","amount_toman","status","bank_account_token","bank_account_id",
                             "idempotency_key","requested_at","reviewed_at","reviewed_by","paid_at","review_note")),
    ("referral_codes", ("user_id","code","created_at")),
    ("referrals", ("id","inviter_user_id","referred_user_id","status","qualified_at","created_at")),
    ("referral_rewards", ("id","referral_id","user_id","amount_toman","ledger_entry_id","sms_status","created_at")),
])

USER_ID_QUERY = """
SELECT id,status,phone_e164,phone_verified_at,created_at,updated_at
FROM users
WHERE id IN (
  SELECT user_id FROM subscriptions
  UNION SELECT user_id FROM payment_orders
  UNION SELECT user_id FROM wallet_accounts
  UNION SELECT user_id FROM wallet_ledger_entries
  UNION SELECT user_id FROM wallet_bank_accounts
  UNION SELECT user_id FROM withdrawal_requests
  UNION SELECT reviewed_by FROM withdrawal_requests WHERE reviewed_by IS NOT NULL
  UNION SELECT user_id FROM referral_codes
  UNION SELECT inviter_user_id FROM referrals
  UNION SELECT referred_user_id FROM referrals
  UNION SELECT user_id FROM referral_rewards
  UNION SELECT requested_by FROM payment_refunds
)
ORDER BY id
"""

def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _validate_tls(dsn: str, label: str) -> None:
    params = conninfo_to_dict(dsn)
    hosts = [h.strip().lower() for h in params.get("host", "localhost").split(",") if h.strip()]
    if any(h not in {"localhost","127.0.0.1","::1"} for h in hosts):
        if params.get("sslmode","prefer").lower() != "verify-full":
            raise RuntimeError(f"Remote {label} connections must explicitly use sslmode=verify-full")


def _fetch_source(source):
    # Mark the transaction read-only before its first source query.
    source.execute("SET TRANSACTION READ ONLY")
    result = {}
    for table, columns in SOURCE_TABLES.items():
        names = ",".join(columns)
        result[table] = source.execute(f"SELECT {names} FROM {table} ORDER BY 1").fetchall()
    result["users"] = source.execute(USER_ID_QUERY).fetchall()
    return result


def _counts(rows):
    return {name: len(values) for name, values in rows.items()}


def run(*, apply: bool) -> int:
    source_dsn = _required("SAAS_CUTOVER_SOURCE_DATABASE_URL")
    source_name = _required("SAAS_CUTOVER_SOURCE_DATABASE_NAME")
    target_dsn = _required("COMMERCE_MIGRATION_DATABASE_URL")
    target_name = _required("COMMERCE_EXPECTED_DATABASE_NAME")
    _validate_tls(source_dsn, "source")
    _validate_tls(target_dsn, "Commerce target")
    if source_dsn == target_dsn or source_name == target_name:
        raise RuntimeError("SaaS source and Commerce target must be different databases")

    source = psycopg.connect(source_dsn, row_factory=dict_row, connect_timeout=5)
    target = psycopg.connect(target_dsn, row_factory=dict_row, connect_timeout=5)
    try:
        source.execute("SET default_transaction_read_only = on")
        source.commit()
        actual_source = source.execute("SELECT current_database() AS name").fetchone()["name"]
        actual_target = target.execute("SELECT current_database() AS name").fetchone()["name"]
        if actual_source != source_name or actual_target != target_name:
            raise RuntimeError("database name does not match its explicitly configured expected name")
        target.commit()
        rows = _fetch_source(source)
        source.commit()

        target.execute("SELECT pg_advisory_xact_lock(20261127,3)")
        existing = {}
        for table in SOURCE_TABLES:
            if table == "plans":
                continue  # The Commerce schema seeds its default catalogue.
            existing[table] = target.execute(f"SELECT count(*)::bigint AS n FROM {table}").fetchone()["n"]
        seeded_plans = target.execute(
            "SELECT code,title,duration_days,price_toman FROM plans ORDER BY code"
        ).fetchall()
        allowed_seed = [
            {"code":"premium_monthly","title":"اشتراک ویژه یک‌ماهه","duration_days":30,"price_toman":899000},
            {"code":"premium_quarterly","title":"اشتراک ویژه سه‌ماهه","duration_days":90,"price_toman":2499000},
        ]
        if seeded_plans not in ([], allowed_seed):
            raise RuntimeError("Commerce target contains a non-default plan catalogue; refusing migration")
        if any(existing.values()):
            raise RuntimeError("Commerce target is not empty; refusing to merge or overwrite commerce history")
        if any(row["plan_id"] is None for row in rows["payment_orders"]):
            raise RuntimeError("legacy payment orders contain a missing plan_id; reconcile these orders before migration")
        if any(row["price_toman"] <= 0 for row in rows["plans"]):
            raise RuntimeError("legacy plan catalogue contains a non-positive price; reconcile it before migration")

        print("SaaS -> Commerce migration inventory (read-only source):")
        for table, count in _counts(rows).items():
            print(f"  {table}: {count}")
        if not apply:
            target.rollback()
            print("DRY RUN ONLY: no target rows were written.")
            return 0

        # Remove only the two known seed plans. All other Commerce data was
        # proven empty above, so preserving source plan UUIDs is FK-safe.
        target.execute("DELETE FROM plans")
        for table, values in rows.items():
            if not values:
                continue
            columns = ("id,status,phone_e164,phone_verified_at,created_at,updated_at"
                       if table == "users" else ",".join(SOURCE_TABLES[table]))
            names = [part.strip() for part in columns.split(",")]
            placeholders = ",".join(["%s"] * len(names))
            statement = f"INSERT INTO {table} ({columns}) VALUES ({placeholders})"
            target.executemany(statement, [tuple(row[name] for name in names) for row in values])

        for table, expected_count in _counts(rows).items():
            actual_count = target.execute(f"SELECT count(*)::bigint AS n FROM {table}").fetchone()["n"]
            if actual_count != expected_count:
                raise RuntimeError(f"row-count parity failed for {table}: expected {expected_count}, got {actual_count}")

        source_totals = source.execute(
            """SELECT bucket,COALESCE(SUM(amount_toman),0)::bigint AS total
               FROM wallet_ledger_entries GROUP BY bucket ORDER BY bucket"""
        ).fetchall()
        target_totals = target.execute(
            """SELECT bucket,COALESCE(SUM(amount_toman),0)::bigint AS total
               FROM wallet_ledger_entries GROUP BY bucket ORDER BY bucket"""
        ).fetchall()
        if source_totals != target_totals:
            raise RuntimeError("wallet ledger bucket totals differ after migration")
        target.commit()
        print("APPLIED: all commerce table row counts and wallet bucket totals match.")
        return 0
    except Exception:
        target.rollback()
        raise
    finally:
        source.close()
        target.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="inspect source and target without writing (default)")
    mode.add_argument("--apply", action="store_true", help="copy rows into an empty Commerce database")
    args = parser.parse_args(argv)
    try:
        return run(apply=bool(args.apply))
    except (RuntimeError, psycopg.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
