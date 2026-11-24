"""Regression tests for the investor sector-affinity engine."""
import unittest
from sector_affinity import SectorAffinityAccumulator


def _sector(group="G1", label="Group 1", sub="S1", category="CAT1"):
    return {
        "cSecVal": group, "lSecVal": label,
        "cSoSecVal": sub, "lSoSecVal": "Sub " + sub if sub else None,
        "dashboard_category_id": category,
        "dashboard_category_label": "Category 1" if category else None,
    }


def _row(day, ins, shares, price, **extra):
    row = {
        "date": day, "ins_code": ins, "symbol": ins,
        "id_scheme": "share_id", "shareholder_id": 10,
        "identity_status": "verified_same_source_row",
        "shares": shares, "reference_price": price,
        "reference_price_date": day if price is not None else None,
        "shares_change": None, "change_calculation_status": None,
        "corporate_action": None, "identity_conflict": False,
        "anomaly_flags": None, "cisin": None,
    }
    row.update(extra)
    return row


class SectorAffinityTests(unittest.TestCase):
    def calculate(self, rows, registry=None, **kwargs):
        engine = SectorAffinityAccumulator(
            registry if registry is not None else {
                "A": _sector("G1", "Group 1", "S1", "CAT1"),
                "B": _sector("G2", "Group 2", "S2", "CAT2"),
            },
            categories={"CAT1": "Category 1", "CAT2": "Category 2"},
            requested_pair=("share_id", 10), **kwargs,
        )
        for row in rows:
            engine.add(row)
        return engine.result()

    def test_current_allocation_keeps_official_and_dashboard_dimensions_separate(self):
        result = self.calculate([
            _row("2026-01-01", "A", 60, 10),
            _row("2026-01-01", "B", 40, 10),
        ])
        groups = {x["code"]: x for x in result["official_groups"]}
        self.assertEqual(groups["G1"]["current_weight_pct"], 60.0)
        self.assertEqual(groups["G2"]["current_weight_pct"], 40.0)
        categories = {x["code"]: x for x in result["dashboard_categories"]}
        self.assertEqual(categories["CAT1"]["current_weight_pct"], 60.0)

    def test_price_change_without_quantity_change_does_not_create_rotation(self):
        result = self.calculate([
            _row("2026-01-01", "A", 100, 10),
            _row("2026-02-01", "A", 100, 20, shares_change=0,
                 change_calculation_status="consecutive_observations"),
        ])
        group = next(x for x in result["official_groups"] if x["code"] == "G1")
        self.assertEqual(group["recent_net_change_value_rial"], 0.0)
        self.assertEqual(group["rotation_direction"], "neutral")

    def test_positive_and_negative_net_changes_remain_signed(self):
        result = self.calculate([
            _row("2026-01-01", "A", 100, 10),
            _row("2026-02-01", "A", 120, 10, shares_change=20,
                 change_calculation_status="consecutive_observations"),
            _row("2026-02-01", "B", 100, 10),
            _row("2026-03-01", "B", 80, 10, shares_change=-20,
                 change_calculation_status="consecutive_observations"),
        ])
        groups = {x["code"]: x for x in result["official_groups"]}
        self.assertEqual(groups["G1"]["recent_net_change_value_rial"], 200.0)
        self.assertEqual(groups["G2"]["recent_net_change_value_rial"], -200.0)

    def test_corporate_actions_gaps_anomalies_and_identity_conflicts_do_not_rotate(self):
        result = self.calculate([
            _row("2026-01-01", "A", 100, 10),
            _row("2026-02-01", "A", 150, 10, shares_change=50,
                 change_calculation_status="consecutive_observations",
                 corporate_action="capital_increase"),
            _row("2026-02-01", "B", 150, 10, shares_change=50,
                 change_calculation_status="spans_gap"),
            _row("2026-02-01", "C", 150, 10, shares_change=50,
                 change_calculation_status="consecutive_observations",
                 anomaly_flags="quantity_anomaly"),
            _row("2026-02-01", "D", 150, 10, shares_change=50,
                 change_calculation_status="consecutive_observations",
                 identity_conflict=True),
        ], registry={
            "A": _sector("G1"), "B": _sector("G2"),
            "C": _sector("G3"), "D": _sector("G4"),
        })
        self.assertTrue(all(x["recent_net_change_value_rial"] == 0
                            for x in result["official_groups"]))
        self.assertGreater(result["data_quality"]["rotation_excluded"], 0)

    def test_cisin_aliases_are_counted_once_and_conflicts_excluded(self):
        same = self.calculate([
            _row("2026-01-01", "A", 100, 10, cisin="ISIN-1"),
            _row("2026-01-01", "A2", 100, 10, cisin="ISIN-1"),
        ], registry={"A": _sector("G1"), "A2": _sector("G1")})
        self.assertEqual(same["current_portfolio_value_rial"], 1000.0)
        conflict = self.calculate([
            _row("2026-01-01", "A", 100, 10, cisin="ISIN-1"),
            _row("2026-01-01", "A2", 200, 10, cisin="ISIN-1"),
        ], registry={"A": _sector("G1"), "A2": _sector("G1")})
        self.assertIsNone(conflict["current_portfolio_value_rial"])
        self.assertEqual(conflict["data_quality"]["cisin_alias_conflicts"], 1)

    def test_missing_stale_and_future_prices_are_not_valued(self):
        result = self.calculate([
            _row("2026-01-01", "A", 100, None, shares_change=5,
                 change_calculation_status="consecutive_observations"),
            _row("2026-01-02", "B", 100, 10, reference_price_date="2025-10-01",
                 shares_change=-5, change_calculation_status="consecutive_observations"),
        ])
        self.assertIsNone(result["current_portfolio_value_rial"])
        self.assertEqual(result["data_quality"]["missing_price"], 1)
        self.assertEqual(result["data_quality"]["stale_or_future_price"], 1)
        self.assertEqual(result["data_quality"]["rotation_change_candidates"], 2)
        self.assertEqual(result["data_quality"]["rotation_eligible"], 0)
        self.assertEqual(result["data_quality"]["rotation_excluded"], 2)
        future = self.calculate([
            _row("2026-01-01", "A", 100, 10, reference_price_date="2026-01-02",
                 shares_change=10, change_calculation_status="consecutive_observations"),
        ])
        self.assertIsNone(future["current_portfolio_value_rial"])

    def test_historical_affinity_weights_recent_observations_more_heavily(self):
        result = self.calculate([
            _row("2025-01-01", "A", 100, 10),
            _row("2025-01-01", "B", 0, 10),
            _row("2026-01-01", "A", 0, 10),
            _row("2026-01-01", "B", 100, 10),
        ], half_life_months=3)
        groups = {x["code"]: x for x in result["official_groups"]}
        self.assertGreater(groups["G2"]["historical_affinity_pct"],
                           groups["G1"]["historical_affinity_pct"])

    def test_unmapped_official_group_stays_out_of_dashboard_category_but_in_total(self):
        result = self.calculate([
            _row("2026-01-01", "A", 50, 10),
            _row("2026-01-01", "B", 50, 10),
        ], registry={
            "A": _sector("G1", category="CAT1"),
            "B": _sector("G2", category=None),
        })
        groups = {x["code"]: x for x in result["official_groups"]}
        self.assertEqual(groups["G2"]["current_weight_pct"], 50.0)
        self.assertEqual(result["dashboard_categories"][0]["current_weight_pct"], 50.0)
        self.assertEqual(result["data_quality"]["sector_classification_coverage_pct"], 100.0)

    def test_dashboard_category_aggregates_recent_rotation(self):
        result = self.calculate([
            _row("2026-01-01", "A", 100, 10),
            _row("2026-02-01", "A", 120, 10, shares_change=20,
                 change_calculation_status="consecutive_observations"),
        ], registry={
            "A": _sector("G1", "Group 1", "S1", "CAT1"),
        }, categories={"CAT1": "Category 1"})
        category = result["dashboard_categories"][0]
        self.assertEqual(category["recent_net_change_value_rial"], 200.0)
        self.assertEqual(category["rotation_direction"], "increasing")

    def test_missing_registry_is_explicit(self):
        result = self.calculate([_row("2026-01-01", "A", 10, 10)], registry={})
        self.assertEqual(result["status"], "registry_missing")
        self.assertIn("instrument_sector_registry_missing", result["warnings"])

    def test_stream_order_and_parameter_bounds_are_checked(self):
        engine = SectorAffinityAccumulator({"A": _sector()})
        engine.add(_row("2026-02-01", "A", 10, 10))
        with self.assertRaises(ValueError):
            engine.add(_row("2026-01-01", "A", 10, 10))
        with self.assertRaises(ValueError):
            SectorAffinityAccumulator({}, half_life_months=0.5)


if __name__ == "__main__":
    unittest.main()
