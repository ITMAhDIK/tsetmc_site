"""Regression tests for ownership resolver Sidecar readiness."""
import sqlite3
import unittest

from server import ApiError, SiteApi, refresh_summaries


class OwnershipResolverReadinessTests(unittest.TestCase):
    def setUp(self):
        self.api = SiteApi(":memory:", ":memory:", ".", ".")
        self.sc = sqlite3.connect(":memory:")
        self.sc.execute(
            "CREATE TABLE site_meta (key TEXT PRIMARY KEY, value TEXT)"
        )

    def tearDown(self):
        self.sc.close()

    def put_meta(self, key, value):
        self.sc.execute(
            "INSERT OR REPLACE INTO site_meta (key, value) VALUES (?, ?)",
            (key, value),
        )
        self.sc.commit()

    def test_ready_requires_completion_marker_and_expected_resolver_schema(self):
        self.put_meta("last_refresh", "2026-11-22T10:00:00")
        self.put_meta(
            "ownership_resolver_schema", "resolved-v1-source-exclusions"
        )
        self.api.require_ready(self.sc)

    def test_legacy_sidecar_is_not_ready_even_with_old_last_refresh(self):
        self.put_meta("last_refresh", "2026-11-21T10:00:00")
        with self.assertRaises(ApiError) as raised:
            self.api.require_ready(self.sc)
        self.assertEqual(raised.exception.status, 503)

    def test_wrong_resolver_schema_is_not_ready(self):
        self.put_meta("last_refresh", "2026-11-22T10:00:00")
        self.put_meta("ownership_resolver_schema", "some-other-layout")
        with self.assertRaises(ApiError) as raised:
            self.api.require_ready(self.sc)
        self.assertEqual(raised.exception.status, 503)

    def test_failed_early_refresh_cannot_leave_stale_ready_markers(self):
        self.put_meta("last_refresh", "2026-11-21T10:00:00")
        self.put_meta(
            "ownership_resolver_schema", "resolved-v1-source-exclusions"
        )
        self.sc.execute(
            "CREATE TABLE site_excluded_holder_rows ("
            "ins_code TEXT, date TEXT, id_scheme TEXT, "
            "shareholder_id INTEGER, reason TEXT)"
        )
        self.sc.commit()

        live = sqlite3.connect(":memory:")
        try:
            # No shareholder_holdings table: exclusion collection fails
            # before the normal refresh stages. Readiness must already be
            # invalidated, even for this early failure path.
            with self.assertRaises(sqlite3.OperationalError):
                refresh_summaries(live, self.sc)
        finally:
            live.close()

        self.assertIsNone(
            self.sc.execute(
                "SELECT value FROM site_meta WHERE key='last_refresh'"
            ).fetchone()
        )
        self.assertIsNone(
            self.sc.execute(
                "SELECT value FROM site_meta "
                "WHERE key='ownership_resolver_schema'"
            ).fetchone()
        )


if __name__ == "__main__":
    unittest.main()
