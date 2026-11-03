#!/usr/bin/env python3
"""Focused tests for bounded branch-recovery witness analysis."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from analyze_branch_recovery_witness import build_report, replay  # noqa: E402


def sample(sequence: int, **updates: int) -> dict:
    value = {
        "sequence": sequence,
        "pc": 0x1000 + 4 * sequence,
        "branch": 0,
        "branch_miss": 0,
        "producer_dists": [0, 0, 0, 0, 0],
        "actual_fetch_cycle": sequence,
        "actual_dispatch_cycle": sequence + 1,
        "actual_issue_cycle": sequence + 2,
        "actual_completion_cycle": sequence + 3,
        "actual_retire_cycle": sequence + 3,
        "base_completion_cycle": sequence + 3,
        "has_load": 0,
        "has_store": 0,
    }
    value.update(updates)
    return value


class BranchRecoveryWitnessTest(unittest.TestCase):
    def test_zero_change_is_identity(self):
        samples = [sample(index) for index in range(4)]
        rows = replay(samples, set(), commit_width=4, execute_to_commit=0)
        self.assertTrue(
            all(
                row[field] == 0
                for row in rows
                for field in (
                    "fetch_delta",
                    "issue_delta",
                    "completion_delta",
                    "retire_delta",
                )
            )
        )

    def test_delayed_branch_gates_correct_path_and_consumer(self):
        samples = [
            sample(
                0,
                branch=1,
                branch_miss=1,
                actual_completion_cycle=20,
                actual_retire_cycle=20,
                base_completion_cycle=3,
            ),
            sample(
                1,
                actual_fetch_cycle=6,
                actual_dispatch_cycle=7,
                actual_issue_cycle=8,
                actual_completion_cycle=12,
                actual_retire_cycle=20,
                base_completion_cycle=12,
                has_load=1,
            ),
            sample(
                2,
                producer_dists=[1, 0, 0, 0, 0],
                actual_fetch_cycle=7,
                actual_dispatch_cycle=8,
                actual_issue_cycle=12,
                actual_completion_cycle=13,
                actual_retire_cycle=20,
                base_completion_cycle=13,
            ),
        ]
        document = {
            "configuration": {
                "interval_max_cycles": 1024,
                "commit_width": 4,
                "execute_to_commit": 0,
            },
            "cores": [{"response_frontier_audit": samples}],
        }
        report = build_report(document, ROOT / "CMakeLists.txt", 0)
        event = report["events"][0]
        self.assertEqual(report["necessary_order_violations"], 1)
        self.assertEqual(event["fetch_before_resolution_cycles"], 14)
        self.assertEqual(event["counterfactual_correct_path_fetch_cycle"], 20)
        self.assertEqual(event["counterfactual_correct_path_issue_cycle"], 22)
        self.assertEqual(event["first_direct_consumer"]["sequence"], 2)
        self.assertGreater(event["counterfactual_max_retire_delta_cycles"], 0)

    def test_sparse_branch_pairs_only_report_observed_violation(self):
        samples = [
            sample(
                10,
                branch=1,
                branch_miss=1,
                actual_completion_cycle=30,
                actual_retire_cycle=30,
                base_completion_cycle=13,
            ),
            sample(
                11,
                actual_fetch_cycle=20,
                actual_dispatch_cycle=21,
                actual_issue_cycle=22,
                actual_completion_cycle=23,
                actual_retire_cycle=30,
                base_completion_cycle=23,
            ),
            sample(100),
            sample(
                200,
                branch=1,
                branch_miss=1,
                actual_completion_cycle=240,
                actual_retire_cycle=240,
                base_completion_cycle=203,
            ),
            sample(
                201,
                actual_fetch_cycle=241,
                actual_dispatch_cycle=242,
                actual_issue_cycle=243,
                actual_completion_cycle=244,
                actual_retire_cycle=244,
                base_completion_cycle=244,
            ),
        ]
        document = {
            "configuration": {
                "interval_max_cycles": 1024,
                "commit_width": 4,
                "execute_to_commit": 0,
            },
            "cores": [{"response_frontier_audit": samples}],
        }
        report = build_report(document, ROOT / "CMakeLists.txt", 0)
        self.assertEqual(report["audit_layout"], "sparse_branch_pairs")
        self.assertEqual(report["branch_misses"], 2)
        self.assertEqual(report["necessary_order_violations"], 1)
        self.assertEqual(report["unpaired_branch_misses"], [])
        self.assertIsNone(report["zero_change_replay_identity"])
        self.assertIsNone(
            report["combined_fixed_service_max_retire_delta_cycles"]
        )
        self.assertIsNone(
            report["events"][0]["counterfactual_max_retire_delta_cycles"]
        )


if __name__ == "__main__":
    unittest.main()
