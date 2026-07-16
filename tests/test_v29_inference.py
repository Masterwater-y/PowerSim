from __future__ import annotations

import json
import os
import shutil

import numpy as np
import pytest

pytest.importorskip("torch")

from tcsim.v29.contracts import (
    FIELD_INDEX,
    FIELD_NAMES,
    RESOURCE_KEY_NAMES,
    UARCH_FEATURE_NAMES,
    feature_contract_metadata,
)
from tcsim.v29.dataset import (
    FUNCTIONAL_CONTAINER_SCHEMA,
    V29FunctionalStore,
    V29TraceStore,
)
from tcsim.v29.diagnostics import single_sample_overfit, visible_signature_audit
from tcsim.v29.inference import (
    V29Prediction,
    aggregate_trace_reports,
    evaluate_oracle_one_step,
    replay_branch_baseline,
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


class _PerfectEngine:
    def __init__(self):
        self.started = 0

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

    def stats(self):
        return {
            "static_cache_hits": 4,
            "static_cache_misses": 2,
            "static_cache_evictions": 0,
            "static_cache_hit_rate": 4.0 / 6.0,
            "gpu_peak_memory_bytes": 0,
        }


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


def test_branch_replay_uses_macro_end_not_pc_change_for_boundaries(tmp_path):
    store = V29TraceStore(_make_cache(tmp_path))
    # A tight loop can execute the same macro PC in consecutive dynamic
    # instructions.  PC changes are therefore not valid macro delimiters.
    for core_id in store.core_ids:
        store.cores[core_id]["macro_pc"] = np.full(6, 100, dtype=np.uint64)
    baseline = replay_branch_baseline(store)
    assert baseline["branches"] == 4
    assert baseline["last_target_misses"] == 2  # first occurrence on each core


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
