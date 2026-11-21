#!/usr/bin/env python3
"""Read-only audit of ownership percentages after a site sidecar rebuild.

This script opens both the source holdings DB and the generated site sidecar
in SQLite read-only mode. It does not rebuild, modify, or repair either file.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

SIDECAR_VERSION_REQUIRED = "10"
DEFAULT_EXPECTED_SYMBOLS = 608


def _readonly_uri(path: str | Path) -> str:
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f"File not found: {p}")
    return p.as_uri() + "?mode=ro"


def _one(con: sqlite3.Connection, sql: str, args=()):
    row = con.execute(sql, args).fetchone()
    return row[0] if row else None


def audit_ownership(db_path: str | Path, sidecar_path: str | Path,
                    expected_symbols: int = DEFAULT_EXPECTED_SYMBOLS) -> dict:
    """Return a JSON-safe audit report without writing to either database."""
    con = sqlite3.connect(_readonly_uri(sidecar_path), uri=True, timeout=60)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only = ON")
        con.execute("ATTACH DATABASE ? AS live", (_readonly_uri(db_path),))
        con.execute("PRAGMA query_only = ON")

        required = {
            "main": ("site_meta", "site_watch", "site_company_daily_dedup",
                     "site_holder_latest", "site_excluded_holder_rows",
                     "site_holder_history"),
            "live": ("shareholder_holdings", "company_daily"),
        }
        for schema, names in required.items():
            for name in names:
                found = _one(
                    con,
                    f"SELECT 1 FROM {schema}.sqlite_master "
                    "WHERE type='table' AND name=?",
                    (name,),
                )
                if not found:
                    raise RuntimeError(f"Required table is missing: {schema}.{name}")

        meta = dict(con.execute(
            "SELECT key, value FROM site_meta "
            "WHERE key IN ('sidecar_version','last_refresh','db_fingerprint')"
        ).fetchall())
        sidecar_version = meta.get("sidecar_version")
        last_refresh = meta.get("last_refresh")
        if not last_refresh:
            raise RuntimeError("Sidecar has no last_refresh completion marker")

        source_symbols = int(_one(
            con, "SELECT COUNT(DISTINCT ins_code) FROM live.shareholder_holdings") or 0)
        watch_symbols = int(_one(con, "SELECT COUNT(*) FROM site_watch") or 0)
        daily_rows = int(_one(
            con, "SELECT COUNT(*) FROM site_company_daily_dedup") or 0)
        daily_symbols = int(_one(
            con, "SELECT COUNT(DISTINCT ins_code) FROM site_company_daily_dedup") or 0)
        holder_latest_rows = int(_one(
            con, "SELECT COUNT(*) FROM site_holder_latest") or 0)
        history_rows = int(_one(
            con, "SELECT COUNT(*) FROM site_holder_history") or 0)
        expected_history_rows = int(_one(con, """
            SELECT COUNT(*) FROM live.shareholder_holdings h
            WHERE NOT EXISTS (
                SELECT 1 FROM site_excluded_holder_rows x
                WHERE x.ins_code=h.ins_code AND x.date=h.date
                  AND x.id_scheme=h.id_scheme
                  AND x.shareholder_id=h.shareholder_id
            )
        """) or 0)

        denominator_missing = int(_one(con, """
            SELECT COUNT(*)
            FROM site_company_daily_dedup d
            LEFT JOIN live.company_daily cd
              ON cd.ins_code=d.ins_code AND cd.date=d.date
            WHERE cd.total_company_shares IS NULL
               OR cd.total_company_shares <= 0
        """) or 0)
        pct_present_without_denominator = int(_one(con, """
            SELECT COUNT(*)
            FROM site_company_daily_dedup d
            LEFT JOIN live.company_daily cd
              ON cd.ins_code=d.ins_code AND cd.date=d.date
            WHERE d.deduped_pct IS NOT NULL
              AND (cd.total_company_shares IS NULL OR cd.total_company_shares <= 0)
        """) or 0)
        formula_mismatch = int(_one(con, """
            SELECT COUNT(*)
            FROM site_company_daily_dedup d
            JOIN live.company_daily cd
              ON cd.ins_code=d.ins_code AND cd.date=d.date
            WHERE cd.total_company_shares > 0
              AND d.deduped_pct IS NOT NULL
              AND ABS(d.deduped_pct -
                      (100.0 * d.deduped_shares / cd.total_company_shares)) > 0.000002
        """) or 0)
        pct_missing_with_positive_shares = int(_one(con, """
            SELECT COUNT(*)
            FROM site_company_daily_dedup d
            JOIN live.company_daily cd
              ON cd.ins_code=d.ins_code AND cd.date=d.date
            WHERE cd.total_company_shares > 0
              AND d.deduped_shares > 0
              AND d.deduped_pct IS NULL
        """) or 0)
        over_100 = int(_one(con, """
            SELECT COUNT(*) FROM site_company_daily_dedup
            WHERE deduped_pct > 100.0
        """) or 0)
        below_zero = int(_one(con, """
            SELECT COUNT(*) FROM site_company_daily_dedup
            WHERE deduped_pct < 0.0
        """) or 0)

        holder_formula_mismatch = int(_one(con, """
            SELECT COUNT(*)
            FROM site_holder_latest s
            LEFT JOIN live.shareholder_holdings h
              ON h.ins_code=s.ins_code AND h.date=s.latest_date
             AND h.id_scheme=s.id_scheme AND h.shareholder_id=s.shareholder_id
            LEFT JOIN live.company_daily cd
              ON cd.ins_code=s.ins_code AND cd.date=s.latest_date
            WHERE s.ownership_percent_calc IS NOT NULL
              AND (h.shares IS NULL OR cd.total_company_shares IS NULL
                   OR cd.total_company_shares <= 0
                   OR ABS(s.ownership_percent_calc -
                          (100.0 * h.shares / cd.total_company_shares)) > 0.000002)
        """) or 0)

        latest_watch_mismatch = int(_one(con, """
            SELECT COUNT(*)
            FROM site_watch w
            LEFT JOIN site_company_daily_dedup d
              ON d.ins_code=w.ins_code AND d.date=w.latest_date
            WHERE (w.deduped_ownership_percent_sum IS NULL
                   AND d.deduped_pct IS NOT NULL)
               OR (w.deduped_ownership_percent_sum IS NOT NULL
                   AND d.deduped_pct IS NULL)
               OR (w.deduped_ownership_percent_sum IS NOT NULL
                   AND d.deduped_pct IS NOT NULL
                   AND ABS(w.deduped_ownership_percent_sum-d.deduped_pct) > 0.000002)
        """) or 0)

        out_of_range = [dict(r) for r in con.execute("""
            SELECT w.symbol, d.date, d.deduped_pct, d.deduped_shares,
                   cd.total_company_shares
            FROM site_company_daily_dedup d
            LEFT JOIN site_watch w ON w.ins_code=d.ins_code
            LEFT JOIN live.company_daily cd
              ON cd.ins_code=d.ins_code AND cd.date=d.date
            WHERE d.deduped_pct > 100.0 OR d.deduped_pct < 0.0
            ORDER BY ABS(d.deduped_pct-100.0) DESC
            LIMIT 50
        """).fetchall()]

        code_integrity_checks = {
            "sidecar_version_is_10": sidecar_version == SIDECAR_VERSION_REQUIRED,
            "historical_holder_rows_match_exclusion_set":
                history_rows == expected_history_rows,
            "source_vs_sidecar_symbol_counts_match": source_symbols == watch_symbols,
            "expected_symbol_count_matches": watch_symbols == expected_symbols,
            "no_percentage_without_same_date_denominator": pct_present_without_denominator == 0,
            "daily_percentage_formula_matches": formula_mismatch == 0,
            "no_positive_share_row_missing_percentage": pct_missing_with_positive_shares == 0,
            "latest_holder_formula_matches": holder_formula_mismatch == 0,
            "latest_watch_matches_daily_series": latest_watch_mismatch == 0,
        }
        report = {
            "sidecar_version": sidecar_version,
            "last_refresh": last_refresh,
            "db_fingerprint": meta.get("db_fingerprint"),
            "source_symbol_count": source_symbols,
            "sidecar_watch_symbol_count": watch_symbols,
            "daily_series_symbol_count": daily_symbols,
            "daily_symbol_date_rows": daily_rows,
            "latest_holder_rows": holder_latest_rows,
            "historical_holder_rows": history_rows,
            "expected_historical_holder_rows": expected_history_rows,
            "dates_missing_same_date_denominator": denominator_missing,
            "percent_present_without_same_date_denominator": pct_present_without_denominator,
            "daily_percentage_formula_mismatches": formula_mismatch,
            "positive_share_rows_missing_percentage": pct_missing_with_positive_shares,
            "latest_holder_percentage_formula_mismatches": holder_formula_mismatch,
            "latest_watch_vs_daily_series_mismatches": latest_watch_mismatch,
            "daily_totals_over_100_percent": over_100,
            "daily_totals_below_0_percent": below_zero,
            "out_of_range_examples": out_of_range,
            "code_integrity_checks": code_integrity_checks,
            "code_integrity_pass": all(code_integrity_checks.values()),
            "data_review_required": bool(denominator_missing or over_100 or below_zero),
            "read_only": True,
        }
        return report
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Read-only audit for a rebuilt TSETMC ownership sidecar.")
    ap.add_argument("--db", required=True,
                    help="path to the source tsetmc_holdings.sqlite database")
    ap.add_argument("--sidecar", required=True,
                    help="path to the rebuilt site_data.sqlite")
    ap.add_argument("--expected-symbols", type=int,
                    default=DEFAULT_EXPECTED_SYMBOLS,
                    help="expected symbol count (default: 608)")
    args = ap.parse_args()
    try:
        report = audit_ownership(args.db, args.sidecar, args.expected_symbols)
    except Exception as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)},
                         ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["code_integrity_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
