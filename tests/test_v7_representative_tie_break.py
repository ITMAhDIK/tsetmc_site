"""Regression tests for deterministic ownership representative selection."""
import sqlite3
import unittest

from server import SiteApi


class RepresentativeTieBreakTests(unittest.TestCase):
    def setUp(self):
        self.live = sqlite3.connect(":memory:")
        self.live.row_factory = sqlite3.Row
        self.live.executescript("""
            CREATE TABLE shareholder_id_xref (
                ins_code TEXT, source_scheme TEXT, source_id INTEGER,
                canonical_scheme TEXT, canonical_id INTEGER, status TEXT
            );
            CREATE TABLE company_daily (
                ins_code TEXT, date TEXT, total_company_shares INTEGER
            );
        """)
        self.live.executemany(
            "INSERT INTO shareholder_id_xref VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("INS1", "holder_id", 11, "canonical", 99,
                 "verified_same_source_row"),
                ("INS1", "share_id", 22, "canonical", 99,
                 "verified_same_source_row"),
            ],
        )
        self.live.execute(
            "INSERT INTO company_daily VALUES (?, ?, ?)",
            ("INS1", "2026-11-20", 100000),
        )
        self.api = object.__new__(SiteApi)

    def tearDown(self):
        self.live.close()

    @staticmethod
    def row(scheme, holder_id, change, price, market_value):
        return {
            "id_scheme": scheme,
            "shareholder_id": holder_id,
            "shares": 25000,
            "shareholder_name": "شرکت نمونه",
            "name_key": "شرکت نمونه",
            "shareholder_type": "حقوقی",
            "identity_status": "verified_same_source_row",
            "shares_change": change,
            "price": price,
            "market_value": market_value,
            "valuation_status": "ok",
            "entry_status": "existing",
            "ownership_percent_reported": 25.0,
            "ownership_percent_calculated": 25.0,
        }

    def resolve(self, rows):
        result = self.api._resolve_date_rows(
            self.live, "INS1", "2026-11-20", rows
        )
        self.assertEqual(len(result), 1)
        return result[0]

    def test_equal_rank_and_name_choose_same_representative_in_any_row_order(self):
        first = self.row("holder_id", 11, 2, 100, 2500000)
        second = self.row("share_id", 22, 3, 200, 5000000)

        forward = self.resolve([first, second])
        reverse = self.resolve([second, first])

        fields = (
            "shareholder_name", "shareholder_type", "shares_change",
            "price", "market_value", "valuation_status", "entry_status",
            "ownership_percent_reported",
        )
        self.assertEqual(
            tuple(forward[field] for field in fields),
            tuple(reverse[field] for field in fields),
        )
        self.assertEqual(forward["shares_change"], 3)
        self.assertEqual(forward["price"], 200)
        self.assertEqual(forward["market_value"], 5000000)
        self.assertEqual(forward["merged_count"], 2)
        self.assertEqual(forward["ownership_percent"], 25.0)


if __name__ == "__main__":
    unittest.main()
