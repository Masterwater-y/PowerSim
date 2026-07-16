"""Deployment-side scheduler/model bridge invariants."""
from __future__ import annotations

import math
import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

try:
    import torch

    from tcsim.chunker.functional_features import (
        CHUNK_SUMMARY_NAMES,
        DYNAMIC_FIELD_NAMES,
        FIELD_NAMES,
        RELATION_FEATURE_NAMES,
        RESOURCE_KEY_NAMES,
        UARCH_FEATURE_NAMES,
    )
    from tcsim.inference.deployment import (
        ContextPrediction,
        DeploymentRunner,
        ModelContextPredictor,
        aggregate_trace_reports,
    )
    from tcsim.model.tcsim_model import TCSimModel

    HAS_TORCH = True
except Exception:
    HAS_TORCH = False


def _chunk(core: int, chunk_id: int, delta: float, branch_miss: int) -> dict:
    K = 4
    fields = [[0] * len(FIELD_NAMES) for _ in range(K)]
    return {
        "trace_id": "fake",
        "core_id": core,
        "chunk_id": chunk_id,
        "n_uops": K,
        "n_load": 1,
        "n_store": 0,
        "n_atomic": 0,
        "n_branch": 10,
        "n_cond_branch": 10,
        "n_branch_miss": branch_miss,
        "n_int": 3,
        "n_fp": 0,
        "n_simd": 0,
        "n_serialize": 0,
        "has_atomic": False,
        "has_serialize": False,
        "n_branch_opportunities": 10,
        "per_uop_fields": fields,
        "valid_uop_mask": [1] * K,
        "chunk_summary": [0.0] * len(CHUNK_SUMMARY_NAMES),
        "per_uop_lines": [100 + core, -1, -1, -1],
        "per_uop_access": [1, 0, 0, 0],
        "per_uop_resource_keys": [
            [100 + core, 0, 0, 0, 0, 0, 0, 0],
            *([[-1] * len(RESOURCE_KEY_NAMES)] * (K - 1)),
        ],
        "read_lines": [100 + core],
        "write_lines": [],
        "uarch_features": [0.0] * len(UARCH_FEATURE_NAMES),
        "valid_label": True,
        "true_delta_cycles": delta,
        "true_cpi": delta / K,
    }


class _FakeTrace:
    trace_id = "fake"
    workload = "fake_workload"
    seed = 1
    K = 4
    uarch_hash = "fake-uarch"
    uarch_features = [0.0] * 28
    core_ids = [0, 1]
    core_counts = {0: 2, 1: 1}
    total_chunks = 3

    def __init__(self):
        self.chunks = {
            (0, 0): _chunk(0, 0, 100.0, 2),
            (0, 1): _chunk(0, 1, 100.0, 2),
            (1, 0): _chunk(1, 0, 1000.0, 5),
        }

    def get_chunk(self, core_id: int, chunk_id: int):
        return self.chunks[(core_id, chunk_id)]


class _ChangingResidentPredictor:
    """Return a different value when a resident row is recomputed."""

    def __init__(self):
        self.n_forwards = 0

    def predict(self, _trace, chunks):
        self.n_forwards += 1
        delta = []
        branch = []
        for chunk in chunks:
            key = (chunk["core_id"], chunk["chunk_id"])
            if key == (1, 0):
                # First exposure must be latched; 9000/0.9 on the second model
                # call must not overwrite the resident state.
                delta.append(1000.0 if self.n_forwards == 1 else 9000.0)
                branch.append(0.5 if self.n_forwards == 1 else 0.9)
            else:
                delta.append(100.0)
                branch.append(0.2)
        return ContextPrediction(delta, branch)


