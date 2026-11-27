#!/usr/bin/env python3
"""Idempotently copy legacy SaaS product analytics into the dedicated Analytics DB.

The source is read-only and is never modified or truncated. The operator must
explicitly choose --dry-run or --apply and verify both expected database names.
"""
from __future__ import annotations

import argparse
import os
import sys
import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from urllib.parse import urlsplit

from analytics_service.service import normalize_page_path


def normalize_legacy_path(value: str) -> str:
    """Strip legacy query/fragment data and safely fall back for unsafe paths."""
    try:
        return normalize_page_path(value)
    except (TypeError, ValueError):
        try:
            parsed = urlsplit(str(value))
            candidate = parsed.path
            if not candidate.startswith("/") or candidate.startswith("//"):
                return "/"
            return normalize_page_path(candidate)
        except (TypeError, ValueError):
            return "/"

EVENT_COLUMNS = (
    "id,user_id,anonymous_id,session_id,event_name,page_path,component_id,element_role,"
    "occurred_at,received_at,viewport_width,viewport_height,scroll_depth_pct,"
    "active_duration_ms,metadata,client_event_id,x_pct,y_pct,event_order"
)


def validate_connection_tls(dsn: str) -> None:
    try:
        params = conninfo_to_dict(dsn)
    except Exception as exc:
        raise RuntimeError("analytics import connection string is invalid") from exc
    hosts = [part.strip().lower() for part in params.get("host", "localhost").split(",") if part.strip()]
    local_hosts = {"localhost", "127.0.0.1", "::1"}
    if any(host not in local_hosts for host in hosts):
        if params.get("sslmode", "prefer").lower() != "verify-full":
            raise RuntimeError("remote analytics import connections require sslmode=verify-full")


def _connect(url: str, expected: str, *, readonly: bool):
    if not url or not expected:
        raise RuntimeError("Both database URLs and their expected names are required")
    validate_connection_tls(url)
    conn = psycopg.connect(url, row_factory=dict_row, connect_timeout=5)
    actual = conn.execute("SELECT current_database() AS name").fetchone()["name"]
    if actual != expected:
        conn.close()
        raise RuntimeError(f"connected database {actual!r} does not match expected {expected!r}")
    if readonly:
        conn.execute("SET default_transaction_read_only = on")
        conn.commit()
    return conn


def source_counts(source) -> dict[str, int]:
    with source.cursor() as cur:
        cur.execute("SELECT count(*)::bigint AS n FROM analytics_consents")
        consents = int(cur.fetchone()["n"])
        cur.execute("SELECT count(*)::bigint AS n FROM analytics_events")
        events = int(cur.fetchone()["n"])
        cur.execute(
            """SELECT count(*)::bigint AS n FROM analytics_events
               WHERE user_id IS NULL OR session_id IS NULL"""
        )
        unlinked_events = int(cur.fetchone()["n"])
        cur.execute("SELECT count(*)::bigint AS n FROM admin_audit_logs WHERE action LIKE 'analytics.%'")
        audits = int(cur.fetchone()["n"])
    return {
        "consents": consents, "events": events,
        "linked_events": events - unlinked_events,
        "unlinked_events_not_imported": unlinked_events,
        "audit_logs": audits,
    }


