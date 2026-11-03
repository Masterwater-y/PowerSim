#!/usr/bin/env python3
"""Focused contracts for the TeaLeaf directed experiment matrix."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

from run_tealeaf_cpi_diagnostics import (  # noqa: E402
    BASELINE_Q_SEQUENCE,
    COMMON_OVERRIDES,
    VARIANTS,
    adjacent_q_changes,
    compare_frontier_samples,
    frontier_counters,
    translation_normalized_frontier_sample,
    variant_overrides,
)
from run_uarch_fastsim import EFFECTIVE_PATHS, config_override  # noqa: E402


class TeaLeafCpiDiagnosticsTest(unittest.TestCase):
    def test_every_experiment_override_is_effective_output_validated(self):
        keys = set(COMMON_OVERRIDES)
        for variant in VARIANTS.values():
            keys.update(variant["overrides"])
        self.assertEqual(keys - set(EFFECTIVE_PATHS), set())

    def test_single_factor_variants_preserve_common_baseline(self):
        baseline = variant_overrides("baseline_q1024")
        expected_differences = {
            "q0128": {"sim.interval_max_cycles"},
            "q0256": {"sim.interval_max_cycles"},
            "q0512": {"sim.interval_max_cycles"},
            "q2048": {"sim.interval_max_cycles"},
            "legacy_causal_timing_on": {"sim.interval_causal_timing"},
            "store_post_commit_on": {"core.store_post_commit_request"},
            "ddr_act_on": {
                "dram.t_ras",
                "dram.t_rrd",
                "dram.t_rrd_l",
                "dram.t_xaw",
                "dram.activation_limit",
            },
            "ddr_column_rank_on": {
                "dram.t_rtp",
                "dram.t_ccd_l",
                "dram.t_cs",
            },
            "ddr_combined": {
                "dram.t_ras",
                "dram.t_rtp",
                "dram.t_rrd",
                "dram.t_rrd_l",
                "dram.t_xaw",
                "dram.activation_limit",
                "dram.t_ccd_l",
                "dram.t_cs",
            },
        }
        for name, expected in expected_differences.items():
            current = variant_overrides(name)
            changed = {
                key for key in baseline if baseline[key] != current[key]
            }
            self.assertEqual(changed, expected, name)

    def test_adjacent_q_changes_attributes_cycle_delta(self):
        rows = []
        for index, variant in enumerate(BASELINE_Q_SEQUENCE):
            for cores in (4, 8):
                response_cycles = 500 - 10 * index
                rows.append(
                    {
                        "variant": variant,
                        "cores": cores,
                        "interval_max_cycles": 128 << index,
                        "fastsim_uop_cpi": 1.0 + index / 100.0,
                        "sum_core_cycles": 1000 + response_cycles,
                        "response_critical_total_cycles": response_cycles,
                        "non_response_lower_bound_cycles": 1000,
                    }
                )
        changes = adjacent_q_changes(rows)
        self.assertEqual(len(changes), 8)
        self.assertEqual(changes[0]["coarse_variant"], "q0128")
        self.assertEqual(changes[-1]["fine_variant"], "q2048")
        self.assertTrue(
            all(
                item["cycle_delta_explained_by_response_critical"]
                for item in changes
            )
        )

    def test_config_override_parser_preserves_bool_number_and_size(self):
        self.assertEqual(config_override("flag=true"), ("flag", True))
        self.assertEqual(config_override("count=0x20"), ("count", 32))
        self.assertEqual(config_override("ratio=1.5"), ("ratio", 1.5))
        self.assertEqual(config_override("cache=32KiB"), ("cache", "32KiB"))

    def test_runtime_diagnostics_come_from_causal_frontier(self):
        counters = frontier_counters(
            {
                "totals": {
                    "causal_timing_candidate_epochs": 999,
                    "store_post_commit_request_events": 999,
                },
                "causal_frontier": {
                    "causal_timing_candidate_epochs": 36,
                    "causal_timing_stable_epochs": 14,
                    "causal_timing_fallback_epochs": 22,
                    "store_post_commit_request_events": 985415,
                },
            }
        )
        self.assertEqual(counters["causal_timing_candidate_epochs"], 36)
        self.assertEqual(counters["causal_timing_stable_epochs"], 14)
        self.assertEqual(counters["causal_timing_fallback_epochs"], 22)
        self.assertEqual(counters["store_post_commit_request_events"], 985415)

    def test_frontier_comparison_finds_first_shared_sequence_mismatch(self):
        variants = ("q0", "q1", "q2")
        samples = {
            "q0": [
                {"sequence": 3, "actual_retire_cycle": 10, "rob_digest": 1},
                {"sequence": 7, "actual_retire_cycle": 20, "rob_digest": 2},
            ],
            "q1": [
                {"sequence": 3, "actual_retire_cycle": 10, "rob_digest": 1},
                {"sequence": 7, "actual_retire_cycle": 21, "rob_digest": 2},
            ],
            "q2": [
                {"sequence": 3, "actual_retire_cycle": 10, "rob_digest": 1},
                {"sequence": 7, "actual_retire_cycle": 22, "rob_digest": 3},
            ],
        }
        comparison = compare_frontier_samples(samples, variants)
        self.assertEqual(comparison["common_sample_count"], 2)
        self.assertFalse(comparison["all_common_samples_equal"])
        self.assertEqual(comparison["earliest_mismatch"]["sequence"], 7)
        self.assertEqual(
            comparison["earliest_mismatch"]["mismatched_fields"],
            ["actual_retire_cycle", "rob_digest"],
        )

    def test_frontier_translation_preserves_zero_sentinel(self):
        normalized = translation_normalized_frontier_sample(
            {
                "sequence": 7,
                "interval_gap_cycles": 100,
                "actual_dispatch_cycle": 123,
                "memory_response_cycle": 0,
                "selected_memory_response_cycle": 137,
                "checkpoint_begin_sequence": 4,
                "memory_events": [{"ordinal": 11}],
                "rob_digest": 42,
            }
        )
        self.assertEqual(normalized["actual_dispatch_cycle"], 23)
        self.assertEqual(normalized["memory_response_cycle"], 0)
        self.assertEqual(normalized["selected_memory_response_cycle"], 37)
        self.assertEqual(normalized["rob_digest"], 42)
        self.assertNotIn("interval_gap_cycles", normalized)
        self.assertNotIn("checkpoint_begin_sequence", normalized)
        self.assertNotIn("memory_events", normalized)


if __name__ == "__main__":
    unittest.main()