@unittest.skipUnless(HAS_TORCH, "torch unavailable")
class TestDeploymentInference(unittest.TestCase):
    def test_aggregate_uses_one_branch_relative_error(self):
        aggregate = aggregate_trace_reports([
            {
                "pred_branch_misses": 12.0,
                "true_branch_misses": 10.0,
                "retired_branches": 100,
            }
        ])["aggregate"]
        self.assertAlmostEqual(aggregate["branch_miss_relative_error"], 0.2)
        self.assertEqual(
            aggregate["branch_miss_rate_relative_error"],
            aggregate["branch_miss_relative_error"],
        )
        self.assertEqual(
            aggregate["global_branch_miss_count_error"],
            aggregate["branch_miss_relative_error"],
        )

        zero_true = aggregate_trace_reports([
            {
                "pred_branch_misses": 1.0,
                "true_branch_misses": 0.0,
                "retired_branches": 10,
            }
        ])["aggregate"]
        self.assertTrue(math.isnan(zero_true["branch_miss_relative_error"]))

    def test_resident_outputs_are_latched_and_committed_exactly_once(self):
        predictor = _ChangingResidentPredictor()
        result = DeploymentRunner(
            epsilon=0.0,
            max_resident_exposure=16,
            force_sync_fast=False,
        ).run(_FakeTrace(), predictor)
        summary = result.summary
        self.assertEqual(summary["exact_once_latched"], 3)
        self.assertEqual(summary["exact_once_committed"], 3)
        self.assertEqual(summary["n_model_forwards"], 2)
        self.assertEqual(summary["n_steps"], 3)
        self.assertEqual(summary["n_resident_events"], 2)
        # 100 + 100 + the originally latched 1000, not the recomputed 9000.
        self.assertEqual(summary["pred_cycle_sum"], 1200.0)
        # Branch counts also latch once: 2 + 2 + 5.
        self.assertEqual(summary["pred_branch_misses"], 9.0)
        self.assertEqual(summary["true_branch_misses"], 9.0)
        self.assertEqual(summary["roi_uops"], 12)
        self.assertEqual(summary["pred_roi_cpi"], 100.0)
        self.assertEqual(summary["true_roi_cpi"], 100.0)
        self.assertEqual(summary["roi_cpi_error"], 0.0)
        self.assertEqual(summary["window_cpi_mape_mean"], 0.0)
        self.assertEqual(summary["core_roi_cpi_mape_mean"], 0.0)
        self.assertEqual(summary["retired_branches"], 30)
        self.assertEqual(summary["pred_branch_miss_rate"], 0.3)
        self.assertEqual(summary["true_branch_miss_rate"], 0.3)
        self.assertEqual(summary["branch_miss_relative_error"], 0.0)
        self.assertEqual(
            summary["branch_miss_count_error"],
            summary["branch_miss_relative_error"],
        )
        self.assertEqual(
            summary["branch_miss_rate_relative_error"],
            summary["branch_miss_relative_error"],
        )
        self.assertEqual(len(summary["per_core"]), 2)

    def test_forward_from_static_is_the_same_model_path(self):
        torch.manual_seed(3)
        model = TCSimModel(
            d_field=4,
            d_static=16,
            d_dyn=16,
            n_heads=4,
            n_layers=1,
            ffn_dim=32,
            dropout=0.0,
            max_K=8,
        ).eval()
        chunks = [_chunk(0, 0, 100.0, 2), _chunk(1, 0, 120.0, 3)]
        batch = {
            "per_uop_fields": torch.tensor(
                [chunk["per_uop_fields"] for chunk in chunks], dtype=torch.long
            ),
            "valid_uop_mask": torch.ones(2, 4, dtype=torch.bool),
            "dynamic_uop_fields": torch.zeros(
                2, 4, len(DYNAMIC_FIELD_NAMES), dtype=torch.long,
            ),
            "chunk_summary": torch.zeros(2, len(CHUNK_SUMMARY_NAMES)),
            "relation_features": torch.zeros(2, len(RELATION_FEATURE_NAMES)),
            "uarch_features": torch.zeros(2, len(UARCH_FEATURE_NAMES)),
            "n_uops": torch.full((2,), 4.0),
            "sample_ptr": torch.tensor([0, 2]),
        }
        with torch.inference_mode():
            direct = model(batch)
            tokens = model.static_enc.encode_tokens(batch["per_uop_fields"])
            bridged = model.forward_from_static(batch, tokens)
        torch.testing.assert_close(direct["log_cpi"], bridged["log_cpi"])
        torch.testing.assert_close(
            direct["pred_branch_miss_prob"], bridged["pred_branch_miss_prob"]
        )

    def test_static_cache_excludes_active_context(self):
        torch.manual_seed(5)
        model = TCSimModel(
            d_field=4,
            d_static=16,
            d_dyn=16,
            n_heads=4,
            n_layers=1,
            ffn_dim=32,
            dropout=0.0,
            max_K=8,
        ).eval()
        predictor = ModelContextPredictor(
            model,
            device="cpu",
            checkpoint_id="test",
            amp_dtype="fp32",
            static_cache_entries=8,
        )
        trace = _FakeTrace()
        chunks = [trace.get_chunk(0, 0), trace.get_chunk(1, 0)]
        first = predictor.predict(trace, chunks)
        second = predictor.predict(trace, chunks)
        predictor.predict(trace, chunks[:1])
        self.assertEqual(predictor.static_cache.misses, 2)
        self.assertEqual(predictor.static_cache.hits, 3)
        self.assertEqual(first.delta_cycles, second.delta_cycles)
        self.assertEqual(first.branch_miss_prob, second.branch_miss_prob)


if __name__ == "__main__":
    unittest.main()
