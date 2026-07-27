from __future__ import annotations

import json
import os
import shutil
import threading

import numpy as np
import pytest

torch = pytest.importorskip("torch")

from tcsim.v29.contracts import (
    FIELD_INDEX,
    FIELD_NAMES,
    RESOURCE_KEY_NAMES,
    UARCH_FEATURE_NAMES,
    feature_contract_metadata,
)
from tcsim.v29.dataset import (
    CONTEXT_BUILDER,
    CONTEXT_PHASE_NAMES,
    FUNCTIONAL_CONTAINER_SCHEMA,
    V29FunctionalStore,
    V29TraceStore,
)
from tcsim.v29.builder import _build_performance_sidecars
from tcsim.v29.diagnostics import single_sample_overfit, visible_signature_audit
from tcsim.v29.inference import (
    V29ParallelModelRunner,
    V29Prediction,
    _prediction_gaps,
    aggregate_trace_reports,
    evaluate_oracle_one_step,
    replay_branch_baseline,
    replay_configured_branch_predictor,
    render_text_report,
    run_free_running,
)
from tcsim.v29.train import _balanced_validation_indices
from tcsim.utils.config import TCSimConfig


def _make_cache(tmp_path):
    root = tmp_path / "trace"
    (root / "cores" / "0").mkdir(parents=True)
    (root / "cores" / "1").mkdir(parents=True)
    horizons = (2.0, 4.0, 8.0)
    core_meta = []
    for core_id in (0, 1):
        core_dir = root / "cores" / str(core_id)
        count = 6
        fields = np.zeros((count, len(FIELD_NAMES)), dtype=np.uint16)
        fields[[1, 4], FIELD_INDEX["branch_kind"]] = 3
        fields[[1, 4], FIELD_INDEX["branch_taken"]] = 2
        resource = np.full(
            (count, len(RESOURCE_KEY_NAMES)), -1, dtype=np.int64,
        )
        branch = np.zeros(count, dtype=np.uint8)
        branch[[1, 4]] = 1
        branch_miss = np.zeros(count, dtype=np.uint8)
        branch_miss[4] = 1
        arrays = {
            "fields": fields,
            "resource": resource,
            "commit_tick": np.arange(2, 14, 2, dtype=np.int64),
            "physical_line": np.full(count, -1, dtype=np.int64),
            "functional_line": np.full(count, -1, dtype=np.int64),
            "functional_page": np.full(count, -1, dtype=np.int64),
            "producer_log": np.zeros(count, dtype=np.float32),
            "semantic_flags": branch.astype(np.uint8) << 3,
            "access": np.zeros(count, dtype=np.uint8),
            "macro_pc": np.arange(100, 106, dtype=np.uint64),
            "macro_end": np.ones(count, dtype=np.uint8),
            "branch": branch,
            "branch_miss": branch_miss,
        }
        for name, value in arrays.items():
            np.save(str(core_dir / f"{name}.npy"), value)
        core_meta.append({
            "core_id": core_id,
            "n_uops": count,
            "n_macros": count,
            "n_branches": 2,
            "n_branch_misses": 1,
            "n_atomics": 0,
            "roi_begin_tick": 0,
            "roi_end_tick": 12,
            "first_commit_tick": 2,
            "last_commit_tick": 12,
            "full_uop_cpi": 2.0,
            "full_macro_cpi": 2.0,
        })
    np.save(str(root / "sample_ticks.npy"), np.asarray([0, 4], dtype=np.int64))
    np.save(
        str(root / "sample_cursors.npy"),
        np.asarray([[0, 0], [2, 2]], dtype=np.int32),
    )
    np.save(str(root / "sample_block_ids.npy"), np.asarray([0, 0], dtype=np.int32))
    meta = {
        **feature_contract_metadata(
            predictor_hash="predictor",
            resource_decoder_hash="decoder",
            horizons=horizons,
            sample_period_cycles=4.0,
        ),
        "trace_id": "toy/c2",
        "workload": "toy",
        "raw_root": "toy_seed0_c2",
        "trace_dir": "/functional/toy",
        "K": 256,
        "tick_per_cycle": 1.0,
        "n_cores": 2,
        "core_ids": [0, 1],
        "n_uops": 12,
        "n_samples": 2,
        "cores": core_meta,
        "min_uops_per_core_contract": 1,
        "max_uops_per_core_contract": 10,
        "max_full_uop_cpi_contract": 10.0,
        "collection_provenance": {"ff_atomic_verified": True},
        "sample_grid": {
            "start_tick": 0,
            "stop_tick": 12,
            "sample_period_cycles": 4.0,
            "block_cycles": 64.0,
        },
        "uarch_hash": "uarch",
        "uarch_features": [0.0] * len(UARCH_FEATURE_NAMES),
        "quality": {
            "status": "pass", "roi_atomic_uops": 0,
            "ff_atomic_verified": True, "synchronous_roi_start": True,
        },
    }
    with open(root / "meta.json", "w", encoding="utf-8") as handle:
        json.dump(meta, handle)
    return str(root)


