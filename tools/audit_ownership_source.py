#!/usr/bin/env python3
"""Read-only source-data audit for TSETMC ownership integrity.

The audit reads the raw holdings database only. It does not rebuild or write
the site Sidecar, alter source rows, or make network requests. Findings are
diagnostics: duplicate keys and percentage differences are not automatically
proof of bad data and must be interpreted with their samples and provenance.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any


def _readonly_uri(path: str | Path) -> str:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Database not found: {resolved}")
    return resolved.as_uri() + "?mode=ro"


def _tables(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f'PRAGMA table_info("{table}")')}


def _query_report(con: sqlite3.Connection, sql: str,
                  sample_limit: int) -> dict[str, Any]:
    count = con.execute(
        "SELECT COUNT(*) FROM (" + sql + ") AS audit_rows").fetchone()[0]
    samples = [dict(r) for r in con.execute(
        sql + " LIMIT ?", (sample_limit,)).fetchall()]
    return {"count": count, "samples": samples}


def audit_source(db_path: str | Path, sample_limit: int = 25) -> dict[str, Any]:
    """Audit source ownership data without modifying the database."""
    if sample_limit < 1 or sample_limit > 500:
        raise ValueError("sample_limit must be between 1 and 500")

    con = sqlite3.connect(_readonly_uri(db_path), uri=True, timeout=60)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only = ON")
        tables = _tables(con)
        required = {"shareholder_holdings", "company_daily"}
        missing = sorted(required - tables)
        if missing:
            raise RuntimeError("Required source tables missing: " + ", ".join(missing))

        hcols = _columns(con, "shareholder_holdings")
        ccols = _columns(con, "company_daily")
        required_h = {"ins_code", "date", "id_scheme", "shareholder_id",
                      "shares", "total_company_shares"}
        required_c = {"ins_code", "date", "total_company_shares"}
        missing_h = sorted(required_h - hcols)
        missing_c = sorted(required_c - ccols)
        if missing_h or missing_c:
            raise RuntimeError(
                "Required columns missing: "
                + ", ".join([*(f"shareholder_holdings.{x}" for x in missing_h),
                             *(f"company_daily.{x}" for x in missing_c)]))

        row_count = con.execute(
            "SELECT COUNT(*) FROM shareholder_holdings").fetchone()[0]
        symbol_count = con.execute(
            "SELECT COUNT(DISTINCT ins_code) FROM shareholder_holdings").fetchone()[0]
        date_count = con.execute(
            "SELECT COUNT(DISTINCT date) FROM shareholder_holdings").fetchone()[0]

        duplicate_source_keys = _query_report(con, """
            SELECT ins_code, date, id_scheme, shareholder_id,
                   COUNT(*) AS row_count,
                   COUNT(DISTINCT shares) AS distinct_share_values,
                   MIN(shares) AS min_shares, MAX(shares) AS max_shares
            FROM shareholder_holdings
            GROUP BY ins_code, date, id_scheme, shareholder_id
            HAVING COUNT(*) > 1
            ORDER BY row_count DESC, ins_code, date
        """, sample_limit)

        company_denominator_conflicts = _query_report(con, """
            SELECT ins_code, date,
                   COUNT(DISTINCT total_company_shares) AS distinct_totals,
                   MIN(total_company_shares) AS min_total,
                   MAX(total_company_shares) AS max_total,
                   COUNT(*) AS source_rows
            FROM company_daily
            WHERE total_company_shares > 0
            GROUP BY ins_code, date
            HAVING COUNT(DISTINCT total_company_shares) > 1
            ORDER BY distinct_totals DESC, ins_code, date
        """, sample_limit)

        holdings_denominator_conflicts = _query_report(con, """
            SELECT ins_code, date,
                   COUNT(DISTINCT total_company_shares) AS distinct_totals,
                   MIN(total_company_shares) AS min_total,
                   MAX(total_company_shares) AS max_total,
                   COUNT(*) AS holdings_rows
            FROM shareholder_holdings
            WHERE total_company_shares > 0
            GROUP BY ins_code, date
            HAVING COUNT(DISTINCT total_company_shares) > 1
            ORDER BY distinct_totals DESC, ins_code, date
        """, sample_limit)

        denominator_source_disagreements = _query_report(con, """
            WITH cd AS (
                SELECT ins_code, date,
                       MIN(total_company_shares) AS cd_min,
                       MAX(total_company_shares) AS cd_max
                FROM company_daily
                WHERE total_company_shares > 0
                GROUP BY ins_code, date
            ), hh AS (
                SELECT ins_code, date,
                       MIN(total_company_shares) AS h_min,
                       MAX(total_company_shares) AS h_max
                FROM shareholder_holdings
                WHERE total_company_shares > 0
                GROUP BY ins_code, date
            )
            SELECT cd.ins_code, cd.date, cd.cd_min, cd.cd_max,
                   hh.h_min, hh.h_max
            FROM cd JOIN hh USING (ins_code, date)
            WHERE cd.cd_min <> cd.cd_max
               OR hh.h_min <> hh.h_max
               OR cd.cd_min <> hh.h_min
               OR cd.cd_max <> hh.h_max
            ORDER BY cd.ins_code, cd.date
        """, sample_limit)

        dates_without_positive_denominator = _query_report(con, """
            WITH dates AS (
                SELECT DISTINCT ins_code, date FROM shareholder_holdings
            ), cd AS (
                SELECT ins_code, date, MAX(total_company_shares) AS total_shares
                FROM company_daily
                WHERE total_company_shares > 0
                GROUP BY ins_code, date
            ), hh AS (
                SELECT ins_code, date, MAX(total_company_shares) AS total_shares
                FROM shareholder_holdings
                WHERE total_company_shares > 0
                GROUP BY ins_code, date
            )
            SELECT dates.ins_code, dates.date
            FROM dates
            LEFT JOIN cd USING (ins_code, date)
            LEFT JOIN hh USING (ins_code, date)
            WHERE cd.total_shares IS NULL AND hh.total_shares IS NULL
            ORDER BY dates.ins_code, dates.date
        """, sample_limit)

        raw_share_totals_over_100 = _query_report(con, """
            WITH totals AS (
                SELECT ins_code, date,
                       SUM(CASE WHEN shares > 0 THEN shares ELSE 0 END) AS raw_shares
                FROM shareholder_holdings
                GROUP BY ins_code, date
            ), cd AS (
                SELECT ins_code, date,
                       MAX(total_company_shares) AS total_shares
                FROM company_daily
                WHERE total_company_shares > 0
                GROUP BY ins_code, date
            ), hh AS (
                SELECT ins_code, date,
                       MAX(total_company_shares) AS total_shares
                FROM shareholder_holdings
                WHERE total_company_shares > 0
                GROUP BY ins_code, date
            ), denoms AS (
                SELECT dates.ins_code, dates.date,
                       COALESCE(cd.total_shares, hh.total_shares) AS total_shares
                FROM (SELECT DISTINCT ins_code, date FROM shareholder_holdings) dates
                LEFT JOIN cd USING (ins_code, date)
                LEFT JOIN hh USING (ins_code, date)
            )
            SELECT t.ins_code, t.date, t.raw_shares, d.total_shares,
                   ROUND(t.raw_shares * 100.0 / d.total_shares, 6)
                       AS raw_percent_before_identity_resolution
            FROM totals t JOIN denoms d USING (ins_code, date)
            WHERE d.total_shares > 0
              AND t.raw_shares * 100.0 / d.total_shares > 100.0
            ORDER BY raw_percent_before_identity_resolution DESC
        """, sample_limit)

        percent_audit: dict[str, Any] = {}
        if "ownership_percent" in hcols:
            percent_audit["reported_percent_vs_same_date_shares"] = _query_report(
                con, """
                WITH cd AS (
                    SELECT ins_code, date, MAX(total_company_shares) AS total_shares
                    FROM company_daily
                    WHERE total_company_shares > 0
                    GROUP BY ins_code, date
                ), hh AS (
                    SELECT ins_code, date, MAX(total_company_shares) AS total_shares
                    FROM shareholder_holdings
                    WHERE total_company_shares > 0
                    GROUP BY ins_code, date
                ), denoms AS (
                    SELECT dates.ins_code, dates.date,
                           COALESCE(cd.total_shares, hh.total_shares) AS total_shares
                    FROM (SELECT DISTINCT ins_code, date FROM shareholder_holdings) dates
                    LEFT JOIN cd USING (ins_code, date)
                    LEFT JOIN hh USING (ins_code, date)
                )
                SELECT h.ins_code, h.date, h.id_scheme, h.shareholder_id,
                       h.shares, h.ownership_percent AS reported_percent,
                       d.total_shares,
                       ROUND(h.shares * 100.0 / d.total_shares, 6)
                           AS same_date_percent,
                       ROUND(h.ownership_percent -
                             (h.shares * 100.0 / d.total_shares), 6)
                           AS difference_percentage_points
                FROM shareholder_holdings h
                JOIN denoms d ON d.ins_code=h.ins_code AND d.date=h.date
                WHERE h.shares IS NOT NULL AND d.total_shares > 0
                  AND h.ownership_percent IS NOT NULL
                  AND ABS(h.ownership_percent -
                          (h.shares * 100.0 / d.total_shares)) > 0.01
                ORDER BY ABS(h.ownership_percent -
                             (h.shares * 100.0 / d.total_shares)) DESC
                """, sample_limit)
        else:
            percent_audit["reported_percent_vs_same_date_shares"] = {
                "skipped": "shareholder_holdings.ownership_percent is absent"}

        if "ownership_percent_calc" in hcols:
            percent_audit["pipeline_calc_vs_same_date_shares"] = _query_report(
                con, """
                WITH cd AS (
                    SELECT ins_code, date, MAX(total_company_shares) AS total_shares
                    FROM company_daily
                    WHERE total_company_shares > 0
                    GROUP BY ins_code, date
                ), hh AS (
                    SELECT ins_code, date, MAX(total_company_shares) AS total_shares
                    FROM shareholder_holdings
                    WHERE total_company_shares > 0
                    GROUP BY ins_code, date
                ), denoms AS (
                    SELECT dates.ins_code, dates.date,
                           COALESCE(cd.total_shares, hh.total_shares) AS total_shares
                    FROM (SELECT DISTINCT ins_code, date FROM shareholder_holdings) dates
                    LEFT JOIN cd USING (ins_code, date)
                    LEFT JOIN hh USING (ins_code, date)
                )
                SELECT h.ins_code, h.date, h.id_scheme, h.shareholder_id,
                       h.shares, h.ownership_percent_calc AS pipeline_percent,
                       d.total_shares,
                       ROUND(h.shares * 100.0 / d.total_shares, 6)
                           AS same_date_percent,
                       ROUND(h.ownership_percent_calc -
                             (h.shares * 100.0 / d.total_shares), 6)
                           AS difference_percentage_points
                FROM shareholder_holdings h
                JOIN denoms d ON d.ins_code=h.ins_code AND d.date=h.date
                WHERE h.shares IS NOT NULL AND d.total_shares > 0
                  AND h.ownership_percent_calc IS NOT NULL
                  AND ABS(h.ownership_percent_calc -
                          (h.shares * 100.0 / d.total_shares)) > 0.01
                ORDER BY ABS(h.ownership_percent_calc -
                             (h.shares * 100.0 / d.total_shares)) DESC
                """, sample_limit)
        else:
            percent_audit["pipeline_calc_vs_same_date_shares"] = {
                "skipped": "shareholder_holdings.ownership_percent_calc is absent"}

        xref_audit: dict[str, Any] = {"available": "shareholder_id_xref" in tables}
        if "shareholder_id_xref" in tables:
            xcols = _columns(con, "shareholder_id_xref")
            xrequired = {"ins_code", "source_scheme", "source_id",
                         "canonical_scheme", "canonical_id", "status"}
            if xrequired.issubset(xcols):
                xref_audit["duplicate_source_keys"] = _query_report(con, """
                    SELECT ins_code, source_scheme, source_id,
                           COUNT(*) AS link_rows,
                           COUNT(DISTINCT canonical_scheme || ':' || canonical_id)
                               AS distinct_canonical_targets,
                           GROUP_CONCAT(DISTINCT status) AS statuses
                    FROM shareholder_id_xref
                    GROUP BY ins_code, source_scheme, source_id
                    HAVING COUNT(*) > 1
                    ORDER BY link_rows DESC, ins_code
                """, sample_limit)
                xref_audit["multiple_canonical_targets"] = _query_report(con, """
                    SELECT ins_code, source_scheme, source_id,
                           COUNT(DISTINCT canonical_scheme || ':' || canonical_id)
                               AS distinct_canonical_targets,
                           GROUP_CONCAT(DISTINCT status) AS statuses
                    FROM shareholder_id_xref
                    GROUP BY ins_code, source_scheme, source_id
                    HAVING COUNT(DISTINCT canonical_scheme || ':' || canonical_id) > 1
                    ORDER BY distinct_canonical_targets DESC, ins_code
                """, sample_limit)
                xref_audit["canonical_share_conflicts"] = _query_report(con, """
                    SELECT h.ins_code, h.date, x.canonical_scheme,
                           x.canonical_id,
                           COUNT(DISTINCT h.shares) AS distinct_share_values,
                           MIN(h.shares) AS min_shares,
                           MAX(h.shares) AS max_shares,
                           COUNT(*) AS member_rows
                    FROM shareholder_holdings h
                    JOIN shareholder_id_xref x
                      ON x.ins_code=h.ins_code
                     AND x.source_scheme=h.id_scheme
                     AND x.source_id=h.shareholder_id
                    WHERE x.status IN ('verified_same_source_row',
                                       'inferred_exact_match')
                      AND h.shares IS NOT NULL
                    GROUP BY h.ins_code, h.date,
                             x.canonical_scheme, x.canonical_id
                    HAVING COUNT(DISTINCT h.shares) > 1
                    ORDER BY distinct_share_values DESC, h.ins_code, h.date
                """, sample_limit)
            else:
                xref_audit["skipped"] = (
                    "shareholder_id_xref does not have the expected source/canonical columns")
        else:
            xref_audit["skipped"] = "shareholder_id_xref table is absent"

        return {
            "status": "completed",
            "read_only": True,
            "source": {
                "database": str(Path(db_path).expanduser().resolve()),
                "holdings_rows": row_count,
                "symbols": symbol_count,
                "dates": date_count,
            },
            "checks": {
                "duplicate_source_keys": duplicate_source_keys,
                "company_daily_denominator_conflicts": company_denominator_conflicts,
                "holdings_denominator_conflicts": holdings_denominator_conflicts,
                "company_daily_vs_holdings_denominator_disagreements":
                    denominator_source_disagreements,
                "dates_without_positive_denominator": dates_without_positive_denominator,
                "raw_share_totals_over_100_before_identity_resolution":
                    raw_share_totals_over_100,
                "percentage_recalculation": percent_audit,
                "identity_xref": xref_audit,
            },
            "interpretation": [
                "Raw totals above 100% are diagnostic only; identity resolution and proven source-row exclusions have not yet been applied.",
                "Duplicate source keys are candidates for inspection, not automatic deletion instructions.",
                "Percentage comparisons use the exact-date resolver precedence: positive company_daily.total_company_shares, otherwise positive holdings.total_company_shares; differences over 0.01 percentage points are sampled.",
                "Conflicting quantities inside a verified/inferred canonical group require review; this audit does not choose MAX or sum the conflicting values.",
            ],
        }
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only audit of raw TSETMC ownership data.")
    parser.add_argument("--db", required=True,
                        help="path to the source tsetmc_holdings.sqlite")
    parser.add_argument("--sample-limit", type=int, default=25,
                        help="maximum examples per check (1..500)")
    parser.add_argument("--output", default="",
                        help="optional JSON report path; only this report file is written")
    args = parser.parse_args()
    try:
        report = audit_source(args.db, args.sample_limit)
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)},
                         ensure_ascii=False, indent=2))
        return 2

    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).expanduser().write_text(rendered + "\n", encoding="utf-8")
        print(f"Report written to {Path(args.output).expanduser().resolve()}")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
