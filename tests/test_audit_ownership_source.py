import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools.audit_ownership_source import audit_source


class OwnershipSourceAuditTests(unittest.TestCase):
    def setUp(self):
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
        self.temp.cleanup()

    def test_detects_duplicate_keys_raw_over_100_and_canonical_conflict(self):
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
        report = audit_source(self.db_path)
        pct = report["checks"]["percentage_recalculation"]
        self.assertEqual(
            pct["reported_percent_vs_same_date_shares"]["count"], 0
        )
        self.assertEqual(
            pct["pipeline_calc_vs_same_date_shares"]["count"], 0
        )

    def test_audit_does_not_change_source_rows(self):
        before = sqlite3.connect(self.db_path).execute(
            "SELECT COUNT(*) FROM shareholder_holdings"
        ).fetchone()[0]
        audit_source(self.db_path)
        after = sqlite3.connect(self.db_path).execute(
            "SELECT COUNT(*) FROM shareholder_holdings"
        ).fetchone()[0]
        self.assertEqual(before, after)

    def test_invalid_sample_limit_is_rejected(self):
        with self.assertRaises(ValueError):
            audit_source(self.db_path, sample_limit=0)


if __name__ == "__main__":
    unittest.main()