def _make_functional_cache(tmp_path):
    labeled = _make_cache(tmp_path)
    functional = tmp_path / "functional"
    shutil.copytree(labeled, functional)
    for core_id in (0, 1):
        os.remove(functional / "cores" / str(core_id) / "commit_tick.npy")
        os.remove(functional / "cores" / str(core_id) / "branch_miss.npy")
    for name in ("sample_ticks.npy", "sample_cursors.npy", "sample_block_ids.npy"):
        os.remove(functional / name)
    with open(functional / "meta.json", "r", encoding="utf-8") as handle:
        meta = json.load(handle)
    meta["container_schema"] = FUNCTIONAL_CONTAINER_SCHEMA
    meta["n_samples"] = 0
    meta["quality"] = {
        "status": "pass",
        "functional_only": True,
        "contains_commit_tick": False,
        "contains_branch_miss_label": False,
    }
    for core in meta["cores"]:
        for key in (
            "n_branch_misses", "roi_begin_tick", "roi_end_tick",
            "first_commit_tick", "last_commit_tick",
        ):
            core.pop(key, None)
    with open(functional / "meta.json", "w", encoding="utf-8") as handle:
        json.dump(meta, handle)
    return str(functional)


def _expand_cache_uops(cache_dir, count):
    count = int(count)
    for core_id in (0, 1):
        core_dir = os.path.join(cache_dir, "cores", str(core_id))
        arrays = {
            "fields": np.zeros((count, len(FIELD_NAMES)), dtype=np.uint16),
            "resource": np.full(
                (count, len(RESOURCE_KEY_NAMES)), -1, dtype=np.int64,
            ),
            "commit_tick": np.arange(2, 2 * count + 1, 2, dtype=np.int64),
            "physical_line": np.full(count, -1, dtype=np.int64),
            "functional_line": np.full(count, -1, dtype=np.int64),
            "functional_page": np.full(count, -1, dtype=np.int64),
            "producer_log": np.zeros(count, dtype=np.float32),
            "semantic_flags": np.zeros(count, dtype=np.uint8),
            "access": np.zeros(count, dtype=np.uint8),
            "macro_pc": np.arange(100, 100 + count, dtype=np.uint64),
            "macro_end": np.ones(count, dtype=np.uint8),
            "branch": np.zeros(count, dtype=np.uint8),
            "branch_miss": np.zeros(count, dtype=np.uint8),
        }
        for name, value in arrays.items():
            np.save(os.path.join(core_dir, f"{name}.npy"), value)
    meta_path = os.path.join(cache_dir, "meta.json")
    with open(meta_path, "r", encoding="utf-8") as handle:
        meta = json.load(handle)
    meta["n_uops"] = 2 * count
    meta["max_uops_per_core_contract"] = count
    for core in meta["cores"]:
        core.update({
            "n_uops": count,
            "n_macros": count,
            "n_branches": 0,
            "n_branch_misses": 0,
            "roi_end_tick": 2 * count,
            "last_commit_tick": 2 * count,
            "full_uop_cpi": 2.0,
            "full_macro_cpi": 2.0,
        })
    with open(meta_path, "w", encoding="utf-8") as handle:
        json.dump(meta, handle)


def _add_performance_sidecars(cache_dir):
    meta_path = os.path.join(cache_dir, "meta.json")
    with open(meta_path, "r", encoding="utf-8") as handle:
        metadata = json.load(handle)
    metadata["performance_sidecars"] = _build_performance_sidecars(
        cache_dir, metadata["cores"],
    )
    with open(meta_path, "w", encoding="utf-8") as handle:
        json.dump(metadata, handle)


def _assert_context_equal(left, right, *, ignore=()):
    ignored = set(ignore)
    assert set(left) - ignored == set(right) - ignored
    for key in sorted(set(left) - ignored):
        lhs, rhs = left[key], right[key]
        if hasattr(lhs, "detach"):
            assert lhs.dtype == rhs.dtype, key
            assert lhs.shape == rhs.shape, key
            assert np.array_equal(lhs.detach().cpu().numpy(), rhs.detach().cpu().numpy()), key
        else:
            assert lhs == rhs, key


