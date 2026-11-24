import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path

from tools.audit_ownership_source import audit_source


class OwnershipSourceAuditTests(unittest.TestCase):
    """Exercise the read-only ownership audit against small, controlled SQLite fixtures."""

    def setUp(self):
        """Create a temporary SQLite source fixture with duplicate keys and conflicting identity links."""

        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "holdings.sqlite"
        con = sqlite3.connect(self.db_path)
        con.executescript("""
            CREATE TABLE shareholder_holdings (
                ins_code TEXT, date TEXT, id_scheme TEXT,
                shareholder_id INTEGER, shares INTEGER,
                total_company_shares INTEGER,
                ownership_percent REAL,
                ownership_percent_calc REAL
            );
            CREATE TABLE company_daily (
                ins_code TEXT, date TEXT, total_company_shares INTEGER
            );
            CREATE TABLE shareholder_id_xref (
                ins_code TEXT, source_scheme TEXT, source_id INTEGER,
                canonical_scheme TEXT, canonical_id INTEGER, status TEXT
            );
            INSERT INTO company_daily VALUES ('X','2026-11-20',100);
            INSERT INTO shareholder_holdings VALUES
              ('X','2026-11-20','holder_id',10,60,100,60,60);
            INSERT INTO shareholder_holdings VALUES
              ('X','2026-11-20','share_id',20,50,100,50,50);
            INSERT INTO shareholder_holdings VALUES
              ('X','2026-11-20','share_id',20,50,100,50,50);
            INSERT INTO shareholder_id_xref VALUES
              ('X','holder_id',10,'canonical',99,'verified_same_source_row');
            INSERT INTO shareholder_id_xref VALUES
              ('X','share_id',20,'canonical',99,'verified_same_source_row');
        """)
        con.commit()
        con.close()

    def tearDown(self):
        """Remove the temporary database directory after each test."""

        self.temp.cleanup()

    def test_detects_duplicate_keys_raw_over_100_and_canonical_conflict(self):
        """Verify the audit reports duplicate source keys, raw totals over 100%, and canonical share conflicts."""

        report = audit_source(self.db_path)
        checks = report["checks"]
        self.assertTrue(report["read_only"])
        self.assertEqual(report["source"]["holdings_rows"], 3)
        self.assertEqual(checks["duplicate_source_keys"]["count"], 1)
        self.assertEqual(
            checks["raw_share_totals_over_100_before_identity_resolution"]["count"],
            1,
        )
        self.assertEqual(
            checks["identity_xref"]["canonical_share_conflicts"]["count"], 1
        )

    def test_same_date_percentage_recalculation_passes_for_fixture_rows(self):
        """Verify reported and pipeline percentages match the fixture's same-date denominator."""

        report = audit_source(self.db_path)
        pct = report["checks"]["percentage_recalculation"]
        self.assertEqual(
            pct["reported_percent_vs_same_date_shares"]["count"], 0
        )
        self.assertEqual(
            pct["pipeline_calc_vs_same_date_shares"]["count"], 0
        )

    def test_audit_does_not_change_source_rows(self):
        """Verify that running the audit leaves the source holdings row count unchanged."""

        with closing(sqlite3.connect(self.db_path)) as con:
            before = con.execute(
                "SELECT COUNT(*) FROM shareholder_holdings"
            ).fetchone()[0]
        audit_source(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as con:
            after = con.execute(
                "SELECT COUNT(*) FROM shareholder_holdings"
            ).fetchone()[0]
        self.assertEqual(before, after)


    def test_denominator_disagreement_and_percentage_mismatch_are_reported(self):
        """Verify denominator-source disagreement and a deliberately incorrect reported percentage are surfaced."""

        with closing(sqlite3.connect(self.db_path)) as con:
            con.execute(
                "UPDATE company_daily SET total_company_shares=90 "
                "WHERE ins_code='X' AND date='2026-11-20'")
            con.commit()
        report = audit_source(self.db_path)
        checks = report["checks"]
        self.assertEqual(
            checks["company_daily_vs_holdings_denominator_disagreements"]["count"],
            1,
        )
        self.assertEqual(
            checks["percentage_recalculation"]
                  ["reported_percent_vs_same_date_shares"]["count"],
            3,
        )

    def test_holdings_denominator_is_used_when_company_daily_total_is_absent(self):
        """Verify the audit falls back to a positive holdings denominator when company_daily has none."""

        with closing(sqlite3.connect(self.db_path)) as con:
            con.execute("DELETE FROM company_daily")
            con.commit()
        report = audit_source(self.db_path)
        checks = report["checks"]
        self.assertEqual(checks["dates_without_positive_denominator"]["count"], 0)
        self.assertEqual(
            checks["percentage_recalculation"]
                  ["reported_percent_vs_same_date_shares"]["count"],
            0,
        )

    def test_invalid_sample_limit_is_rejected(self):
        """Verify sample limits outside the supported range are rejected before querying."""

        with self.assertRaises(ValueError):
            audit_source(self.db_path, sample_limit=0)


if __name__ == "__main__":
    unittest.main()