def migrate(source, target, *, batch_size: int = 1000) -> dict[str, int]:
    if not 1 <= batch_size <= 10_000:
        raise ValueError("batch_size must be between 1 and 10000")
    counts = source_counts(source)
    with target.transaction():
        with source.cursor(name="legacy_analytics_consents_export") as src:
            src.itersize = batch_size
            src.execute(
                """SELECT user_id,essential,product_analytics,consent_version,updated_at
                   FROM analytics_consents ORDER BY user_id"""
            )
            with target.cursor() as dst:
                while True:
                    batch = src.fetchmany(batch_size)
                    if not batch:
                        break
                    for row in batch:
                        dst.execute(
                            """INSERT INTO analytics_consents
                               (user_id,essential,product_analytics,consent_version,updated_at)
                               VALUES (%s,%s,%s,%s,%s)
                               ON CONFLICT (user_id) DO UPDATE SET user_id=EXCLUDED.user_id
                               WHERE analytics_consents.essential IS NOT DISTINCT FROM EXCLUDED.essential
                                 AND analytics_consents.product_analytics IS NOT DISTINCT FROM EXCLUDED.product_analytics
                                 AND analytics_consents.consent_version IS NOT DISTINCT FROM EXCLUDED.consent_version
                                 AND analytics_consents.updated_at IS NOT DISTINCT FROM EXCLUDED.updated_at
                               RETURNING user_id""",
                            (row["user_id"],row["essential"],row["product_analytics"],
                             row["consent_version"],row["updated_at"]),
                        )
                        if dst.fetchone() is None:
                            raise RuntimeError("conflicting consent row found in Analytics target; refusing overwrite")
                        dst.execute(
                            """INSERT INTO analytics_consent_history
                               (user_id,essential,product_analytics,consent_version,source,occurred_at)
                               SELECT %s,%s,%s,%s,'legacy_import',%s
                               WHERE NOT EXISTS (
                                 SELECT 1 FROM analytics_consent_history
                                 WHERE user_id=%s AND source='legacy_import'
                               )""",
                            (row["user_id"],row["essential"],row["product_analytics"],
                             row["consent_version"],row["updated_at"],row["user_id"]),
                        )

        with source.cursor(name="legacy_analytics_events_export") as src:
            src.itersize = batch_size
            select_columns = EVENT_COLUMNS.replace(
                ",client_event_id,", ",COALESCE(client_event_id,id) AS client_event_id,"
            )
            src.execute(
                f"SELECT {select_columns} FROM analytics_events "
                "WHERE user_id IS NOT NULL AND session_id IS NOT NULL ORDER BY id"
            )
            with target.cursor() as dst:
                while True:
                    batch = src.fetchmany(batch_size)
                    if not batch:
                        break
                    for row in batch:
                        values_list = [row[column] for column in EVENT_COLUMNS.split(",")]
                        metadata_index = EVENT_COLUMNS.split(",").index("metadata")
                        # Legacy metadata was free-form and may contain PII. Never import it.
                        values_list[metadata_index] = Jsonb({})
                        page_path_index = EVENT_COLUMNS.split(",").index("page_path")
                        values_list[page_path_index] = normalize_legacy_path(
                            values_list[page_path_index]
                        )
                        values = tuple(values_list)
                        dst.execute(
                            f"""INSERT INTO analytics_events ({EVENT_COLUMNS})
                                VALUES ({','.join(['%s'] * len(values))})
                                ON CONFLICT (id) DO UPDATE SET id=EXCLUDED.id
                                WHERE analytics_events.user_id IS NOT DISTINCT FROM EXCLUDED.user_id
                                  AND analytics_events.session_id IS NOT DISTINCT FROM EXCLUDED.session_id
                                  AND analytics_events.event_name IS NOT DISTINCT FROM EXCLUDED.event_name
                                  AND analytics_events.page_path IS NOT DISTINCT FROM EXCLUDED.page_path
                                  AND analytics_events.client_event_id IS NOT DISTINCT FROM EXCLUDED.client_event_id
                                  AND analytics_events.event_order IS NOT DISTINCT FROM EXCLUDED.event_order
                                RETURNING id""",
                            values,
                        )
                        if dst.fetchone() is None:
                            raise RuntimeError("conflicting event row found in Analytics target; refusing overwrite")

        with source.cursor(name="legacy_analytics_audit_export") as src:
            src.itersize = batch_size
            src.execute(
                """SELECT id,actor_user_id,action,target_type,target_id,request_id,created_at
                   FROM admin_audit_logs WHERE action LIKE 'analytics.%' ORDER BY id"""
            )
            with target.cursor() as dst:
                while True:
                    batch = src.fetchmany(batch_size)
                    if not batch:
                        break
                    for row in batch:
                        audit_columns = (
                            "id", "actor_user_id", "action", "target_type",
                            "target_id", "request_id", "created_at"
                        )
                        dst.execute(
                            """INSERT INTO analytics_audit_logs
                               (id,actor_user_id,action,target_type,target_id,request_id,created_at)
                               VALUES (%s,%s,%s,%s,%s,%s,%s)
                               ON CONFLICT (id) DO NOTHING RETURNING id""",
                            tuple(row[column] for column in audit_columns),
                        )
                        if dst.fetchone() is None:
                            dst.execute(
                                """SELECT actor_user_id,action,target_type,target_id,request_id,created_at
                                   FROM analytics_audit_logs WHERE id=%s""",
                                (row["id"],),
                            )
                            existing = dst.fetchone()
                            if existing is None or tuple(existing[column] for column in audit_columns[1:]) != tuple(
                                row[column] for column in audit_columns[1:]
                            ):
                                raise RuntimeError(
                                    "conflicting audit row found in Analytics target; refusing overwrite"
                                )
        target.execute(
            """SELECT setval('analytics_event_order_seq',
                       GREATEST(COALESCE((SELECT max(event_order) FROM analytics_events),1),1),
                       EXISTS(SELECT 1 FROM analytics_events))"""
        )
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--batch-size", type=int, default=1000)
    args = parser.parse_args(argv)
    source_url = os.getenv("ANALYTICS_IMPORT_SOURCE_DATABASE_URL", "").strip()
    source_name = os.getenv("ANALYTICS_IMPORT_SOURCE_DATABASE_NAME", "").strip()
    target_url = os.getenv("ANALYTICS_IMPORT_TARGET_DATABASE_URL", "").strip()
    target_name = os.getenv("ANALYTICS_EXPECTED_DATABASE_NAME", "").strip()
    if not target_url:
        target_url = os.getenv("ANALYTICS_MIGRATION_DATABASE_URL", "").strip()
    source = target = None
    try:
        source = _connect(source_url, source_name, readonly=True)
        target = _connect(target_url, target_name, readonly=False)
        if source_name == target_name:
            raise RuntimeError("legacy source and Analytics target must be different databases")
        counts = source_counts(source)
        if args.dry_run:
            print("DRY RUN — no target rows written")
        else:
            counts = migrate(source, target, batch_size=args.batch_size)
            print("IMPORT COMPLETE — source data retained; no deletes or truncates performed")
        print("Source counts: " + ", ".join(f"{k}={v}" for k,v in counts.items()))
        return 0
    except (psycopg.Error, RuntimeError, ValueError) as exc:
        if target is not None:
            target.rollback()
        print(f"Analytics import failed: {exc}", file=sys.stderr)
        return 2
    finally:
        if source is not None:
            source.close()
        if target is not None:
            target.close()


if __name__ == "__main__":
    raise SystemExit(main())