def _add_branch_replay_contract(cache_dir):
    for core_id in (0, 1):
        core_dir = os.path.join(cache_dir, "cores", str(core_id))
        np.save(
            os.path.join(core_dir, "replay_branch_index.npy"),
            np.asarray([1, 4], dtype=np.uint32),
        )
        np.save(
            os.path.join(core_dir, "replay_branch_target.npy"),
            np.asarray([201, 204], dtype=np.uint64),
        )
        np.save(
            os.path.join(core_dir, "replay_branch_next_pc.npy"),
            np.asarray([201, 204], dtype=np.uint64),
        )
        np.save(
            os.path.join(core_dir, "replay_branch_history.npy"),
            np.asarray([0, 1], dtype=np.uint16),
        )
        np.save(
            os.path.join(core_dir, "replay_branch_thread_id.npy"),
            np.asarray([0, 0], dtype=np.uint16),
        )
    meta_path = os.path.join(cache_dir, "meta.json")
    with open(meta_path, "r", encoding="utf-8") as handle:
        meta = json.load(handle)
    meta["functional_branch_replay_contract"] = "functional-branch-replay-v1"
    meta["uarch_profile"] = {
        "branch_predictor": {
            "root": {
                "type": "BranchPredictor", "numthreads": "1",
                "requiresbtbhit": "false", "updatebtbatsquash": "true",
                "speculativehistupdate": "true", "instshiftamt": "0",
            },
            "conditionalBranchPred": {
                "type": "TournamentBP", "numthreads": "1",
                "localpredictorsize": "8", "localhistorytablesize": "8",
                "globalpredictorsize": "8", "choicepredictorsize": "8",
                "localctrbits": "2", "globalctrbits": "2",
                "choicectrbits": "2", "instshiftamt": "0",
            },
            "btb": {
                "type": "SimpleBTB", "numthreads": "1",
                "numentries": "8", "associativity": "1",
                "tagbits": "8", "instshiftamt": "0",
            },
            "btb.btbIndexingPolicy": {
                "type": "BTBSetAssociative", "num_entries": "8",
                "assoc": "1", "tag_bits": "8", "set_shift": "0",
            },
            "btb.btbReplPolicy": {"type": "LRURP"},
            "ras": {
                "type": "ReturnAddrStack", "numthreads": "1",
                "numentries": "4",
            },
            "indirectBranchPred": {
                "type": "SimpleIndirectPredictor", "numthreads": "1",
                "indirectsets": "8", "indirectways": "1",
                "indirecttagsize": "8", "indirectpathlength": "2",
                "speculativepathlength": "8", "indirectghrbits": "3",
                "instshiftamt": "0", "indirecthashghr": "true",
                "indirecthashtargets": "true",
            },
        }
    }
    with open(meta_path, "w", encoding="utf-8") as handle:
        json.dump(meta, handle)


class _PerfectEngine:
    def __init__(self):
        self.started = 0
        self.free_calls = 0

    def begin_trace(self, _store):
        self.started += 1

    def predict(self, store, context):
        assert not any(
            key in context for key in (
                "commit_time_target", "prefix_target", "progress_target",
                "branch_miss_target",
            )
        ) or "commit_time_target" in context
        rows, K = context["valid_uop_mask"].shape
        tau = np.tile(
            np.arange(1, K + 1, dtype=np.float32) * 2.0,
            (rows, 1),
        )
        valid = context["valid_uop_mask"].numpy().astype(bool)
        horizons = np.asarray(store.horizons, dtype=np.float32)
        probability = (
            (tau[:, :, None] <= horizons[None, None, :])
            & valid[:, :, None]
        ).astype(np.float32)
        return V29Prediction(
            commit_time=tau,
            commit_probability=probability,
            progress=probability.sum(axis=1),
            branch_miss_probability=np.full((rows, K), 0.25, dtype=np.float32),
            valid_uop_mask=valid,
        )

    def predict_free(self, store, context):
        self.free_calls += 1
        prediction = self.predict(store, context)
        return V29Prediction(
            commit_time=prediction.commit_time,
            commit_probability=None,
            progress=None,
            branch_miss_probability=prediction.branch_miss_probability,
            valid_uop_mask=prediction.valid_uop_mask,
        )

    def stats(self):
        return {
            "static_cache_hits": 4,
            "static_cache_misses": 2,
            "static_cache_evictions": 0,
            "static_cache_hit_rate": 4.0 / 6.0,
            "gpu_peak_memory_bytes": 0,
        }


