import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools.audit_ownership_sidecar import audit_ownership


class OwnershipSidecarAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db_path = root / "source.sqlite"
        self.sidecar_path = root / "site_data.sqlite"
        self.live = sqlite3.connect(self.db_path)
        self.live.executescript("""
            CREATE TABLE shareholder_holdings (
                ins_code TEXT, date TEXT, id_scheme TEXT,
                shareholder_id INTEGER, shares INTEGER
            );
            CREATE TABLE company_daily (
                ins_code TEXT, date TEXT, total_company_shares INTEGER
            );
            INSERT INTO shareholder_holdings
              VALUES ('X','2026-11-20','share_id',10,100);
            INSERT INTO company_daily VALUES ('X','2026-11-20',1000);
        """)
        self.live.commit()
        self.live.close()
        self.side = sqlite3.connect(self.sidecar_path)
        self.side.executescript("""
            CREATE TABLE site_meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE site_watch (
                ins_code TEXT PRIMARY KEY, symbol TEXT, latest_date TEXT,
                deduped_ownership_percent_sum REAL
            );
            CREATE TABLE site_company_daily_dedup (
                ins_code TEXT, date TEXT, deduped_pct REAL,
                deduped_shares INTEGER, PRIMARY KEY(ins_code,date)
            );
            CREATE TABLE site_holder_latest (
                ins_code TEXT, id_scheme TEXT, shareholder_id INTEGER,
                latest_date TEXT, shares INTEGER, ownership_percent_calc REAL
            );
            CREATE TABLE site_excluded_holder_rows (
                ins_code TEXT, date TEXT, id_scheme TEXT,
                shareholder_id INTEGER, reason TEXT
            );
            CREATE TABLE site_holder_history (
                id_scheme TEXT, shareholder_id INTEGER, ins_code TEXT,
                date TEXT, shares INTEGER
            );
            INSERT INTO site_meta VALUES ('sidecar_version','10');
            INSERT INTO site_meta VALUES ('last_refresh','2026-11-22T00:00:00');
            INSERT INTO site_meta VALUES ('db_fingerprint','fixture');
            INSERT INTO site_watch VALUES ('X','X','2026-11-20',10.0);
            INSERT INTO site_company_daily_dedup
              VALUES ('X','2026-11-20',10.0,100);
            INSERT INTO site_holder_latest
              VALUES ('X','share_id',10,'2026-11-20',100,10.0);
            INSERT INTO site_holder_history
              VALUES ('share_id',10,'X','2026-11-20',100);
        """)
        self.side.commit()
        self.side.close()

    def tearDown(self):
        self.temp.cleanup()

    def test_same_date_percentages_pass_read_only_audit(self):
        report = audit_ownership(self.db_path, self.sidecar_path, expected_symbols=1)
        self.assertTrue(report["code_integrity_pass"])
        self.assertEqual(report["daily_percentage_formula_mismatches"], 0)
        self.assertEqual(report["latest_holder_percentage_formula_mismatches"], 0)
        self.assertEqual(report["latest_watch_vs_daily_series_mismatches"], 0)
        self.assertTrue(report["read_only"])

    def test_missing_same_date_denominator_stays_null(self):
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                "INSERT INTO shareholder_holdings VALUES ('X','2026-11-21','share_id',10,100)")
        with sqlite3.connect(self.sidecar_path) as con:
            con.execute(
                "INSERT INTO site_holder_history "
                "VALUES ('share_id',10,'X','2026-11-21',100)")
            con.execute("UPDATE site_watch SET latest_date='2026-11-21', "
                        "deduped_ownership_percent_sum=NULL WHERE ins_code='X'")
            con.execute("INSERT INTO site_company_daily_dedup "
                        "VALUES ('X','2026-11-21',NULL,100)")
            con.execute("UPDATE site_holder_latest SET latest_date='2026-11-21', "
                        "ownership_percent_calc=NULL WHERE ins_code='X'")
        report = audit_ownership(self.db_path, self.sidecar_path, expected_symbols=1)
        self.assertEqual(report["dates_missing_same_date_denominator"], 1)
        self.assertEqual(report["percent_present_without_same_date_denominator"], 0)
        self.assertTrue(report["code_integrity_pass"])
        self.assertTrue(report["data_review_required"])

    def test_over_100_is_reported_for_review_not_clamped(self):
        with sqlite3.connect(self.db_path) as con:
            con.execute("UPDATE shareholder_holdings SET shares=102 WHERE ins_code='X'")
            con.execute("UPDATE company_daily SET total_company_shares=100 WHERE ins_code='X'")
        with sqlite3.connect(self.sidecar_path) as con:
            con.execute("UPDATE site_watch SET deduped_ownership_percent_sum=102 WHERE ins_code='X'")
            con.execute("UPDATE site_company_daily_dedup SET deduped_pct=102, "
                        "deduped_shares=102 WHERE ins_code='X'")
            con.execute("UPDATE site_holder_latest SET shares=102, "
                        "ownership_percent_calc=102 WHERE ins_code='X'")
        report = audit_ownership(self.db_path, self.sidecar_path, expected_symbols=1)
        self.assertEqual(report["daily_totals_over_100_percent"], 1)
        self.assertEqual(report["out_of_range_examples"][0]["deduped_pct"], 102.0)
        self.assertTrue(report["code_integrity_pass"])
        self.assertTrue(report["data_review_required"])

    def test_formula_mismatch_fails_integrity_check(self):
        with sqlite3.connect(self.sidecar_path) as con:
            con.execute("UPDATE site_company_daily_dedup SET deduped_pct=12 WHERE ins_code='X'")
            con.execute("UPDATE site_holder_latest SET ownership_percent_calc=12 WHERE ins_code='X'")
            con.execute("UPDATE site_watch SET deduped_ownership_percent_sum=12 WHERE ins_code='X'")
        report = audit_ownership(self.db_path, self.sidecar_path, expected_symbols=1)
        self.assertEqual(report["daily_percentage_formula_mismatches"], 1)
        self.assertFalse(report["code_integrity_pass"])

    def test_missing_percent_with_positive_shares_is_flagged(self):
        with sqlite3.connect(self.sidecar_path) as con:
            con.execute("UPDATE site_company_daily_dedup SET deduped_pct=NULL WHERE ins_code='X'")
            con.execute("UPDATE site_holder_latest SET ownership_percent_calc=NULL WHERE ins_code='X'")
            con.execute("UPDATE site_watch SET deduped_ownership_percent_sum=NULL WHERE ins_code='X'")
        report = audit_ownership(self.db_path, self.sidecar_path, expected_symbols=1)
        self.assertEqual(report["positive_share_rows_missing_percentage"], 1)
        self.assertFalse(report["code_integrity_pass"])


if __name__ == "__main__":
    unittest.main()
