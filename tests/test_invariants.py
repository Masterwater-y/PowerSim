"""Unit tests for TCSim MVP invariants (plan §1.2).

Run with:
    python -m unittest tests.test_invariants
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
import glob
import json

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from tcsim.chunker.fixed_chunk import (
    ALIGNED_RECORD_COLUMNS,
    build_chunks_from_records,
    compute_chunk_labels,
    load_labels,
    build_trace,
)
from tcsim.chunker.functional_features import predictor_hash
from tcsim.dataset.synth import synth_default
from tcsim.dataset.rollout_builder import build_and_dump_trace
from tcsim.scheduler.epsilon_resident import EpsilonResidentScheduler

try:
    import torch
    from tcsim.dataset.torch_dataset import TCSimSampleDataset, collate_variable_active
    from tcsim.model.tcsim_model import FunctionalInteractionBlock, TCSimModel
    from tcsim.train.losses import compute_losses
    HAS_TORCH = True
except Exception:
    HAS_TORCH = False

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
    HAS_PYARROW = True
except Exception:
    HAS_PYARROW = False


def _make_recs(n: int, core_id: int = 0):
    return [
        {"core_id": core_id, "micro_seq": i + 1, "seq_num": i + 1, "op_class": 1,
         "is_load": 0, "is_store": 0, "is_atomic": 0, "is_branch": 0,
         "is_branch_cond": 0, "is_branch_indirect": 0, "is_call": 0,
         "is_return": 0, "branch_taken": 0, "branch_target": 0,
         "branch_next_pc": 0, "branch_history": 0,
         "is_int": 1, "is_fp": 0, "is_simd": 0, "is_serialize": 0,
         "is_microop": 1, "is_last_microop": 1, "n_src": 1, "n_dst": 1}
        for i in range(n)
    ]


class TestChunkInvariants(unittest.TestCase):
    def test_predictor_hash_ignores_gem5_instance_paths(self):
        def profile(switch_name):
            prefix = f"board.processor.{switch_name}.core.branchPred"
            return {
                "branch_predictor": {
                    "root": {
                        "type": "BranchPredictor",
                        "children": "btb conditionalBranchPred ras",
                        "btb": f"{prefix}.btb",
                        "conditionalbranchpred": f"{prefix}.conditionalBranchPred",
                        "ras": f"{prefix}.ras",
                        "eventq_index": "0",
                    },
                    "btb": {
                        "type": "SimpleBTB",
                        "numentries": "4096",
                        "btbindexingpolicy": f"{prefix}.btb.btbIndexingPolicy",
                        "clk_domain": "board.clk_domain",
                    },
                    "btb.power_state": {
                        "type": "PowerState",
                    },
                }
            }

        hashes = {
            predictor_hash(profile(name))
            for name in ("switch", "switch0", "switch00")
        }
        self.assertEqual(len(hashes), 1)

    def test_predictor_hash_changes_with_semantic_configuration(self):
        base = {
            "branch_predictor": {
                "root": {"type": "BranchPredictor"},
                "btb": {"type": "SimpleBTB", "numentries": "4096"},
            }
        }
        changed = json.loads(json.dumps(base))
        changed["branch_predictor"]["btb"]["numentries"] = "8192"
        self.assertNotEqual(predictor_hash(base), predictor_hash(changed))

    def test_fixed_K_boundaries(self):
        recs = _make_recs(1000, core_id=0)
        chs = build_chunks_from_records("t", 0, recs, K=256)
        # 1000 / 256 = 4 full + 1 tail of 232
        self.assertEqual(len(chs), 4)  # 256 * 4 == 1024 > 1000 → so 4 chunks: 256,256,256,232
        self.assertEqual(chs[0].n_uops, 256)
        self.assertEqual(chs[-1].n_uops, 232)
        # boundaries must be by functional index only
        self.assertEqual(chs[0].uop_start, 0)
        self.assertEqual(chs[1].uop_start, 256)

    def test_pad_to_K(self):
        recs = _make_recs(50)
        chs = build_chunks_from_records("t", 0, recs, K=256, pad_opclass=127)
        self.assertEqual(len(chs), 1)
        ch = chs[0]
        self.assertEqual(len(ch.per_uop_op_class), 256)
        self.assertEqual(ch.per_uop_op_class[50], 127)  # padded
        self.assertEqual(sum(ch.valid_uop_mask), 50)
        self.assertEqual(ch.valid_uop_mask[50], 0)

    def test_chunk_labels_monotonic(self):
        recs = _make_recs(600)
        chs = build_chunks_from_records("t", 0, recs, K=256)
        labels_by_seq = {i: i * 100 for i in range(1, 601)}
        rows = compute_chunk_labels(chs, labels_by_seq, tick_per_cycle=10.0)
        prev_end = None
        for r in rows:
            self.assertIsNotNone(r["delta_cycles"])
            self.assertGreater(r["delta_cycles"], 0)
            if prev_end is not None:
                self.assertGreaterEqual(r["start_tick"], prev_end)
            prev_end = r["end_tick"]
        self.assertEqual(sum(r["delta_cycles"] for r in rows), 6000.0)
        self.assertTrue(all(r["valid_label"] for r in rows))

    @unittest.skipUnless(HAS_PYARROW, "pyarrow unavailable")
    def test_aligned_input_matches_raw_boundaries(self):
        """Aligned input is a storage substitute, not a changed data contract."""
        with tempfile.TemporaryDirectory() as tmp:
            trace_dir = synth_default(os.path.join(tmp, "raw"), n_uops=600)[0]
            raw_chunks, raw_labels = build_trace(
                trace_dir, K=64, tick_per_cycle=500.0, input_format="raw",
            )
            rec_path = next(
                path for path in glob.glob(os.path.join(trace_dir, "*.records.micro.jsonl"))
                if ".switch0.core." in path
            )
            lab_path = rec_path.replace("records.micro.jsonl", "labels.micro.jsonl")
            labels = {}
            with open(lab_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    row = json.loads(line)
                    labels[int(row.get("micro_seq") or row.get("seq_num") or 0)] = row
            rows = []
            with open(rec_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    rec = json.loads(line)
                    key = int(rec.get("micro_seq") or rec.get("seq_num") or 0)
                    lab = labels.get(key, {})
                    row = {name: rec.get(name, [] if name.startswith("producer_") else 0)
                           for name in ALIGNED_RECORD_COLUMNS}
                    row["fetch_tick"] = int(lab.get("fetch_tick", 0))
                    row["commit_tick"] = int(lab.get("commit_tick", 0))
                    row["mispredicted"] = int(lab.get("mispredicted", 0))
                    rows.append(row)
            pq.write_table(
                pa.Table.from_pylist(rows),
                rec_path.replace(".records.micro.jsonl", ".aligned.parquet"),
            )
            aligned_chunks, aligned_labels = build_trace(
                trace_dir, K=64, tick_per_cycle=500.0, input_format="aligned",
            )
            raw_chunks = [chunk for chunk in raw_chunks if chunk.core_id == 0]
            raw_labels = [row for row in raw_labels if row["core_id"] == 0]
            self.assertEqual(
                [(c.core_id, c.chunk_id, c.n_uops, c.boundary_end_seq) for c in raw_chunks],
                [(c.core_id, c.chunk_id, c.n_uops, c.boundary_end_seq) for c in aligned_chunks],
            )
            self.assertEqual(
                [(r["delta_ticks"], r["valid_label"]) for r in raw_labels],
                [(r["delta_ticks"], r["valid_label"]) for r in aligned_labels],
            )


class TestSchedulerInvariants(unittest.TestCase):
    def _mk(self, cpi_by_core):
        chunks_by_core = {}
        for c, cpi in enumerate(cpi_by_core):
            chunks_by_core[c] = build_chunks_from_records(
                "t", c, _make_recs(2048, core_id=c), K=256,
            )
        deltas = {(c, ch.chunk_id): int(cpi_by_core[c] * ch.n_uops)
                  for c, chs in chunks_by_core.items() for ch in chs}
        def pred(core_id, ch, _s, _ctx):
            return float(deltas[(core_id, ch.chunk_id)])
        return chunks_by_core, pred

    def test_exactly_once_commit(self):
        chunks_by_core, pred = self._mk([1.0, 3.0])
        sched = EpsilonResidentScheduler(
            chunks_by_core, pred, epsilon=200.0, max_forward_budget=10_000,
            trace_id="t",
        )
        samples = sched.run()
        # every chunk should have been committed exactly once
        n_chunks_total = sum(len(v) for v in chunks_by_core.values())
        self.assertEqual(sched.stats.n_commits, n_chunks_total)
        # commit keys are unique
        self.assertEqual(len(sched._committed_keys), n_chunks_total)

    def test_resident_produced(self):
        chunks_by_core, pred = self._mk([1.0, 10.0])
        sched = EpsilonResidentScheduler(
            chunks_by_core, pred, epsilon=100.0, max_forward_budget=10_000,
            trace_id="t",
        )
        _ = sched.run()
        # slow core should have produced resident events
        self.assertGreater(sched.stats.n_resident_events, 0)

    def test_epsilon_ratio(self):
        """Larger epsilon should reduce resident events."""
        chunks_by_core, pred = self._mk([1.0, 3.0])
        low = EpsilonResidentScheduler(chunks_by_core, pred, epsilon=10.0, max_forward_budget=10_000, trace_id="t")
        _ = low.run()
        chunks_by_core2, pred2 = self._mk([1.0, 3.0])
        high = EpsilonResidentScheduler(chunks_by_core2, pred2, epsilon=5000.0, max_forward_budget=10_000, trace_id="t")
        _ = high.run()
        self.assertGreaterEqual(low.stats.n_resident_events, high.stats.n_resident_events)

    def test_resident_exposure_cap_forces_exact_commit(self):
        chunks_by_core, pred = self._mk([1.0, 20.0])
        sched = EpsilonResidentScheduler(
            chunks_by_core, pred, epsilon=0.0, max_forward_budget=10_000,
            max_resident_exposure=2, trace_id="t",
        )
        sched.run()
        self.assertEqual(
            sched.stats.n_commits, sum(len(v) for v in chunks_by_core.values())
        )
        self.assertLessEqual(sched.stats.max_exposure, 2)


class TestSyntheticEndToEnd(unittest.TestCase):
    def test_synth_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace_dirs = synth_default(os.path.join(tmp, "raw"), n_uops=512)
            for td in trace_dirs:
                art = build_and_dump_trace(
                    td, out_dir=os.path.join(tmp, "cache", os.path.basename(os.path.dirname(td))),
                    K=64, epsilon=200.0, tick_per_cycle=500.0,
                    max_forward_budget=1024,
                )
                self.assertGreater(art.n_chunks, 0)
                self.assertGreater(art.n_samples, 0)


@unittest.skipUnless(HAS_TORCH, "torch unavailable")
class TestFunctionalOnlyModel(unittest.TestCase):
    def test_batched_full_cross_attention_matches_reference(self):
        """Core-count bucketing must preserve the original full-QKVR result."""
        torch.manual_seed(7)
        block = FunctionalInteractionBlock(d_dyn=8, n_heads=2, dropout=0.0)
        # Three samples: 2, 3, and 1 active cores.  The last valid UOP of
        # several cores is masked to also exercise fixed-K tail chunks.
        sample_ptr = torch.tensor([0, 2, 5, 6], dtype=torch.long)
        r = torch.randn(6, 5, 8)
        k = torch.randn(6, 5, 8)
        v = torch.randn(6, 5, 8)
        mask = torch.tensor([
            [1, 1, 1, 1, 1], [1, 1, 1, 0, 0],
            [1, 1, 1, 1, 0], [1, 1, 1, 1, 1], [1, 1, 0, 0, 0],
            [1, 1, 1, 1, 1],
        ], dtype=torch.bool)

        expected_rows = []
        for start, end in zip(sample_ptr.tolist(), sample_ptr.tolist()[1:]):
            for row in range(start, end):
                other_rows = [idx for idx in range(start, end) if idx != row]
                if not other_rows:
                    expected_rows.append(r[row] * 0.0)
                    continue
                expected_rows.append(block._attend(
                    r[row:row + 1],
                    k[other_rows].reshape(1, -1, 8),
                    v[other_rows].reshape(1, -1, 8),
                    mask[other_rows].reshape(1, -1),
                ).squeeze(0))
        expected = torch.stack(expected_rows, dim=0)
        actual = block._cross_attention(r, k, v, mask, sample_ptr)
        torch.testing.assert_close(actual, expected, rtol=1e-5, atol=1e-6)

    def test_model_batch_has_no_timing_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            td = synth_default(os.path.join(tmp, "raw"), n_uops=512)[0]
            out = os.path.join(tmp, "cache")
            build_and_dump_trace(
                td, out_dir=out, K=64, epsilon=200.0,
                tick_per_cycle=500.0, max_forward_budget=1024,
            )
            ds = TCSimSampleDataset([out])
            batch = collate_variable_active([ds[0]])
            self.assertNotIn("T_pred", batch)
            self.assertNotIn("E_pred", batch)
            self.assertNotIn("delta_hat", batch)
            model = TCSimModel(max_K=96)
            pred = model(batch)
            self.assertEqual(pred["log_cpi"].shape[0], batch["core_ids"].shape[0])

    def test_complete_context_sample_split_is_disjoint(self):
        """A seed0 dev split keeps every cross-core context row whole."""
        with tempfile.TemporaryDirectory() as tmp:
            td = synth_default(os.path.join(tmp, "raw"), n_uops=4096)[0]
            out = os.path.join(tmp, "cache")
            build_and_dump_trace(
                td, out_dir=out, K=64, epsilon=200.0,
                tick_per_cycle=500.0, max_forward_budget=1024,
            )
            base = {"validation_percent": 50, "seed": 17}
            train_ds = TCSimSampleDataset([{
                "rollout_dir": out, "sample_split": {**base, "partition": "train"},
            }])
            val_ds = TCSimSampleDataset([{
                "rollout_dir": out, "sample_split": {**base, "partition": "validation"},
            }])
            train_keys = {(x["trace_id"], int(x["step"])) for x in train_ds._flat}
            val_keys = {(x["trace_id"], int(x["step"])) for x in val_ds._flat}
            self.assertTrue(train_keys)
            self.assertTrue(val_keys)
            self.assertFalse(train_keys & val_keys)

    def test_centered_loss_penalizes_core_collapse(self):
        pred_log = torch.tensor([0.0, 0.0], requires_grad=True)
        preds = {"log_cpi": pred_log}
        batch = {
            "log_cpi": torch.tensor([0.0, 1.0]),
            "label_mask": torch.ones(2),
            "context_label_mask": torch.ones(2),
            "context_weight": torch.ones(2),
            "sample_ptr": torch.tensor([0, 2]),
        }
        out = compute_losses(
            preds, batch,
            weights={"abs_log_cpi": 1.0, "centered": 0.5},
            huber_delta_log=0.3, prefix_lens=[],
            centered_spread_threshold=0.1,
        )
        self.assertGreater(float(out.centered), 0.0)
        out.total.backward()
        self.assertGreater(float(pred_log.grad.abs().sum()), 0.0)

    def test_centered_loss_ignores_unidentifiable_symmetry(self):
        preds = {"log_cpi": torch.tensor([0.0, 0.0], requires_grad=True)}
        batch = {
            "log_cpi": torch.tensor([0.0, 1.0]),
            "label_mask": torch.ones(2),
            "context_label_mask": torch.ones(2),
            "context_weight": torch.ones(2),
            "functional_group_id": torch.zeros(2, dtype=torch.long),
            "sample_ptr": torch.tensor([0, 2]),
        }
        out = compute_losses(
            preds, batch,
            weights={"abs_log_cpi": 1.0, "centered": 0.5},
            huber_delta_log=0.3, prefix_lens=[],
            centered_spread_threshold=0.1,
        )
        self.assertEqual(float(out.centered), 0.0)
        self.assertEqual(out.n_spread_samples, 0)


if __name__ == "__main__":
    unittest.main()
