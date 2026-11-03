#!/usr/bin/env python3
"""Focused tests for cross-workload component classification."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

from analyze_cross_workload_component_matrix import (  # noqa: E402
    add_native_comparison_sample,
    bound_class,
    component_for_cause,
    distribution,
    fastsim_shared_path_name,
    hierarchy_path_semantically_matches,
    issue_gate_name,
    issue_owner_chain,
    native_hierarchy_comparable,
    native_comparison_bucket,
    render_native_comparison_rows,
    validate_identity,
)


class CrossWorkloadComponentMatrixTest(unittest.TestCase):
    def test_memory_response_uses_path_identity(self):
        dram = {
            "selected_memory_valid": 1,
            "selected_memory_path": 5,
            "selected_memory_unique_dram_request": 1,
        }
        cache = {
            "selected_memory_valid": 1,
            "selected_memory_path": 0,
            "selected_memory_unique_dram_request": 0,
        }
        self.assertEqual(
            component_for_cause("memory_response", dram), "dram_service"
        )
        self.assertEqual(
            component_for_cause("memory_response", cache),
            "cache_coherence_response",
        )

    def test_distribution_preserves_signs(self):
        result = distribution([-2, 0, 4])
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["negative"], 1)
        self.assertEqual(result["zero"], 1)
        self.assertEqual(result["positive"], 1)
        self.assertEqual(result["p50"], 0)

    def test_residual_bound_classification(self):
        self.assertEqual(bound_class(1, 3), "proven_positive")
        self.assertEqual(bound_class(-3, -1), "proven_negative")
        self.assertEqual(bound_class(-1, 2), "ambiguous")
        with self.assertRaisesRegex(ValueError, "invalid residual bound"):
            bound_class(2, 1)

    def test_issue_gate_taxonomy_is_stable(self):
        self.assertEqual(issue_gate_name(6), "register_producer")
        self.assertEqual(issue_gate_name(7), "store_set_producer")
        self.assertEqual(issue_gate_name(9), "dtlb_pending_fill")
        self.assertEqual(issue_gate_name(99), "unknown_99")

    def test_native_hierarchy_comparison_keeps_path_and_timing_separate(self):
        self.assertEqual(fastsim_shared_path_name(5), "memory")
        self.assertTrue(hierarchy_path_semantically_matches(
            "memory", "ruby_memory_read"
        ))
        self.assertTrue(hierarchy_path_semantically_matches(
            "local_private_cache", "private_l2_hit"
        ))
        self.assertFalse(hierarchy_path_semantically_matches(
            "memory", "l1d_hit"
        ))

    def test_native_comparison_rejects_multi_event_uop(self):
        sample = {
            "selected_memory_valid": 1,
            "selected_memory_instruction_fetch": 0,
            "memory_events": [
                {"instruction_fetch": 0},
                {"instruction_fetch": 0},
            ],
        }
        pair = {"memory_event_scope": "data-hierarchy"}
        native = {"line_requests": 1}
        self.assertFalse(native_hierarchy_comparable(sample, pair, native))
        sample["memory_events"].pop()
        self.assertTrue(native_hierarchy_comparable(sample, pair, native))

    def test_native_timing_bucket_can_be_rendered_per_event_class(self):
        bucket = native_comparison_bucket()
        has_timing = add_native_comparison_sample(
            bucket,
            semantic_match=False,
            tail=-12.0,
            native={
                "response_timestamps_available": True,
                "timing_cycles": {
                    "issue_to_first_admission": 1.0,
                    "first_admission_to_last_response": 40.0,
                    "last_response_to_commit": 3.0,
                },
            },
            fastsim_latency=2.0,
        )
        rows = render_native_comparison_rows({
            ("local_private_cache", "sequencer_coalesced"): bucket,
        })
        self.assertTrue(has_timing)
        self.assertEqual(rows[0]["samples"], 1)
        self.assertEqual(rows[0]["semantic_mismatches"], 1)
        self.assertEqual(
            rows[0]["fastsim_latency_minus_gem5_response_cycles"]["mean"],
            -38.0,
        )

    def test_dense_issue_owner_chain_stops_at_response_root(self):
        samples = {
            9: {
                "sequence": 9,
                "pc": 0x1000,
                "issue_gate_kind": 6,
                "issue_gate_extra_cycles": 20,
                "issue_gate_owner_valid": 1,
                "issue_gate_owner_sequence": 4,
                "completion_cause": 7,
                "retire_cause": 7,
            },
            4: {
                "sequence": 4,
                "pc": 0x2000,
                "issue_gate_kind": 0,
                "issue_gate_extra_cycles": 0,
                "issue_gate_owner_valid": 0,
                "completion_cause": 12,
                "retire_cause": 12,
                "has_load": 1,
                "selected_memory_valid": 1,
                "selected_memory_path": 5,
                "selected_memory_latency_cycles": 247,
                "selected_memory_exposed_cycles": 243,
            },
        }
        chain = issue_owner_chain(9, samples)
        self.assertEqual([node["sequence"] for node in chain["nodes"]], [9, 4])
        self.assertEqual(chain["nodes"][-1]["completion_cause"], "memory_response")
        self.assertEqual(chain["nodes"][-1]["selected_memory_path"], 5)
        self.assertEqual(chain["stop"], "no_issue_owner")

    def test_mmio_is_the_only_load_store_identity_exception(self):
        sample = {
            "sequence": 4,
            "pc": 0x1000,
            "has_load": 0,
            "has_store": 0,
            "producer_dists": [1, 0, 0, 0, 0],
        }
        pair = {
            "record_ordinal": 4,
            "pc": 0x1000,
            "is_load": True,
            "is_store": False,
            "producer_dists": [1, 0, 0, 0, 0],
            "memory_event_scope": "mmio-escape-no-data-event",
        }
        self.assertTrue(validate_identity("case", 0, sample, pair))
        pair["memory_event_scope"] = "data-hierarchy"
        with self.assertRaisesRegex(ValueError, "load/store identity"):
            validate_identity("case", 0, sample, pair)


if __name__ == "__main__":
    unittest.main()
