#!/usr/bin/env python3
"""Regression tests for uarch-generalization PMU event definitions."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

from evaluate_uarch_generalization import (  # noqa: E402
    PMUS,
    cha_breakdown,
    cha_summary,
)


def stats() -> dict:
    return {
        "cha": [
            {
                "requests": 17,
                "llc_hits": 5,
                "llc_misses": 7,
                "upgrades": 3,
                "remote_supplies": 1,
                "llc_merged_misses": 1,
                "llc_outcomes_conserved": True,
            },
            {
                "requests": 11,
                "llc_hits": 4,
                "llc_misses": 4,
                "upgrades": 2,
                "remote_supplies": 1,
                "llc_merged_misses": 0,
                "llc_outcomes_conserved": True,
            },
        ]
    }


class UarchGeneralizationMetricsTest(unittest.TestCase):
    def test_cha_demand_excludes_upgrades_but_includes_remote_outcomes(self):
        breakdown = cha_breakdown(stats())
        self.assertEqual(breakdown["total_requests"], 28)
        self.assertEqual(breakdown["permission_upgrades"], 5)
        self.assertEqual(breakdown["demand_lookups"], 23)
        self.assertEqual(breakdown["remote_supplies"], 2)
        self.assertTrue(breakdown["request_class_conserved"])
        self.assertTrue(breakdown["outcomes_conserved"])
        predictor, _, scope = PMUS["cha_llc_lookups"]
        self.assertEqual(predictor(stats()), 23.0)
        self.assertEqual(scope, "strict")

    def test_o3_capacity_events_are_explicitly_proxy_scope(self):
        for name in ("iq_full_events", "rob_full_events", "lsq_full_events"):
            self.assertEqual(PMUS[name][2], "proxy")

    def test_cha_aggregate_preserves_request_class_identity(self):
        breakdown = cha_breakdown(stats())
        row = {f"cha_{key}_fastsim": value for key, value in breakdown.items()}
        aggregate = cha_summary([row, row])
        self.assertEqual(aggregate["total_requests"], 56)
        self.assertEqual(aggregate["demand_lookups"], 46)
        self.assertEqual(aggregate["permission_upgrades"], 10)
        self.assertTrue(aggregate["request_class_conserved"])
        self.assertTrue(aggregate["outcomes_conserved"])

    def test_invalid_upgrade_population_is_rejected(self):
        invalid = stats()
        invalid["cha"][0]["upgrades"] = 40
        with self.assertRaisesRegex(ValueError, "upgrades exceed"):
            cha_breakdown(invalid)


if __name__ == "__main__":
    unittest.main()