class _ParallelLane(_PerfectEngine):
    checkpoint_meta = {"checkpoint_id": "parallel-test"}
    config = object()
    amp_dtype = None
    device = torch.device("cpu")


def test_direct_retirement_gap_survives_rounded_commit_prefix():
    gap = np.asarray([[1000.0, 1.0e-5, 2.0e-5]], dtype=np.float32)
    rounded_tau = np.cumsum(gap, axis=1, dtype=np.float64).astype(np.float32)
    assert rounded_tau[0, 1] == rounded_tau[0, 0]
    prediction = V29Prediction(
        commit_time=rounded_tau,
        commit_probability=None,
        progress=None,
        branch_miss_probability=np.zeros_like(gap),
        valid_uop_mask=np.ones_like(gap, dtype=np.bool_),
        retirement_gap=gap.astype(np.float64),
    )
    np.testing.assert_allclose(
        _prediction_gaps(prediction, 0, 3),
        gap[0].astype(np.float64),
        rtol=0.0,
        atol=0.0,
    )


class _EarlyFinishEngine(_PerfectEngine):
    def predict(self, store, context):
        rows, K = context["valid_uop_mask"].shape
        slots = context["core_slots"].tolist()
        tau = np.stack([
            np.arange(1, K + 1, dtype=np.float32) * (1.0 if slot == 0 else 2.0)
            for slot in slots
        ])
        valid = context["valid_uop_mask"].numpy().astype(bool)
        horizons = np.asarray(store.horizons, dtype=np.float32)
        probability = (
            (tau[:, :, None] <= horizons[None, None, :])
            & valid[:, :, None]
        ).astype(np.float32)
        return V29Prediction(
            commit_time=tau,
            commit_probability=probability,
            progress=probability.sum(axis=1),
            branch_miss_probability=np.full((rows, K), 0.25, dtype=np.float32),
            valid_uop_mask=valid,
        )


class _SpeculativeGapEngine(_PerfectEngine):
    def predict(self, store, context):
        rows, K = context["valid_uop_mask"].shape
        slots = context["core_slots"].tolist()
        tau = np.stack([
            np.arange(1, K + 1, dtype=np.float32) * (
                0.25 if slot == 0 else 2.0
            )
            for slot in slots
        ])
        valid = context["valid_uop_mask"].numpy().astype(bool)
        horizons = np.asarray(store.horizons, dtype=np.float32)
        probability = (
            (tau[:, :, None] <= horizons[None, None, :])
            & valid[:, :, None]
        ).astype(np.float32)
        return V29Prediction(
            commit_time=tau,
            commit_probability=probability,
            progress=probability.sum(axis=1),
            branch_miss_probability=np.full((rows, K), 0.25, dtype=np.float32),
            valid_uop_mask=valid,
        )


def test_deployment_context_contains_no_oracle_labels(tmp_path):
    store = V29TraceStore(_make_cache(tmp_path))

    class PoisonOracleArray:
        def __getitem__(self, _key):
            raise AssertionError("deployment context read an oracle array")

    for core_id in store.core_ids:
        store.cores[core_id]["commit_tick"] = PoisonOracleArray()
        store.cores[core_id]["branch_miss"] = PoisonOracleArray()
    context = store.context_from_cursors(
        [0, 0], state_time_cycles=0.0, include_labels=False,
        last_commit_cycles={0: 0.0, 1: 0.0},
    )
    for key in (
        "commit_time_target", "prefix_target", "progress_target",
        "branch_miss_target",
    ):
        assert key not in context


def test_batched_multicore_windows_match_reference_and_compact_ids(tmp_path):
    cache = _make_cache(tmp_path)
    reference_store = V29TraceStore(cache)
    reference = reference_store.context_from_cursors(
        [0, 0], state_time_tick=0, include_labels=True,
        use_batched_windows=False,
    )
    batched_store = V29TraceStore(cache)
    batched = batched_store.context_from_cursors(
        [0, 0], state_time_tick=0, include_labels=True,
        use_batched_windows=True,
    )
    _assert_context_equal(reference, batched)

    _add_performance_sidecars(cache)
    compact_store = V29TraceStore(cache)
    compact = compact_store.context_from_cursors(
        [0, 0], state_time_tick=0, include_labels=True,
    )
    _assert_context_equal(reference, compact, ignore={"macro_id"})
    assert compact_store.runtime_stats()["resource_compact_sidecar"] is True
    macro_ids = compact["macro_id"].numpy()
    valid = compact["valid_uop_mask"].numpy()
    for row, (slot, cursor) in enumerate(zip(
        compact["core_slots"].tolist(), compact["cursors"].tolist(),
    )):
        core_id = compact_store.core_ids[int(slot)]
        expected = compact_store.cores[core_id]["macro_pc"][
            int(cursor):int(cursor) + int(valid[row].sum())
        ]
        actual = compact_store.macro_pc_table[macro_ids[row][valid[row]]]
        np.testing.assert_array_equal(actual, expected)


