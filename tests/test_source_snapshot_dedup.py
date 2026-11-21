import sqlite3
import unittest

from server import _mirrored_source_snapshot_dates


class MirroredSourceSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute(
            """
            CREATE TABLE source_observations (
                ins_code TEXT,
                row_date TEXT,
                endpoint TEXT,
                name TEXT,
                shares INTEGER,
                percent REAL
            )
            """
        )

    def tearDown(self):
        self.db.close()

    def add(self, endpoint, name, shares, percent,
            ins_code="INS1", row_date="2026-11-10"):
        self.db.execute(
            """
            INSERT INTO source_observations
                (ins_code, row_date, endpoint, name, shares, percent)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (ins_code, row_date, endpoint, name, shares, percent),
        )

    def test_exact_complete_views_are_mirrored(self):
        dated = [
            ("شرکت الف", 1000, 10.0),
            ("شخص حقیقی", 500, 5.0),
        ]
        latest = list(reversed(dated))
        for row in dated:
            self.add("shareholders_on_date", *row)
        for row in latest:
            self.add("shareholder_last", *row)

        self.assertIn(
            ("INS1", "2026-11-10"),
            _mirrored_source_snapshot_dates(self.db),
        )

    def test_partial_overlap_is_not_mirrored(self):
        self.add("shareholders_on_date", "شرکت الف", 1000, 10.0)
        self.add("shareholders_on_date", "شرکت ب", 500, 5.0)
        self.add("shareholder_last", "شرکت الف", 1000, 10.0)

        self.assertNotIn(
            ("INS1", "2026-11-10"),
            _mirrored_source_snapshot_dates(self.db),
        )

    def test_duplicate_multiplicity_must_match(self):
        self.add("shareholders_on_date", "شخص حقیقی", 500, 5.0)
        self.add("shareholders_on_date", "شخص حقیقی", 500, 5.0)
        self.add("shareholder_last", "شخص حقیقی", 500, 5.0)

        self.assertNotIn(
            ("INS1", "2026-11-10"),
            _mirrored_source_snapshot_dates(self.db),
        )

    def test_different_ownership_percent_is_not_mirrored(self):
        self.add("shareholders_on_date", "شرکت الف", 1000, 10.0)
        self.add("shareholder_last", "شرکت الف", 1000, 9.9)

        self.assertNotIn(
            ("INS1", "2026-11-10"),
            _mirrored_source_snapshot_dates(self.db),
        )

    def test_single_endpoint_is_not_mirrored(self):
        self.add("shareholders_on_date", "شرکت الف", 1000, 10.0)

        self.assertNotIn(
            ("INS1", "2026-11-10"),
            _mirrored_source_snapshot_dates(self.db),
        )


if __name__ == "__main__":
    unittest.main()
