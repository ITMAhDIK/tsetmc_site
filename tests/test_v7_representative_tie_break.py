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

    def assert_same_representative(self, first, second, expected_field,
                                   expected_value):
        forward = self.resolve([first, second])
        reverse = self.resolve([second, first])
        self.assertEqual(
            tuple(forward.get(k) for k in (
                "shareholder_name", "shareholder_type", "shares_change",
                "price", "market_value", "valuation_status", "entry_status",
                "ownership_percent_reported",
            )),
            tuple(reverse.get(k) for k in (
                "shareholder_name", "shareholder_type", "shares_change",
                "price", "market_value", "valuation_status", "entry_status",
                "ownership_percent_reported",
            )),
        )
        self.assertEqual(forward[expected_field], expected_value)
        self.assertEqual(reverse[expected_field], expected_value)
        self.assertEqual(forward["merged_count"], 2)
        self.assertEqual(reverse["merged_count"], 2)
        self.assertEqual(forward["ownership_percent"], 25.0)
        self.assertEqual(reverse["ownership_percent"], 25.0)

    def test_equal_rank_and_name_choose_same_representative_in_any_row_order(self):
        first = self.row("holder_id", 11, 2, 100, 2500000)
        second = self.row("share_id", 22, 3, 200, 5000000)

        self.assert_same_representative(
            first, second, "shares_change", 3
        )
        forward = self.resolve([first, second])
        self.assertEqual(forward["price"], 200)
        self.assertEqual(forward["market_value"], 5000000)

    def test_numeric_strings_and_multi_digit_values_use_numeric_order(self):
        first = self.row("holder_id", 11, 9, 100, 2500000)
        second = self.row("share_id", 22, "10", 100, 2500000)

        self.assert_same_representative(
            first, second, "shares_change", "10"
        )

    def test_negative_and_decimal_values_use_numeric_order(self):
        negative_low = self.row("holder_id", 11, -10, 100, 2500000)
        negative_high = self.row("share_id", 22, -2, 100, 2500000)
        self.assert_same_representative(
            negative_low, negative_high, "shares_change", -2
        )

        decimal_low = self.row("holder_id", 11, 2, 100, 2500000)
        decimal_high = self.row("share_id", 22, 2, 100, 2500000)
        decimal_low["ownership_percent_reported"] = 1.2
        decimal_high["ownership_percent_reported"] = "1.25"
        self.assert_same_representative(
            decimal_low, decimal_high, "ownership_percent_reported", "1.25"
        )

    def test_missing_and_invalid_numeric_values_sort_below_valid_numbers(self):
        missing = self.row("holder_id", 11, None, 100, 2500000)
        zero = self.row("share_id", 22, 0, 100, 2500000)
        self.assert_same_representative(
            missing, zero, "shares_change", 0
        )

        malformed = self.row("holder_id", 11, "not-a-number", 100, 2500000)
        valid = self.row("share_id", 22, 0, 100, 2500000)
        self.assert_same_representative(
            malformed, valid, "shares_change", 0
        )

        non_finite = self.row("holder_id", 11, float("nan"), 100, 2500000)
        finite = self.row("share_id", 22, 0, 100, 2500000)
        self.assert_same_representative(
            non_finite, finite, "shares_change", 0
        )

    def test_none_and_empty_text_have_stable_distinct_ordering(self):
        missing = self.row("holder_id", 11, 2, 100, 2500000)
        empty = self.row("share_id", 22, 2, 100, 2500000)
        missing["shareholder_type"] = None
        empty["shareholder_type"] = ""

        self.assert_same_representative(
            missing, empty, "shareholder_type", ""
        )


if __name__ == "__main__":
    unittest.main()