def test_declared_performance_sidecars_fail_closed_when_partial(tmp_path):
    cache = _make_cache(tmp_path)
    _add_performance_sidecars(cache)
    os.remove(os.path.join(cache, "cores", "1", "macro_id.npy"))
    with pytest.raises(RuntimeError, match="partial"):
        V29TraceStore(cache)


def test_compact_resource_sidecar_fails_closed_on_value_mismatch(tmp_path):
    cache = _make_cache(tmp_path)
    _add_performance_sidecars(cache)
    path = os.path.join(cache, "cores", "0", "resource_compact.npy")
    compact = np.load(path, mmap_mode="r+")
    compact[0, 0] = np.uint32(0)
    compact.flush()
    del compact
    with pytest.raises(RuntimeError, match="sampled validation failed"):
        V29TraceStore(cache)


def test_cpu_window_cache_is_bounded_per_core_and_reported(tmp_path):
    store = V29TraceStore(_make_cache(tmp_path))
    first = store.window(0, 0, include_oracle=False)
    assert store.window(0, 0, include_oracle=False) is first
    store.window(0, 1, include_oracle=False)
    stats = store.runtime_stats()
    assert stats["context_builder"] == CONTEXT_BUILDER
    assert stats["cpu_window_cache_hits"] == 1
    assert stats["cpu_window_cache_misses"] == 2
    assert stats["cpu_window_cache_entries"] == 1
    assert stats["context_calls"] == 0
    assert set(stats["context_phase_seconds"]) == set(CONTEXT_PHASE_NAMES)


def test_deployment_window_skips_public_python_lists(tmp_path):
    store = V29TraceStore(_make_cache(tmp_path))
    compact = store._window(
        0, 0, include_oracle=True, numpy_only=True,
    )
    for key in (
        "per_uop_fields", "per_uop_resource_keys", "per_uop_lines",
        "per_uop_access", "valid_uop_mask", "semantic_flags",
        "functional_lines", "functional_pages", "producer_logs",
        "macro_pcs", "macro_end", "branch", "read_lines", "write_lines",
        "branch_miss", "commit_ticks",
    ):
        assert key not in compact
    assert "commit_tick" in compact["_numpy"]
    assert "branch_miss" in compact["_numpy"]

    public = store.window(0, 0, include_oracle=True)
    assert isinstance(public["per_uop_fields"], list)
    assert isinstance(public["valid_uop_mask"], list)
    assert isinstance(public["commit_ticks"], list)


def test_context_subphase_timing_is_complete_and_resettable(tmp_path):
    store = V29TraceStore(_make_cache(tmp_path))
    store.context_from_cursors(
        [0, 0], state_time_cycles=0.0, include_labels=False,
        last_commit_cycles={0: 0.0, 1: 0.0},
    )
    stats = store.runtime_stats()
    assert stats["context_calls"] == 1
    assert set(stats["context_phase_seconds"]) == set(CONTEXT_PHASE_NAMES)
    assert all(value >= 0.0 for value in stats["context_phase_seconds"].values())
    assert sum(stats["context_phase_seconds"].values()) > 0.0

    store.reset_runtime_stats(clear_cache=False)
    reset = store.runtime_stats()
    assert reset["context_calls"] == 0
    assert sum(reset["context_phase_seconds"].values()) == 0.0


def test_single_global_time_rollout_consumes_prefix_events_exactly_once(tmp_path):
    store = V29TraceStore(_make_cache(tmp_path))
    report = run_free_running(
        store,
        _PerfectEngine(),
        source={"workload": "toy", "seed": 0},
        target_stride=2,
        max_step_cycles=10.0,
    )
    assert report["complete"] is True
    assert report["steps"] == 3
    assert report["global_time_cycles"] == 12.0
    assert report["retired_uops"] == report["true_uops"] == 12
    assert report["retired_macros"] == report["true_macros"] == 12
    assert report["branch_opportunities"] == 4
    assert report["predicted_branch_misses"] == 1.0
    assert report["true_branch_misses"] == 2
    assert report["predicted_micro_cpi"] == report["true_micro_cpi"] == 2.0
    assert report["micro_cpi_abs_relative_error"] == 0.0
    assert report["cumulative_progress_error_uops"]["max"] == 0.0
    assert report["oracle_cursor_interval_abs_offset_cycles"]["max"] == 0.0
    # The next retirement is still in the future even for a perfect cursor;
    # this residual is not itself a drift metric.
    assert report["oracle_head_abs_residual_cycles"]["max"] == 2.0
    assert report["model_context_uses_oracle_timing"] is False


