from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

import numpy as np

from eval.macro_v29_scheduler import (
    MacroCursorState,
    MacroSchedulerError,
    apply_macro_step,
    oracle_rollout,
    select_macro_step,
    validate_finished,
)
from eval.macro_v29_rollout import PredictedMacroContext, model_free_rollout
from eval.rollout_macro_v29_checkpoint import _deployment_metrics
from train.macro_v29_dataset import (
    MacroTokenOverflow,
    PackedCoreMacroView,
    assert_model_input_allowlist,
    collate_macro_contexts,
    collate_macro_sequences,
    contiguous_macro_sequences,
    eligible_macro_sample_indices,
    macro_block_partition,
    tokenize_macro_texts,
)


class FakeTokenizer:
    def __len__(self):
        return 512

    def __call__(self, text, **kwargs):
        return {"input_ids": [ord(char) % 511 for char in text]}


class FakeResolver:
    def render_window(self, macro_pcs):
        return [f"add rax, {int(pc)}" for pc in macro_pcs]

    def is_architectural_branch(self, macro_pc):
        return int(macro_pc) == 200


def write_synthetic_core(root: Path) -> None:
    # Three macros with 2, 1 and 3 UOPs.
    arrays = {
        "fields": np.zeros((6, 26), dtype=np.uint16),
        "macro_end": np.asarray([0, 1, 1, 0, 0, 1], dtype=np.uint8),
        "macro_pc": np.asarray([100, 100, 200, 300, 300, 300], dtype=np.uint64),
        "commit_tick": np.asarray([8, 10, 20, 25, 30, 30], dtype=np.int64),
        "branch": np.asarray([0, 0, 1, 0, 0, 0], dtype=np.uint8),
        "branch_miss": np.asarray([0, 0, 1, 0, 0, 0], dtype=np.uint8),
        "access": np.zeros(6, dtype=np.uint8),
        "semantic_flags": np.zeros(6, dtype=np.uint8),
        "resource": np.full((6, 10), -1, dtype=np.int64),
        "physical_line": np.full(6, -1, dtype=np.int64),
        "functional_line": np.full(6, -1, dtype=np.int64),
        "functional_page": np.full(6, -1, dtype=np.int64),
        "producer_log": np.zeros(6, dtype=np.float32),
    }
    for name, value in arrays.items():
        np.save(root / f"{name}.npy", value)


def write_large_macro_core(root: Path, uops: int = 60) -> None:
    arrays = {
        "fields": np.zeros((uops, 26), dtype=np.uint16),
        "macro_end": np.asarray([0] * (uops - 1) + [1], dtype=np.uint8),
        "macro_pc": np.full(uops, 400, dtype=np.uint64),
        "commit_tick": np.arange(1, uops + 1, dtype=np.int64),
        "branch": np.zeros(uops, dtype=np.uint8),
        "branch_miss": np.zeros(uops, dtype=np.uint8),
        "access": np.zeros(uops, dtype=np.uint8),
        "semantic_flags": np.zeros(uops, dtype=np.uint8),
        "resource": np.full((uops, 10), -1, dtype=np.int64),
        "physical_line": np.full(uops, -1, dtype=np.int64),
        "functional_line": np.full(uops, -1, dtype=np.int64),
        "functional_page": np.full(uops, -1, dtype=np.int64),
        "producer_log": np.zeros(uops, dtype=np.float32),
    }
    for name, value in arrays.items():
        np.save(root / f"{name}.npy", value)


class MacroDatasetContractTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        write_synthetic_core(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_deployment_metrics_use_macro_headline_contract(self):
        context = SimpleNamespace(meta={
            "tick_per_cycle": 2.0,
            "cores": [
                {
                    "core_id": 0, "n_macros": 10, "n_branches": 4,
                    "n_branch_misses": 1, "first_commit_tick": 100,
                    "last_commit_tick": 140, "full_macro_cpi": 2.0,
                },
                {
                    "core_id": 1, "n_macros": 20, "n_branches": 6,
                    "n_branch_misses": 2, "first_commit_tick": 100,
                    "last_commit_tick": 180, "full_macro_cpi": 2.0,
                },
            ],
        })
        rollout = {
            "complete": True,
            "total_consumed_macros": 30,
            "global_time_cycles": 40.0,
            "predicted_last_commit_cycles": {"0": 20.0, "1": 40.0},
            "per_core_retired_macros": {"0": 10, "1": 20},
            "functional_state": {
                "predicted_branch_misses": 3.0,
                "architectural_branches": 10,
            },
        }
        metrics = _deployment_metrics(context, rollout)
        self.assertEqual(metrics["metric_scope"], "full_roi")
        self.assertEqual(metrics["roi_completion_fraction"], 1.0)
        self.assertEqual(metrics["predicted_macro_cpi"], 2.0)
        self.assertEqual(metrics["true_macro_cpi"], 2.0)
        self.assertEqual(metrics["makespan_abs_relative_error"], 0.0)
        self.assertEqual(metrics["branch_miss_rate_abs_error_pp"], 0.0)

    def test_macro_boundaries_targets_and_separation(self):
        view = PackedCoreMacroView(
            self.root, tick_per_cycle=2.0, k_macro=4,
        )
        report = view.validate_contract()
        self.assertEqual(report["n_macros"], 3)
        self.assertEqual(report["max_uops_per_macro"], 3)
        self.assertEqual(report["max_same_tick_macros"], 1)
        window = view.window_from_cursor(0, state_tick=0)
        np.testing.assert_array_equal(
            window.model_inputs["uop_count"], [2, 1, 3, 0],
        )
        np.testing.assert_allclose(
            window.labels["commit_time_target_macro"], [5.0, 10.0, 15.0, 0.0],
        )
        self.assertNotIn("macro_pc", window.model_inputs)
        self.assertNotIn("commit_time_target_macro", window.model_inputs)
        self.assertEqual(view.cursor_at_tick(10), 1)

    def test_native_token_spans_are_complete(self):
        view = PackedCoreMacroView(
            self.root, tick_per_cycle=2.0, k_macro=4,
        )
        window = view.window_from_cursor(0, state_tick=0)
        view.attach_native_tokens(
            window, FakeResolver(), FakeTokenizer(), max_tokens=128,
        )
        starts = window.model_inputs["macro_token_start"]
        ends = window.model_inputs["macro_token_end"]
        self.assertTrue(np.all(ends[:3] > starts[:3]))
        self.assertTrue(np.all(starts[1:3] == ends[:2]))
        self.assertEqual(int(window.model_inputs["token_to_macro"].max()), 2)
        window.model_inputs.update({
            "dynamic_uop_fields": np.full((6, 8), 8, dtype=np.int64),
            "chunk_summary": np.zeros(38, dtype=np.float32),
            "relation_features": np.zeros(22, dtype=np.float32),
            "state_features": np.zeros(5, dtype=np.float32),
            "uarch_features": np.zeros(29, dtype=np.float32),
        })
        assert_model_input_allowlist(window)
        batch = collate_macro_contexts(
            [[window, window]], pad_token_id=0,
        )
        self.assertEqual(tuple(batch["uop_fields"].shape), (2, 6, 26))
        self.assertEqual(tuple(batch["uop_to_macro"].shape), (2, 6))
        self.assertEqual(batch["sample_ptr"].tolist(), [0, 2])

    def test_token_overflow_fails_instead_of_truncating(self):
        with self.assertRaises(MacroTokenOverflow):
            tokenize_macro_texts(
                ["mov rax, rbx"] * 4,
                FakeTokenizer(),
                k_macro=4,
                max_tokens=8,
            )

    def test_learned_null_collation_contains_no_semantic_or_token_input(self):
        view = PackedCoreMacroView(
            self.root, tick_per_cycle=2.0, k_macro=4,
        )
        window = view.window_from_cursor(0, state_tick=0)
        view.attach_learned_null(window, FakeResolver())
        window.model_inputs.update({
            "dynamic_uop_fields": np.full((6, 8), 8, dtype=np.int64),
            "chunk_summary": np.zeros(38, dtype=np.float32),
            "relation_features": np.zeros(22, dtype=np.float32),
            "state_features": np.zeros(5, dtype=np.float32),
            "uarch_features": np.zeros(29, dtype=np.float32),
        })
        assert_model_input_allowlist(window)
        batch = collate_macro_contexts([[window]], pad_token_id=0)
        self.assertEqual(tuple(batch["null_semantic_marker"].shape), (1, 4))
        self.assertEqual(int(batch["null_semantic_marker"].sum()), 0)
        for forbidden in (
            "input_ids", "attention_mask", "static_semantic", "static_anchor",
        ):
            self.assertNotIn(forbidden, batch)

    def test_ragged_side_preserves_sixty_uop_macro(self):
        large = self.root / "large"
        large.mkdir()
        write_large_macro_core(large)
        view = PackedCoreMacroView(
            large, tick_per_cycle=1.0, k_macro=2,
        )
        window = view.window_from_cursor(0, state_tick=0)
        self.assertEqual(int(window.model_inputs["uop_count"][0]), 60)
        self.assertEqual(tuple(window.model_inputs["uop_fields"].shape), (60, 26))
        np.testing.assert_array_equal(
            window.model_inputs["uop_to_macro"], np.zeros(60, dtype=np.int16),
        )

    def test_guarded_macro_sequences_do_not_cross_blocks(self):
        np.save(self.root / "sample_ticks.npy", np.arange(0, 60, 2, dtype=np.int64))
        np.save(
            self.root / "sample_block_ids.npy",
            np.arange(0, 60, 2, dtype=np.int64) // 20,
        )
        macro_ticks = np.arange(1, 101, dtype=np.int64)
        view = SimpleNamespace(
            n_macros=len(macro_ticks),
            macro_end_tick=macro_ticks,
            cursor_at_tick=lambda tick: int(
                np.searchsorted(macro_ticks, int(tick), side="right")
            ),
        )
        context = SimpleNamespace(
            trace_root=self.root,
            horizons=(2.0,),
            tick_per_cycle=1.0,
            k_macro=2,
            core_ids=(0,),
            views={0: view},
            core_meta={0: {"roi_begin_tick": 0}},
            meta={
                "trace_id": "synthetic/trace",
                "sample_grid": {
                    "start_tick": 0,
                    "block_ticks": 20,
                    "block_cycles": 20.0,
                },
            },
        )
        policy = {
            "validation_percent": 50,
            "seed": 7,
            "guard_cycles": 2.0,
            "require_full_lookahead_within_block": True,
        }
        policy["partition"] = macro_block_partition(
            "synthetic/trace", 0, policy,
        )
        eligible = eligible_macro_sample_indices(context, policy)
        self.assertTrue(eligible)
        ticks = np.load(self.root / "sample_ticks.npy")
        blocks = np.load(self.root / "sample_block_ids.npy")
        for index in eligible:
            block = int(blocks[index])
            self.assertEqual(
                macro_block_partition("synthetic/trace", block, policy),
                policy["partition"],
            )
            position = int(ticks[index]) - block * 20
            self.assertGreaterEqual(position, 2)
            self.assertLess(position, 18)
        sequences = contiguous_macro_sequences(
            eligible, blocks, sequence_length=3, sequence_stride=1,
        )
        self.assertTrue(sequences)
        for sequence in sequences:
            self.assertEqual(len({int(blocks[index]) for index in sequence}), 1)
            self.assertTrue(all(
                right == left + 1
                for left, right in zip(sequence, sequence[1:])
            ))

    def test_sequence_collation_emits_drift_grouping(self):
        view = PackedCoreMacroView(
            self.root, tick_per_cycle=2.0, k_macro=4,
        )
        window = view.window_from_cursor(0, state_tick=0)
        view.attach_native_tokens(
            window, FakeResolver(), FakeTokenizer(), max_tokens=128,
        )
        window.model_inputs.update({
            "dynamic_uop_fields": np.full((6, 8), 8, dtype=np.int64),
            "chunk_summary": np.zeros(38, dtype=np.float32),
            "relation_features": np.zeros(22, dtype=np.float32),
            "state_features": np.zeros(5, dtype=np.float32),
            "uarch_features": np.zeros(29, dtype=np.float32),
        })
        item = {
            "contexts": [[window, window], [window, window]],
            "trace_id": "synthetic/trace",
            "sample_indices": (10, 11),
            "sample_ticks": (100, 110),
            "sample_block_id": 3,
            "sample_period_cycles": 64.0,
            "horizons": (16, 32, 64, 128, 256, 512, 1024),
        }
        batch = collate_macro_sequences([item], pad_token_id=0)
        self.assertEqual(batch["sample_ptr"].tolist(), [0, 2, 4])
        self.assertEqual(batch["sequence_ptr"].tolist(), [0, 2])
        self.assertEqual(batch["row_sequence"].tolist(), [0, 0, 0, 0])
        self.assertEqual(batch["row_sequence_step"].tolist(), [0, 0, 1, 1])
        self.assertEqual(batch["sample_block_ids"].tolist(), [3, 3])


class MacroSchedulerContractTest(unittest.TestCase):
    def test_stride_guard_and_exact_finish(self):
        times = np.asarray([
            [1.0, 2.0, 2.0, 3.0],
            [1.5, 2.5, 3.5, 4.5],
        ])
        valid = np.ones_like(times, dtype=np.bool_)
        with self.assertRaises(MacroSchedulerError):
            select_macro_step(
                times, valid, target_stride_macro=5, max_step_cycles=10.0,
            )
        full_window = select_macro_step(
            times, valid, target_stride_macro=4, max_step_cycles=10.0,
        )
        np.testing.assert_array_equal(full_window.consumed_macros, [4, 2])
        step = select_macro_step(
            times, valid, target_stride_macro=2, max_step_cycles=10.0,
        )
        np.testing.assert_array_equal(step.consumed_macros, [3, 1])
        begins = [
            np.asarray([0, 2, 3, 6]),
            np.asarray([0, 1, 2, 3]),
        ]
        ends = [
            np.asarray([2, 3, 6, 7]),
            np.asarray([1, 2, 3, 4]),
        ]
        state = MacroCursorState.at_start(begins, global_time_cycles=0.0)
        apply_macro_step(state, step, begins, ends)
        np.testing.assert_array_equal(state.macro_cursors, [3, 1])
        state.macro_cursors[:] = [4, 4]
        state.uop_cursors[:] = [7, 4]
        report = validate_finished(state, ends)
        self.assertEqual(report["total_macros"], 8)
        self.assertEqual(report["total_uops"], 11)

    def test_full_window_stride_allows_zero_cycle_tie_continuation(self):
        macro_ticks = [np.asarray([1, 1, 1, 2], dtype=np.int64)]
        begins = [np.arange(4, dtype=np.int64)]
        ends = [np.arange(1, 5, dtype=np.int64)]
        report = oracle_rollout(
            macro_ticks,
            begins,
            ends,
            tick_per_cycle=1.0,
            start_tick=0,
            k_macro=2,
            target_stride_macro=2,
            max_step_cycles=10.0,
        )
        self.assertEqual(report["total_macros"], 4.0)
        self.assertEqual(report["steps"], 2.0)
        self.assertEqual(report["final_global_time_cycles"], 2.0)

    def test_label_free_model_rollout_finishes_exactly_once(self):
        mappings = {
            0: (np.asarray([0, 2, 3, 6]), np.asarray([2, 3, 6, 7])),
            1: (np.asarray([0, 1, 3]), np.asarray([1, 3, 4])),
        }
        context = SimpleNamespace(
            core_ids=(0, 1),
            views={
                core_id: SimpleNamespace(
                    n_macros=len(begin),
                    macro_uop_begin=begin,
                    macro_uop_end=end,
                )
                for core_id, (begin, end) in mappings.items()
            },
        )

        class FakePredictor:
            def predict(self, cursors, **kwargs):
                active = tuple(
                    core_id for core_id in context.core_ids
                    if cursors[core_id] < context.views[core_id].n_macros
                )
                times = np.zeros((len(active), 4), dtype=np.float64)
                valid = np.zeros((len(active), 4), dtype=np.bool_)
                for row, core_id in enumerate(active):
                    remaining = context.views[core_id].n_macros - cursors[core_id]
                    count = min(4, remaining)
                    times[row, :count] = np.arange(1, count + 1)
                    valid[row, :count] = True
                return PredictedMacroContext(
                    core_ids=active,
                    commit_time_macro=times,
                    valid_macro_mask=valid,
                    branch_miss_probability=np.zeros_like(times),
                    label_keys=(),
                )

        progress_events = []
        report = model_free_rollout(
            context,
            FakePredictor(),
            target_stride_macro=2,
            max_step_cycles=10.0,
            progress_interval=1,
            progress=progress_events.append,
        )
        self.assertTrue(report["complete"])
        self.assertEqual(report["total_consumed_macros"], 7)
        self.assertEqual(report["total_consumed_uops"], 11)
        self.assertEqual(report["exactly_once"]["total_uops"], 11)
        self.assertEqual(report["per_core_retired_macros"], {"0": 4, "1": 3})
        self.assertEqual(report["per_core_retired_uops"], {"0": 7, "1": 4})
        self.assertFalse(report["functional_state"]["available"])
        self.assertEqual(report["free_context_label_keys"], [])
        self.assertTrue(progress_events)
        self.assertEqual(progress_events[-1]["phase"], "free_running")
        self.assertEqual(progress_events[-1]["retired_macros"], 7)
        self.assertEqual(progress_events[-1]["total_macros"], 7)
        self.assertGreater(progress_events[-1]["macro_per_s"], 0.0)


if __name__ == "__main__":
    unittest.main()
