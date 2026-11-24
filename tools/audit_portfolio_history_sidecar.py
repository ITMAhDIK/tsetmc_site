#!/usr/bin/env python3
"""Read-only integrity audit for the historical-holder sidecar table."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def _readonly_uri(path: str | Path) -> str:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Database not found: {resolved}")
    return resolved.as_uri() + "?mode=ro"


def audit_history(source_path: str | Path, sidecar_path: str | Path) -> dict:
    """Compare source and sidecar observation multisets without writing."""
    con = sqlite3.connect(_readonly_uri(sidecar_path), uri=True, timeout=60)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only = ON")
        con.execute("ATTACH DATABASE ? AS live", (_readonly_uri(source_path),))
        con.execute("PRAGMA query_only = ON")

        def table_exists(schema: str, table: str) -> bool:
            return con.execute(
                f"SELECT 1 FROM {schema}.sqlite_master "
                "WHERE type='table' AND name=?", (table,)
            ).fetchone() is not None

        for schema, table in (
            ("main", "site_meta"),
            ("main", "site_holder_history"),
            ("main", "site_excluded_holder_rows"),
            ("live", "shareholder_holdings"),
        ):
            if not table_exists(schema, table):
                raise RuntimeError(f"Required table is missing: {schema}.{table}")

        meta = dict(con.execute(
            "SELECT key, value FROM site_meta "
            "WHERE key IN ('sidecar_version', 'last_refresh')"
        ).fetchall())
        source_count = con.execute("""
            SELECT COUNT(*) FROM live.shareholder_holdings h
            WHERE NOT EXISTS (
                SELECT 1 FROM site_excluded_holder_rows x
                WHERE x.ins_code=h.ins_code AND x.date=h.date
                  AND x.id_scheme=h.id_scheme
                  AND x.shareholder_id=h.shareholder_id
            )
        """).fetchone()[0]
        history_count = con.execute(
            "SELECT COUNT(*) FROM site_holder_history"
        ).fetchone()[0]
        key_mismatches = con.execute("""
            WITH source_counts AS (
                SELECT h.ins_code, h.date, h.id_scheme, h.shareholder_id,
                       COUNT(*) AS n
                FROM live.shareholder_holdings h
                WHERE NOT EXISTS (
                    SELECT 1 FROM site_excluded_holder_rows x
                    WHERE x.ins_code=h.ins_code AND x.date=h.date
                      AND x.id_scheme=h.id_scheme
                      AND x.shareholder_id=h.shareholder_id
                )
                GROUP BY h.ins_code, h.date, h.id_scheme, h.shareholder_id
            ), history_counts AS (
                SELECT ins_code, date, id_scheme, shareholder_id, COUNT(*) AS n
                FROM site_holder_history
                GROUP BY ins_code, date, id_scheme, shareholder_id
            ), differences AS (
                SELECT s.ins_code, s.date, s.id_scheme, s.shareholder_id,
                       s.n AS source_n, COALESCE(h.n, 0) AS history_n
                FROM source_counts s
                LEFT JOIN history_counts h
                  USING (ins_code, date, id_scheme, shareholder_id)
                UNION ALL
                SELECT h.ins_code, h.date, h.id_scheme, h.shareholder_id,
                       0 AS source_n, h.n AS history_n
                FROM history_counts h
                LEFT JOIN source_counts s
                  USING (ins_code, date, id_scheme, shareholder_id)
                WHERE s.ins_code IS NULL
            )
            SELECT COUNT(*) FROM differences
            WHERE source_n <> history_n
        """).fetchone()[0]
        indexes = {
            row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        required_indexes = {
            "idx_site_holder_history_identity_date",
            "idx_site_holder_history_symbol",
        }
        integrity = con.execute("PRAGMA main.quick_check").fetchone()[0]
        checks = {
            "sidecar_version_is_10": meta.get("sidecar_version") == "10",
            "refresh_completion_marker_exists": bool(meta.get("last_refresh")),
            "source_and_history_row_counts_match": source_count == history_count,
            "source_and_history_key_multiplicities_match": key_mismatches == 0,
            "history_indexes_exist": required_indexes.issubset(indexes),
            "sidecar_quick_check_ok": integrity == "ok",
        }
        return {
            "sidecar_version": meta.get("sidecar_version"),
            "last_refresh": meta.get("last_refresh"),
            "source_expected_rows": source_count,
            "sidecar_history_rows": history_count,
            "key_multiplicity_mismatches": key_mismatches,
            "sidecar_quick_check": integrity,
            "checks": checks,
            "pass": all(checks.values()),
            "read_only": True,
        }
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only audit of the materialised shareholder history.")
    parser.add_argument("--db", required=True, help="Source holdings SQLite path")
    parser.add_argument("--sidecar", required=True, help="Site sidecar SQLite path")
    args = parser.parse_args()
    try:
        report = audit_history(args.db, args.sidecar)
    except Exception as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)},
                         ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