def test_terminal_interval_detects_core_predicted_finished_too_early(tmp_path):
    store = V29TraceStore(_make_cache(tmp_path))
    report = run_free_running(
        store, _EarlyFinishEngine(), target_stride=2, max_step_cycles=10.0,
    )
    assert report["complete"] is True
    assert report["per_core"][0]["predicted_cycles"] == 6.0
    assert report["per_core"][0]["true_cycles"] == 12.0
    # At predicted T=6 core0 is already at terminal cursor, while its true
    # terminal interval starts at T=12.  This used to disappear from drift.
    assert report["oracle_cursor_interval_abs_offset_cycles"]["max"] == 6.0


def test_free_fast_path_can_skip_horizons_and_oracle_drift(tmp_path):
    store = V29TraceStore(_make_cache(tmp_path))
    engine = _PerfectEngine()
    events = []
    report = run_free_running(
        store,
        engine,
        target_stride=2,
        collect_oracle_drift=False,
        progress_interval=2,
        progress=events.append,
    )
    assert report["complete"] is True
    assert engine.free_calls == report["steps"]
    assert report["oracle_drift_diagnostics_enabled"] is False
    assert report["oracle_timing_usage"] == "final_metrics_only"
    assert report["cumulative_progress_error_uops"]["count"] == 0
    assert [event["step"] for event in events] == [2]
    timing = report["timing_breakdown"]
    phases = timing["context_phase_seconds"]
    assert set(phases) == set(CONTEXT_PHASE_NAMES) | {"call_overhead"}
    assert sum(phases.values()) == pytest.approx(
        timing["context_build_seconds"], rel=1.0e-9, abs=1.0e-9,
    )
    assert report["context_calls"] == report["steps"]
    assert set(events[0]["context_phase_avg_ms"]) == set(phases)
    assert events[0]["context_total_avg_ms"] > 0.0


def test_unconditional_parallel_windows_preserve_exact_once_accounting(tmp_path):
    store = V29TraceStore(_make_cache(tmp_path))
    engine = _PerfectEngine()
    report = run_free_running(
        store,
        engine,
        target_stride=2,
        max_step_cycles=10.0,
        window_parallel_mode="unconditional",
        window_parallel_shift=2,
        window_parallel_depth=2,
    )
    assert report["complete"] is True
    assert report["retired_uops"] == report["true_uops"] == 12
    assert report["retired_macros"] == report["true_macros"] == 12
    assert report["branch_opportunities"] == 4
    assert report["window_parallel_mode"] == "unconditional"
    assert report["parallel_waves"] == 1
    assert report["steps"] == 3
    assert report["model_forwards"] == 2
    assert report["speculative_window_hit_rate"] is None
    assert report["scheduler_window_count"] == report["steps"]
    assert report["scheduler_window_cpi_mape_mean"] == 0.0
    assert report["scheduler_window_predicted_cycles_sum"] == pytest.approx(
        report["predicted_cycles_sum"],
    )
    assert report["scheduler_window_true_cycles_sum"] == pytest.approx(
        report["true_cycles_sum"],
    )


def test_unconditional_parallel_windows_allow_non_overlapping_shift(tmp_path):
    cache = _make_cache(tmp_path)
    _expand_cache_uops(cache, 300)
    store = V29TraceStore(cache)
    report = run_free_running(
        store,
        _PerfectEngine(),
        target_stride=256,
        max_step_cycles=10.0,
        window_parallel_mode="unconditional",
        window_parallel_shift=store.K,
        window_parallel_depth=2,
    )
    assert report["complete"] is True
    assert report["retired_uops"] == report["true_uops"] == 600
    assert report["parallel_waves"] == 1
    assert report["model_forwards"] == 2
    assert report["window_parallel_shift"] == 256


