#!/usr/bin/env python3
"""Focused tests for the branch-miss oracle scope audit."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from audit_branch_miss_oracle import (  # noqa: E402
    build_report,
    indexed_stat,
    percentile,
)


class BranchMissOracleAuditTest(unittest.TestCase):
    def test_indexed_stat_reads_switch_core_totals(self):
        text = "\n".join(
            (
                "board.processor.switch0.core.branchPred."
                "mispredicted_0::total 17 # count",
                "board.processor.switch3.core.branchPred."
                "mispredicted_0::total 29 # count",
            )
        )
        self.assertEqual(
            indexed_stat(text, "mispredicted_0::total"), {0: 17, 3: 29}
        )

    def test_type7_percentile(self):
        self.assertAlmostEqual(percentile([0.0, 10.0], 0.99), 9.9)

    def test_report_excludes_scope_skew_and_compares_both_oracles(self):
        rows = [
            {
                "case_id": "aligned",
                "core": 0,
                "trace_branches": 1000,
                "fastsim_branches": 1000,
                "gem5_bpred_committed": 1000,
                "branch_population_skew": 0,
                "branch_population_skew_ratio": 0.0,
                "legacy_dyninst_misses": 50,
                "gem5_bpred_misses": 100,
                "fastsim_misses": 101,
                "legacy_label_gap": 50,
            },
            {
                "case_id": "post-target",
                "core": 1,
                "trace_branches": 1000,
                "fastsim_branches": 1000,
                "gem5_bpred_committed": 1200,
                "branch_population_skew": 200,
                "branch_population_skew_ratio": 0.2,
                "legacy_dyninst_misses": 20,
                "gem5_bpred_misses": 30,
                "fastsim_misses": 25,
                "legacy_label_gap": 10,
            },
        ]
        report = build_report(rows, 0.001)
        self.assertEqual(report["scope_aligned_rows"], 1)
        self.assertAlmostEqual(
            report["legacy_dyninst_reference"]["mape_percent"], 102.0
        )
        self.assertAlmostEqual(
            report["gem5_bpred_reference"]["mape_percent"], 1.0
        )
        self.assertEqual(
            report["legacy_label_gap"]["missing_from_legacy_total"], 50
        )


if __name__ == "__main__":
    unittest.main()
