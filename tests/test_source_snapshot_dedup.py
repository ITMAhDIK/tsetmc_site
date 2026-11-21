import sqlite3
import unittest

from server import (
    _duplicate_ondate_source_ids,
    _mirrored_source_snapshot_dates,
)


class SourceSnapshotDedupTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute(
            """
            CREATE TABLE source_observations (
                ins_code TEXT,
                row_date TEXT,
                endpoint TEXT,
                request_ref TEXT,
                name TEXT,
                shares INTEGER,
                percent REAL,
                change_code INTEGER,
                change_amount REAL,
                source_id INTEGER
            )
            """
        )
        self.next_source_id = 100

    def tearDown(self):
        self.db.close()

    def add(self, endpoint, name, shares, percent,
            ins_code="INS1", row_date="2026-11-10",
            request_ref="dEven:20261109", change_code=1,
            change_amount=0):
        self.db.execute(
            """
            INSERT INTO source_observations
                (ins_code, row_date, endpoint, request_ref, name, shares,
                 percent, change_code, change_amount, source_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ins_code, row_date, endpoint, request_ref, name, shares,
             percent, change_code, change_amount, self.next_source_id),
        )
        self.next_source_id += 1

    def test_exact_complete_views_are_mirrored(self):
        rows = [("شرکت الف", 1000, 10.0), ("شخص حقیقی", 500, 5.0)]
        for row in rows:
            self.add("shareholders_on_date", *row)
        for row in reversed(rows):
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

    def test_repeated_endpoint_batch_excludes_only_extra_ids(self):
        for i in range(4):
            row = ("شخص حقیقي %d" % i, 1000 + i, 10.0 + i)
            self.add("shareholders_on_date", *row)
            self.add("shareholders_on_date", *row)
        self.add("shareholders_on_date", "شرکت یکتا", 777, 1.0)
        # source IDs are increasing; the second copy of each pair is removed.
        excluded = _duplicate_ondate_source_ids(self.db)
        self.assertEqual(
            excluded,
            {("INS1", "2026-11-10", 101),
             ("INS1", "2026-11-10", 103),
             ("INS1", "2026-11-10", 105),
             ("INS1", "2026-11-10", 107)},
        )

    def test_low_duplicate_fraction_is_not_excluded(self):
        for i in range(3):
            row = ("شخص حقیقی %d" % i, 1000 + i, 10.0 + i)
            self.add("shareholders_on_date", *row)
            self.add("shareholders_on_date", *row)
        self.add("shareholders_on_date", "شرکت یکتا الف", 700, 7.0)
        self.add("shareholders_on_date", "شرکت یکتا ب", 600, 6.0)
        self.assertEqual(_duplicate_ondate_source_ids(self.db), set())

    def test_triplicate_signature_disqualifies_the_batch(self):
        rows = [("شرکت %d" % i, 1000 + i, 10.0 + i) for i in range(3)]
        for row in rows:
            self.add("shareholders_on_date", *row)
            self.add("shareholders_on_date", *row)
        self.add("shareholders_on_date", *rows[0])
        self.assertEqual(_duplicate_ondate_source_ids(self.db), set())


if __name__ == "__main__":
    unittest.main()