def test_parallel_runner_uses_lane_local_context_workspaces(tmp_path):
    store = V29TraceStore(_make_cache(tmp_path))
    first_workspace = store.fork_context_workspace()
    second_workspace = store.fork_context_workspace()
    assert first_workspace.cores is store.cores
    assert second_workspace.cores is store.cores
    assert first_workspace._window_cache is not second_workspace._window_cache

    runner = V29ParallelModelRunner([_ParallelLane(), _ParallelLane()])
    try:
        barrier = threading.Barrier(2)

        class BarrierWorkspace:
            def context_from_cursors(self, cursors, **_kwargs):
                barrier.wait(timeout=2.0)
                return tuple(cursors)

            def runtime_stats(self):
                return {
                    "context_phase_seconds": {
                        name: 0.0 for name in CONTEXT_PHASE_NAMES
                    },
                }

        built = runner.build_context_many(
            [BarrierWorkspace(), BarrierWorkspace()],
            [(0, 0), (2, 2)],
            state_time_cycles=0.0,
            last_commit_cycles={0: 0.0, 1: 0.0},
        )
        assert built == [(0, 0), (2, 2)]
        report = run_free_running(
            store,
            runner,
            target_stride=2,
            max_step_cycles=10.0,
            window_parallel_mode="unconditional",
            window_parallel_shift=2,
            window_parallel_depth=2,
        )
    finally:
        runner.close()
    assert report["complete"] is True
    assert report["retired_uops"] == report["true_uops"] == 12
    assert report["context_parallel_workers"] == 2
    assert report["context_calls"] == report["model_forwards"] == 2
    assert report["cpu_window_cache_policy"] == (
        "last-window-per-core-per-lane"
    )
    assert report["context_build_parallel_wall_seconds"] > 0.0
    assert report["context_build_worker_seconds"] > 0.0
    assert set(report["context_phase_worker_seconds"]) == set(
        CONTEXT_PHASE_NAMES
    )


def test_speculative_parallel_windows_report_hits_and_full_chains(tmp_path):
    store = V29TraceStore(_make_cache(tmp_path))
    report = run_free_running(
        store,
        _PerfectEngine(),
        target_stride=2,
        max_step_cycles=10.0,
        window_parallel_mode="speculative",
        window_parallel_shift=4,
        window_parallel_depth=2,
    )
    assert report["complete"] is True
    assert report["retired_uops"] == report["true_uops"] == 12
    assert report["speculative_windows_issued"] == 1
    assert report["speculative_windows_accepted"] == 1
    assert report["speculative_windows_rejected"] == 0
    assert report["speculative_window_hit_rate"] == 1.0
    assert report["speculative_full_chain_hit_rate"] == 1.0
    assert report["speculative_first_failure_depth"] == {}
    assert report["steps"] == 3
    assert report["model_forwards"] == 2
    assert report["parallel_waves"] == 1
    trace_report = {
        "n_cores": 2,
        "workload": "toy",
        "seed": 0,
        "free_running": report,
    }
    aggregate = aggregate_trace_reports([trace_report])
    assert aggregate["by_core_count"][0]["speculative_window_hit_rate"] == 1.0
    assert "specHit" in render_text_report(aggregate)


def test_speculative_gap_rejects_current_and_deeper_windows(tmp_path):
    cache = _make_cache(tmp_path)
    _expand_cache_uops(cache, 300)
    store = V29TraceStore(cache)
    report = run_free_running(
        store,
        _SpeculativeGapEngine(),
        target_stride=32,
        max_step_cycles=10.0,
        window_parallel_mode="speculative",
        window_parallel_shift=64,
        window_parallel_depth=2,
    )
    assert report["complete"] is True
    assert report["retired_uops"] == report["true_uops"] == 600
    assert report["speculative_windows_issued"] > 0
    assert report["speculative_windows_rejected"] > 0
    assert report["speculative_window_hit_rate"] < 1.0
    assert int(report["speculative_first_failure_depth"]["1"]) > 0
    assert int(report["speculative_failure_reasons"]["start_not_covered"]) > 0


