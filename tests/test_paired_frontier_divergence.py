#!/usr/bin/env python3
"""Focused tests for paired response-frontier divergence analysis."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

from analyze_paired_frontier_divergence import (  # noqa: E402
    analyze_core,
    normalize_sample,
)


def sample(sequence: int, gap: int, retire: int, **updates):
    value = {
        "sequence": sequence,
        "interval_gap_cycles": gap,
        "pc": 0x1000 + sequence,
        "has_load": 0,
        "has_store": 0,
        "branch": 0,
        "branch_miss": 0,
        "producer_dists": [0, 0, 0, 0, 0],
        "memory_events": [],
        "actual_fetch_cycle": retire - 7,
        "actual_rename_cycle": retire - 6,
        "actual_dispatch_cycle": retire - 5,
        "actual_issue_cycle": retire - 4,
        "actual_completion_cycle": retire - 2,
        "memory_response_cycle": 0,
        "actual_retire_cycle": retire,
        "commit_cycle": retire,
        "dispatch_cause": 7,
        "completion_cause": 7,
        "retire_cause": 7,
        "incoming_sq_release_valid": 0,
        "selected_memory_valid": 0,
    }
    value.update(updates)
    return value


class PairedFrontierDivergenceTest(unittest.TestCase):
    def test_translation_only_shift_is_semantically_equal(self):
        baseline = sample(0, 90, 100)
        candidate = sample(0, 100, 110)
        self.assertEqual(
            normalize_sample(baseline), normalize_sample(candidate)
        )

    def test_lead_to_sq_lag_reports_owner_and_gate(self):
        baseline = {
            0: sample(0, 90, 100),
            1: sample(1, 90, 120),
            2: sample(
                2,
                90,
                130,
                has_store=1,
                dispatch_cause=6,
                completion_cause=6,
                retire_cause=6,
                incoming_sq_release_valid=1,
                incoming_sq_release_sequence=0,
                incoming_sq_release_cycle=125,
                incoming_sq_release_displacement_cycles=0,
                sq_head_release_cycle=150,
            ),
        }
        candidate = {
            0: sample(0, 100, 110),
            1: sample(1, 90, 115),
            2: sample(
                2,
                90,
                135,
                has_store=1,
                dispatch_cause=6,
                completion_cause=6,
                retire_cause=6,
                incoming_sq_release_valid=1,
                incoming_sq_release_sequence=0,
                incoming_sq_release_cycle=130,
                incoming_sq_release_displacement_cycles=0,
                sq_head_release_cycle=155,
            ),
        }
        result = analyze_core(0, baseline, candidate)
        self.assertEqual(
            result["first_semantic_mismatch"]["sequence"], 1
        )
        reversal = result["first_observed_lead_to_lag"]
        self.assertEqual(reversal["sequence"], 2)
        self.assertEqual(
            reversal["raw_stage_delta_cycles"]["actual_retire_cycle"], 5
        )
        self.assertIn("sq_capacity", reversal["observed_components"])
        self.assertEqual(
            reversal["candidate"]["incoming_sq_owner"]["sequence"], 0
        )


if __name__ == "__main__":
    unittest.main()