def test_branch_replay_counts_microcoded_control_uops_without_fake_targets(tmp_path):
    store = V29TraceStore(_make_cache(tmp_path))
    # Model four control UOPs inside one architectural macro per core.  IDIV
    # microcode does this in real x86 traces, so macro boundaries cannot be
    # used to invent branch targets or collapse branch opportunities.
    for core_id in store.core_ids:
        store.cores[core_id]["macro_pc"] = np.full(6, 100, dtype=np.uint64)
        store.cores[core_id]["macro_end"] = np.asarray(
            [0, 0, 0, 0, 0, 1], dtype=np.uint8,
        )
        store.cores[core_id]["branch"] = np.asarray(
            [1, 1, 1, 1, 0, 0], dtype=np.uint8,
        )
        store.cores[core_id]["branch_miss"] = np.asarray(
            [1, 0, 1, 0, 0, 0], dtype=np.uint8,
        )
        fields = np.asarray(store.cores[core_id]["fields"]).copy()
        fields[:, FIELD_INDEX["branch_kind"]] = 3
        fields[:, FIELD_INDEX["branch_taken"]] = 2
        store.cores[core_id]["fields"] = fields
    baseline = replay_branch_baseline(store)
    assert baseline["branches"] == 8
    assert baseline["true_misses"] == 4
    assert baseline["predicted_misses"] == baseline["direction_only_misses"]
    assert baseline["target_component_available"] is False
    assert "last_target_misses" not in baseline


def test_configured_full_branch_replay_uses_compact_functional_arrays(tmp_path):
    cache = _make_cache(tmp_path)
    _add_branch_replay_contract(cache)
    store = V29TraceStore(cache)
    report = replay_configured_branch_predictor(store)
    assert report["status"] == "ok"
    assert report["target_component_available"] is True
    assert report["branches"] == 4
    assert report["functional_history_mismatches"] == 0
    assert report["true_misses"] == 2
    assert report["oracle_labels_consumed_as_input"] is False
    assert report["oracle_labels_used_post_replay_for_evaluation"] is True


def test_oracle_one_step_and_workload_equal_aggregation(tmp_path):
    store = V29TraceStore(_make_cache(tmp_path))
    report = evaluate_oracle_one_step(store, _PerfectEngine())
    assert report["commit_time_log_error"]["mae"] == 0.0
    assert report["commit_time_cycle_error"]["mae"] == 0.0
    assert report["prefix_monotonic_token_violations"] == 0
    assert report["prefix_monotonic_horizon_violations"] == 0
    trace = {
        "trace_id": store.trace_id,
        "workload": "toy",
        "n_cores": 2,
        "oracle_one_step": report,
        "free_running": run_free_running(
            store, _PerfectEngine(), source={"workload": "toy"}, target_stride=2,
        ),
    }
    aggregate = aggregate_trace_reports([trace])
    assert aggregate["aggregation_contract"]["global_pooled_headline_forbidden"] is True
    assert aggregate["by_core_count"][0]["micro_cpi_mape"] == 0.0


def test_visible_signature_variance_and_overfit_gate_execute(tmp_path):
    cache = _make_cache(tmp_path)
    visible = visible_signature_audit([cache], max_samples_per_trace=2)
    assert visible["exact"]["duplicate_row_coverage"] == 1.0
    assert visible["exact"]["head_log_time_irreducible_rmse"] == 0.0
    config = TCSimConfig(
        chunk={"sequence_length": 2},
        model={},
        train={
            "loss_weights": {
                "commit_time": 1.0,
                "prefix_bce": 0.5,
                "progress_count": 0.5,
                "cumulative": 0.25,
                "branch_token": 0.1,
                "branch_count": 0.1,
            },
        },
    )
    overfit = single_sample_overfit(
        cache, config, steps=2, device="cpu", tiny_model=True,
    )
    assert overfit["steps"] == 2
    assert np.isfinite(overfit["final_loss"])


def test_label_free_functional_rollout_never_requires_oracle_arrays(tmp_path):
    store = V29FunctionalStore(_make_functional_cache(tmp_path))
    assert store.has_oracle_labels is False
    assert "commit_tick" not in store.cores[0]
    assert "branch_miss" not in store.cores[0]
    report = run_free_running(store, _PerfectEngine(), target_stride=2)
    assert report["complete"] is True
    assert report["truth_available"] is False
    assert report["oracle_timing_usage"] == "none"
    assert report["retired_uops"] == 12
    assert report["predicted_micro_cpi"] == 2.0
    assert report["branch_opportunities"] == 4


def test_validation_subset_is_trace_balanced_and_time_spread():
    class Dataset:
        sample_trace_ids = ["a"] * 20 + ["b"] * 10 + ["c"] * 5

        def __len__(self):
            return len(self.sample_trace_ids)

    dataset = Dataset()
    selected = _balanced_validation_indices(dataset, 9)
    assert len(selected) == 9
    counts = {
        name: sum(dataset.sample_trace_ids[index] == name for index in selected)
        for name in ("a", "b", "c")
    }
    assert counts == {"a": 3, "b": 3, "c": 3}
    assert 0 in selected and 19 in selected
