from dataclasses import asdict
from types import SimpleNamespace

import numpy as np
import pytest

from tcsim.v30.gss import (
    GSS_CATEGORICAL_FIELDS,
    GSS_CONTINUOUS_FIELDS,
    GSSFeatureEngine,
    GSSGeometry,
)


def _engine() -> GSSFeatureEngine:
    return GSSFeatureEngine(GSSGeometry(
        l1_sets=1,
        l1_ways=2,
        l2_sets=1,
        l2_ways=2,
        llc_sets_per_bank=1,
        llc_ways=2,
        llc_banks=1,
    ))


def _access(engine: GSSFeatureEngine, line: int, core: int = 0):
    return engine.access(
        core=core,
        physical_line=line,
        l1_set=0,
        l2_set=0,
        llc_set=0,
        llc_bank=0,
        access_kind=1,
    )


def test_gss_reports_pre_access_hit_level():
    engine = _engine()
    first = _access(engine, 10)
    second = _access(engine, 10)
    assert first.categorical[0] == 4
    assert first.categorical[4] == 1
    assert second.categorical[0] == 1
    assert second.categorical[1] == 0


def test_gss_private_l1_and_shared_llc_are_core_scoped():
    engine = _engine()
    _access(engine, 10, core=0)
    other = _access(engine, 10, core=1)
    assert other.categorical[0] == 3
    assert other.categorical[6] == 1


def test_gss_lru_and_tree_plru_evict_without_future_state():
    engine = _engine()
    _access(engine, 10)
    _access(engine, 11)
    _access(engine, 10)
    inserted = _access(engine, 12)
    assert inserted.categorical[5] == 3
    old = _access(engine, 11)
    assert old.categorical[0] == 4


def test_invalid_physical_line_does_not_mutate_state():
    engine = _engine()
    invalid = _access(engine, -1)
    assert invalid.categorical[-1] == 0
    assert engine.state_summary()["events"] == 0


def test_gss_transactional_fork_does_not_mutate_canonical_state():
    engine = _engine()
    _access(engine, 10)
    before = dict(engine.state_summary())
    shadow = engine.fork()
    first_shadow = _access(shadow, 11)
    second_shadow = _access(shadow, 11)
    assert first_shadow.categorical[0] == 4
    assert second_shadow.categorical[0] == 1
    assert dict(engine.state_summary()) == before
    canonical = _access(engine, 11)
    assert canonical.categorical[0] == 4


def test_native_gss_batch_matches_reference_and_rolls_preview_back():
    from tcsim.v30.native import NativeGSSFeatureEngine, load_native_gss

    if load_native_gss() is None:
        pytest.skip("native GSS extension is not built")
    geometry = GSSGeometry(
        l1_sets=4, l1_ways=2, l2_sets=4, l2_ways=2,
        llc_sets_per_bank=4, llc_ways=2, llc_banks=2,
    )
    reference = GSSFeatureEngine(geometry)
    native = NativeGSSFeatureEngine(geometry)
    events = np.asarray([
        [0, 10, 2, 2, 1, 0, 1],
        [1, 10, 2, 2, 1, 0, 1],
        [0, 11, 3, 3, 1, 1, 2],
        [0, 10, 2, 2, 1, 0, 1],
        [1, -1, -1, -1, -1, -1, 1],
        [1, 18, 2, 2, 1, 0, 3],
        [0, 26, 2, 2, 1, 0, 1],
    ], dtype=np.int64)
    ref_categorical, ref_continuous = reference.preview_batch(events)
    native_categorical, native_continuous = native.preview_batch(events)
    np.testing.assert_array_equal(native_categorical, ref_categorical)
    np.testing.assert_allclose(native_continuous, ref_continuous, atol=1e-7, rtol=0)
    assert native.state_summary()["events"] == 0
    native.commit_batch(events[:4])
    reference.commit_batch(events[:4])
    assert native.state_summary() == reference.state_summary()
    next_events = events[4:]
    ref_categorical, ref_continuous = reference.preview_batch(next_events)
    native_categorical, native_continuous = native.preview_batch(next_events)
    np.testing.assert_array_equal(native_categorical, ref_categorical)
    np.testing.assert_allclose(native_continuous, ref_continuous, atol=1e-7, rtol=0)


def test_serial_rollout_previews_in_shadow_and_commits_only_accepted_prefix():
    torch = pytest.importorskip("torch")
    from tcsim.v29.contracts import RESOURCE_KEY_INDEX, RESOURCE_KEY_NAMES
    from tcsim.v30.rollout import GSSSerialRollout
    from tcsim.v30.sidecar import GSS_SIDECAR_SCHEMA

    geometry = GSSGeometry(
        l1_sets=1, l1_ways=2, l2_sets=1, l2_ways=2,
        llc_sets_per_bank=1, llc_ways=2, llc_banks=1,
    )
    resources = {}
    accesses = {}
    for core in (0, 1):
        resource = np.full((4, len(RESOURCE_KEY_NAMES)), -1, dtype=np.int64)
        resource[0, RESOURCE_KEY_INDEX["physical_line"]] = 10
        for name in ("l1_set", "l2_set", "llc_set", "llc_bank"):
            resource[0, RESOURCE_KEY_INDEX[name]] = 0
        resources[core] = resource
        accesses[core] = np.asarray([1, 0, 0, 0], dtype=np.uint8)
    store = SimpleNamespace(
        trace_id="gss-rollout-test",
        K=4,
        core_ids=[0, 1],
        cores={
            core: {"resource": resources[core], "access": accesses[core]}
            for core in (0, 1)
        },
        meta={
            "resource_decoder": {
                "l1_sets": 1, "l2_sets": 1,
                "llc_sets_per_bank": 1, "llc_banks": 1,
            },
            "uarch_profile": {"cache": {
                "l1d": {"assoc": 2}, "l2": {"assoc": 2},
                "l3": {"assoc": 2},
            }},
        },
    )
    contract = {
        "schema_version": GSS_SIDECAR_SCHEMA,
        "engine_schema": "tcsim-v30-gss-cache-reference-1",
        "clock_source": "commit",
        "order_policy": "commit_tick_then_core_then_uop_v1",
        "features_are_pre_access": True,
        "timestamp_is_model_visible": False,
        "replacement": {
            "l1d": "lru", "l2": "tree_plru", "llc": "tree_plru",
        },
        "geometry": asdict(geometry),
        "categorical_fields": list(GSS_CATEGORICAL_FIELDS),
        "continuous_fields": list(GSS_CONTINUOUS_FIELDS),
        "categorical_dtype": "uint8",
        "continuous_dtype": "float16",
    }
    rollout = GSSSerialRollout(store, contract)
    context = {
        "core_slots": torch.tensor([0, 1]),
        "cursors": torch.tensor([0, 0]),
        "valid_uop_mask": torch.ones((2, 4), dtype=torch.bool),
    }
    rollout.augment_context(
        context,
        predicted_commit_time=np.asarray([
            [1.0, 2.0, 3.0, 4.0],
            [1.5, 2.5, 3.5, 4.5],
        ]),
        step_start_cycles=0.0,
    )
    categorical = context["gss_uop_categorical"].numpy()
    assert categorical[0, 0, 0] == 4  # first core: shared-state cold miss
    assert categorical[1, 0, 0] == 3  # second core: shared LLC hit in preview
    assert rollout.canonical.state_summary()["events"] == 0

    # Row/core-slot permutation cannot change a core's features.  Stable ties
    # are resolved by architectural core ID, not by transient batch position.
    permuted_rollout = GSSSerialRollout(store, contract)
    permuted_context = {
        "core_slots": torch.tensor([1, 0]),
        "cursors": torch.tensor([0, 0]),
        "valid_uop_mask": torch.ones((2, 4), dtype=torch.bool),
    }
    permuted_rollout.augment_context(
        permuted_context,
        predicted_commit_time=np.asarray([
            [1.5, 2.5, 3.5, 4.5],
            [1.0, 2.0, 3.0, 4.0],
        ]),
        step_start_cycles=0.0,
    )
    permuted = permuted_context["gss_uop_categorical"].numpy()
    assert np.array_equal(permuted[1], categorical[0])
    assert np.array_equal(permuted[0], categorical[1])

    prediction = SimpleNamespace(
        valid_uop_mask=np.ones((2, 4), dtype=np.bool_),
        commit_time=np.asarray([
            [1.0, 2.0, 3.0, 4.0],
            [1.5, 2.5, 3.5, 4.5],
        ]),
    )
    rollout.commit_context(
        context, prediction, [1, 0], step_start_cycles=0.0,
    )
    assert rollout.canonical.state_summary()["events"] == 1
    assert rollout.committed_cursors == {0: 1, 1: 0}

    next_context = {
        "core_slots": torch.tensor([0, 1]),
        "cursors": torch.tensor([1, 0]),
        "valid_uop_mask": torch.tensor([
            [1, 1, 1, 0], [1, 1, 1, 1],
        ], dtype=torch.bool),
    }
    rollout.augment_context(
        next_context,
        predicted_commit_time=np.asarray([
            [1.0, 2.0, 3.0, 0.0],
            [1.5, 2.5, 3.5, 4.5],
        ]),
        step_start_cycles=1.0,
    )
    assert next_context["gss_uop_categorical"][1, 0, 0].item() == 3
    assert rollout.stats()["gss_oracle_sidecar_consumed"] is False


def test_parallel_relaxed_rollout_previews_union_once_and_commits_prefix():
    torch = pytest.importorskip("torch")
    from tcsim.v29.contracts import RESOURCE_KEY_INDEX, RESOURCE_KEY_NAMES
    from tcsim.v30.rollout import GSSSerialRollout
    from tcsim.v30.sidecar import GSS_SIDECAR_SCHEMA

    geometry = GSSGeometry(
        l1_sets=1, l1_ways=2, l2_sets=1, l2_ways=2,
        llc_sets_per_bank=1, llc_ways=2, llc_banks=1,
    )
    resource = np.full((6, len(RESOURCE_KEY_NAMES)), -1, dtype=np.int64)
    for absolute, line in ((0, 10), (2, 10), (4, 11)):
        resource[absolute, RESOURCE_KEY_INDEX["physical_line"]] = line
        for name in ("l1_set", "l2_set", "llc_set", "llc_bank"):
            resource[absolute, RESOURCE_KEY_INDEX[name]] = 0
    store = SimpleNamespace(
        trace_id="gss-parallel-rollout-test", K=4, core_ids=[0],
        cores={0: {
            "resource": resource,
            "access": np.asarray([1, 0, 1, 0, 1, 0], dtype=np.uint8),
        }},
        meta={
            "resource_decoder": {
                "l1_sets": 1, "l2_sets": 1,
                "llc_sets_per_bank": 1, "llc_banks": 1,
            },
            "uarch_profile": {"cache": {
                "l1d": {"assoc": 2}, "l2": {"assoc": 2},
                "l3": {"assoc": 2},
            }},
        },
    )
    contract = {
        "schema_version": GSS_SIDECAR_SCHEMA,
        "engine_schema": "tcsim-v30-gss-cache-reference-1",
        "clock_source": "commit",
        "order_policy": "commit_tick_then_core_then_uop_v1",
        "features_are_pre_access": True,
        "timestamp_is_model_visible": False,
        "replacement": {
            "l1d": "lru", "l2": "tree_plru", "llc": "tree_plru",
        },
        "geometry": asdict(geometry),
        "categorical_fields": list(GSS_CATEGORICAL_FIELDS),
        "continuous_fields": list(GSS_CONTINUOUS_FIELDS),
        "categorical_dtype": "uint8",
        "continuous_dtype": "float16",
    }
    rollout = GSSSerialRollout(
        store, contract, rollout_mode="speculative",
    )
    lane0 = {
        "core_slots": torch.tensor([0]),
        "cursors": torch.tensor([0]),
        "valid_uop_mask": torch.ones((1, 4), dtype=torch.bool),
    }
    lane1 = {
        "core_slots": torch.tensor([0]),
        "cursors": torch.tensor([2]),
        "valid_uop_mask": torch.ones((1, 4), dtype=torch.bool),
    }
    rollout.augment_contexts(
        [lane0, lane1],
        anchor_cursors=[0],
        deadline_lookup=lambda core, uop: 10.0 if uop == 0 else None,
    )
    # Absolute UOP 2 is present in both lanes.  It is evaluated once in the
    # continuous union preview and both lanes receive the identical feature.
    np.testing.assert_array_equal(
        lane0["gss_uop_categorical"][0, 2].numpy(),
        lane1["gss_uop_categorical"][0, 0].numpy(),
    )
    assert lane0["gss_uop_categorical"][0, 2, 0].item() == 1
    assert rollout.canonical.state_summary()["events"] == 0
    stats = rollout.stats()
    assert stats["gss_accuracy_mode"] == "parallel-relaxed"
    assert stats["gss_parallel_preview_waves"] == 1
    assert stats["gss_preview_memory_uops"] == 3
    assert stats["gss_parallel_lane_memory_uops"] == 4
    assert stats["gss_parallel_retained_deadline_uops"] == 1
    assert stats["gss_parallel_fallback_order_uops"] == 2

    prediction = SimpleNamespace(
        valid_uop_mask=np.ones((1, 4), dtype=np.bool_),
        commit_time=np.asarray([[1.0, 2.0, 3.0, 4.0]]),
    )
    rollout.commit_step(
        [0], [0], prediction, [1], step_start_cycles=0.0,
    )
    assert rollout.canonical.state_summary()["events"] == 1
    assert rollout.committed_cursors == {0: 1}

    ready_contract = dict(contract)
    ready_contract.update({
        "clock_source": "ready",
        "order_policy": "ready_tick_then_core_then_uop_v1",
    })
    with pytest.raises(RuntimeError, match="explicit.*compatibility"):
        GSSSerialRollout(
            store, ready_contract, rollout_mode="speculative",
        )
    compatible = GSSSerialRollout(
        store,
        ready_contract,
        rollout_mode="speculative",
        allow_ready_clock_compat=True,
    )
    compatible_stats = compatible.stats()
    assert compatible_stats["gss_clock_contract_exact"] is False
    assert compatible_stats["gss_formal_accuracy_valid"] is False
    assert compatible_stats["gss_throughput_valid"] is True
    assert compatible_stats["gss_accuracy_qualification"] == (
        "exploratory-approximate"
    )


def test_parallel_relaxed_retained_deadline_uses_architectural_core_id():
    torch = pytest.importorskip("torch")
    from tcsim.v29.contracts import RESOURCE_KEY_INDEX, RESOURCE_KEY_NAMES
    from tcsim.v30.rollout import GSSSerialRollout
    from tcsim.v30.sidecar import GSS_SIDECAR_SCHEMA

    geometry = GSSGeometry(
        l1_sets=1, l1_ways=2, l2_sets=1, l2_ways=2,
        llc_sets_per_bank=1, llc_ways=2, llc_banks=1,
    )
    cores = {}
    for core_id in (9, 3):
        resource = np.full((2, len(RESOURCE_KEY_NAMES)), -1, dtype=np.int64)
        resource[0, RESOURCE_KEY_INDEX["physical_line"]] = 10
        for name in ("l1_set", "l2_set", "llc_set", "llc_bank"):
            resource[0, RESOURCE_KEY_INDEX[name]] = 0
        cores[core_id] = {
            "resource": resource,
            "access": np.asarray([1, 0], dtype=np.uint8),
        }
    store = SimpleNamespace(
        trace_id="gss-parallel-deadline-order", K=2, core_ids=[9, 3],
        cores=cores,
        meta={
            "resource_decoder": {
                "l1_sets": 1, "l2_sets": 1,
                "llc_sets_per_bank": 1, "llc_banks": 1,
            },
            "uarch_profile": {"cache": {
                "l1d": {"assoc": 2}, "l2": {"assoc": 2},
                "l3": {"assoc": 2},
            }},
        },
    )
    contract = {
        "schema_version": GSS_SIDECAR_SCHEMA,
        "engine_schema": "tcsim-v30-gss-cache-reference-1",
        "clock_source": "commit",
        "order_policy": "commit_tick_then_core_then_uop_v1",
        "features_are_pre_access": True,
        "timestamp_is_model_visible": False,
        "replacement": {
            "l1d": "lru", "l2": "tree_plru", "llc": "tree_plru",
        },
        "geometry": asdict(geometry),
        "categorical_fields": list(GSS_CATEGORICAL_FIELDS),
        "continuous_fields": list(GSS_CONTINUOUS_FIELDS),
        "categorical_dtype": "uint8",
        "continuous_dtype": "float16",
    }
    rollout = GSSSerialRollout(
        store, contract, rollout_mode="unconditional",
    )
    context = {
        "core_slots": torch.tensor([0, 1]),
        "cursors": torch.tensor([0, 0]),
        "valid_uop_mask": torch.ones((2, 2), dtype=torch.bool),
    }
    deadlines = {(9, 0): 20.0, (3, 0): 10.0}
    rollout.augment_contexts(
        [context],
        anchor_cursors=[0, 0],
        deadline_lookup=lambda core, uop: deadlines.get((core, uop)),
    )
    categorical = context["gss_uop_categorical"].numpy()
    # core 3 is slot 1 but has the earlier retained deadline, so it accesses
    # the shared line first.  Transient slot order cannot change this result.
    assert categorical[1, 0, 0] == 4
    assert categorical[0, 0, 0] == 3
    assert rollout.stats()["gss_parallel_retained_deadline_uops"] == 2


def test_staged_gss_runner_executes_full_qkvr_once():
    torch = pytest.importorskip("torch")
    from tcsim.utils.config import TCSimConfig
    from tcsim.v29.contracts import (
        CHUNK_SUMMARY_NAMES,
        DYNAMIC_FIELD_NAMES,
        FIELD_NAMES,
        RELATION_FEATURE_NAMES,
        STATE_FEATURE_NAMES,
        UARCH_FEATURE_NAMES,
    )
    from tcsim.v29.inference import V29ModelRunner
    from tcsim.v29.model import TCSimV29Model

    rows, length = 2, 6
    model = TCSimV29Model(
        horizons=[4, 8], d_field=4, d_dynamic_field=4,
        d_static=32, d_dyn=32, n_heads=4, n_layers=1, ffn_dim=64,
        max_K=length, gss_mode="causal_adapter", gss_adapter_dim=8,
        gss_adapter_heads=2, gss_field_dim=2,
    ).eval()
    runner = V29ModelRunner(
        model,
        TCSimConfig(chunk={}, scheduler={}, uarch={}, model={}, train={}),
        {"contract": {}},
        device="cpu",
        amp_dtype="fp32",
        static_cache=False,
    )
    store = SimpleNamespace(
        trace_id="single-qkvr", core_ids=[3, 7], meta={},
    )
    runner._active_trace = store.trace_id
    context = {
        "per_uop_fields": torch.zeros(
            rows, length, len(FIELD_NAMES), dtype=torch.long,
        ),
        "dynamic_uop_fields": torch.zeros(
            rows, length, len(DYNAMIC_FIELD_NAMES), dtype=torch.long,
        ),
        "valid_uop_mask": torch.ones(rows, length, dtype=torch.bool),
        "chunk_summary": torch.zeros(rows, len(CHUNK_SUMMARY_NAMES)),
        "relation_features": torch.zeros(rows, len(RELATION_FEATURE_NAMES)),
        "uarch_features": torch.zeros(rows, len(UARCH_FEATURE_NAMES)),
        "state_features": torch.zeros(rows, len(STATE_FEATURE_NAMES)),
        "branch_mask": torch.zeros(rows, length, dtype=torch.bool),
        "core_slots": torch.tensor([0, 1]),
        "cursors": torch.tensor([0, 0]),
    }

    class FakeRollout:
        calls = 0
        provisional = None

        def augment_context(
            self, target, *, predicted_commit_time, step_start_cycles,
            deadline_lookup,
        ):
            self.calls += 1
            self.provisional = np.asarray(predicted_commit_time).copy()
            assert step_start_cycles == 11.0
            assert deadline_lookup(3, 0) == 101.0
            target["gss_uop_categorical"] = torch.zeros(
                rows, length, len(GSS_CATEGORICAL_FIELDS), dtype=torch.long,
            )
            target["gss_uop_continuous"] = torch.zeros(
                rows, length, len(GSS_CONTINUOUS_FIELDS), dtype=torch.float32,
            )
            target["gss_memory_mask"] = torch.zeros(
                rows, length, dtype=torch.bool,
            )

    rollout = FakeRollout()
    interaction_calls = 0

    def count_interaction(_module, _inputs, _output):
        nonlocal interaction_calls
        interaction_calls += 1

    hook = model.interaction.register_forward_hook(count_interaction)
    try:
        prediction = runner.predict_free_gss(
            store,
            context,
            rollout,
            step_start_cycles=11.0,
            deadline_lookup=lambda core, uop: (
                101.0 if (core, uop) == (3, 0) else None
            ),
        )
    finally:
        hook.remove()
    assert interaction_calls == 1
    assert rollout.calls == 1
    assert rollout.provisional.shape == (rows, length)
    assert prediction.commit_time.shape == (rows, length)


def _adapter_inputs(torch, rows=2, K=6):
    token = torch.randn(rows, K, 16)
    categorical = torch.zeros(
        rows, K, len(GSS_CATEGORICAL_FIELDS), dtype=torch.long,
    )
    continuous = torch.zeros(
        rows, K, len(GSS_CONTINUOUS_FIELDS), dtype=torch.float32,
    )
    memory_mask = torch.zeros(rows, K, dtype=torch.bool)
    memory_mask[:, 2] = True
    categorical[:, 2, 0] = 3
    categorical[:, 2, 7] = 1
    categorical[:, 2, 8] = 1
    continuous[:, 2, 0] = 0.5
    return token, categorical, continuous, memory_mask


def test_gss_adapter_is_exactly_zero_initialized_and_first_gradient_is_isolated():
    torch = pytest.importorskip("torch")
    from tcsim.v30.model import CausalGSSResidualAdapter

    adapter = CausalGSSResidualAdapter(
        token_dim=16, adapter_dim=8, heads=2, field_dim=2, max_K=6,
    )
    inputs = _adapter_inputs(torch)
    delta = adapter(*inputs)
    assert torch.count_nonzero(delta) == 0
    delta.sum().backward()
    nonzero = {
        name for name, parameter in adapter.named_parameters()
        if parameter.grad is not None and torch.count_nonzero(parameter.grad)
    }
    assert nonzero == {"output.weight"}


def test_gss_adapter_cannot_route_future_events_to_past_tokens():
    torch = pytest.importorskip("torch")
    from tcsim.v30.model import CausalGSSResidualAdapter

    torch.manual_seed(7)
    adapter = CausalGSSResidualAdapter(
        token_dim=16, adapter_dim=8, heads=2, field_dim=2, max_K=6,
    ).eval()
    with torch.no_grad():
        adapter.output.weight.normal_(mean=0.0, std=0.1)
    token, categorical, continuous, memory_mask = _adapter_inputs(torch)
    changed_categorical = categorical.clone()
    changed_continuous = continuous.clone()
    changed_mask = memory_mask.clone()
    changed_mask[:, 4] = True
    changed_categorical[:, 4, 0] = 4
    changed_categorical[:, 4, 7] = 2
    changed_categorical[:, 4, 8] = 1
    changed_continuous[:, 4, 2] = 1.0
    with torch.no_grad():
        no_memory = adapter(
            token, categorical * 0, continuous * 0,
            torch.zeros_like(memory_mask),
        )
        before = adapter(token, categorical, continuous, memory_mask)
        after = adapter(
            token, changed_categorical, changed_continuous, changed_mask,
        )
    assert torch.count_nonzero(no_memory) == 0
    torch.testing.assert_close(before[:, :4], after[:, :4], atol=0.0, rtol=0.0)
    assert not torch.equal(before[:, 4:], after[:, 4:])


def test_gss_event_packed_attention_matches_dense_masked_attention():
    torch = pytest.importorskip("torch")
    from tcsim.v30.model import CausalGSSResidualAdapter

    torch.manual_seed(19)
    adapter = CausalGSSResidualAdapter(
        token_dim=16, adapter_dim=8, heads=2, field_dim=2, max_K=6,
    ).eval()
    with torch.no_grad():
        adapter.output.weight.normal_(mean=0.0, std=0.1)
    token, categorical, continuous, memory_mask = _adapter_inputs(torch)
    memory_mask[0, 0] = True
    categorical[0, 0, 0] = 4
    categorical[0, 0, 7] = 1
    categorical[0, 0, 8] = 1
    positions = [torch.nonzero(row, as_tuple=False).flatten() for row in memory_mask]
    width = max(
        len(value) + int(len(value) == 0 or int(value[0]) != 0)
        for value in positions
    )
    event_categorical = torch.zeros(
        len(positions), width, categorical.shape[-1], dtype=categorical.dtype,
    )
    event_continuous = torch.zeros(
        len(positions), width, continuous.shape[-1], dtype=continuous.dtype,
    )
    event_positions = torch.zeros(len(positions), width, dtype=torch.long)
    event_valid = torch.zeros(len(positions), width, dtype=torch.bool)
    event_is_memory = torch.zeros_like(event_valid)
    for row, selected in enumerate(positions):
        sentinel = int(len(selected) == 0 or int(selected[0]) != 0)
        event_valid[row, 0] = True
        destination = torch.arange(len(selected)) + sentinel
        event_categorical[row, destination] = categorical[row, selected]
        event_continuous[row, destination] = continuous[row, selected]
        event_positions[row, destination] = selected
        event_valid[row, destination] = True
        event_is_memory[row, destination] = True
    with torch.no_grad():
        dense = adapter(token, categorical, continuous, memory_mask)
        packed = adapter(
            token, categorical, continuous, memory_mask,
            event_categorical=event_categorical,
            event_continuous=event_continuous,
            event_positions=event_positions,
            event_valid=event_valid,
            event_is_memory=event_is_memory,
        )
    torch.testing.assert_close(dense, packed, atol=2e-7, rtol=1e-6)


def test_gss_mask_only_erases_content_but_preserves_causal_memory_boundary():
    torch = pytest.importorskip("torch")
    from tcsim.v30.model import (
        GSS_CONTENT_MASK_ONLY,
        CausalGSSResidualAdapter,
    )

    torch.manual_seed(11)
    adapter = CausalGSSResidualAdapter(
        token_dim=16, adapter_dim=8, heads=2, field_dim=2, max_K=6,
        content_mode=GSS_CONTENT_MASK_ONLY,
    ).eval()
    with torch.no_grad():
        adapter.output.weight.normal_(mean=0.0, std=0.1)
    token, categorical, continuous, memory_mask = _adapter_inputs(torch)
    changed_categorical = torch.randint_like(categorical, low=0, high=2)
    changed_continuous = torch.randn_like(continuous)
    with torch.no_grad():
        before = adapter(token, categorical, continuous, memory_mask)
        changed = adapter(
            token, changed_categorical, changed_continuous, memory_mask,
        )
        changed_mask = memory_mask.clone()
        changed_mask[:, 4] = True
        extra_event = adapter(
            token, changed_categorical, changed_continuous, changed_mask,
        )
        no_memory = adapter(
            token, changed_categorical, changed_continuous,
            torch.zeros_like(memory_mask),
        )
    torch.testing.assert_close(before, changed, atol=0.0, rtol=0.0)
    torch.testing.assert_close(
        before[:, :4], extra_event[:, :4], atol=0.0, rtol=0.0,
    )
    assert not torch.equal(before[:, 4:], extra_event[:, 4:])
    assert torch.count_nonzero(before[:, :2]) == 0
    assert torch.count_nonzero(before[:, 2:]) > 0
    assert torch.count_nonzero(no_memory) == 0


def test_gss_mask_only_and_g1_have_identical_parameter_shapes():
    torch = pytest.importorskip("torch")
    from tcsim.v30.model import (
        GSS_CONTENT_MASK_ONLY,
        CausalGSSResidualAdapter,
    )

    g1 = CausalGSSResidualAdapter(
        token_dim=16, adapter_dim=8, heads=2, field_dim=2, max_K=6,
    )
    control = CausalGSSResidualAdapter(
        token_dim=16, adapter_dim=8, heads=2, field_dim=2, max_K=6,
        content_mode=GSS_CONTENT_MASK_ONLY,
    )
    assert {
        name: tuple(parameter.shape)
        for name, parameter in g1.named_parameters()
    } == {
        name: tuple(parameter.shape)
        for name, parameter in control.named_parameters()
    }


def test_gss_exposure_gate_is_bounded_and_has_requested_initial_scale():
    torch = pytest.importorskip("torch")
    from tcsim.v30.model import GSSExposureGate

    gate = GSSExposureGate(
        token_dim=16, hidden_dim=8, minimum=0.25, initial=0.95,
    )
    token = torch.randn(2, 6, 16)
    delta = torch.randn_like(token)
    memory = torch.tensor([
        [0, 1, 0, 0, 1, 0],
        [1, 0, 0, 1, 0, 0],
    ], dtype=torch.bool)
    scale = gate(token, delta, memory)
    torch.testing.assert_close(
        scale, torch.full_like(scale, 0.95), atol=1.0e-6, rtol=0.0,
    )
    scale.sum().backward()
    assert gate.mlp[-1].bias.grad is not None
    assert torch.count_nonzero(gate.mlp[-1].bias.grad)
    assert float(scale.min()) >= 0.25
    assert float(scale.max()) <= 1.0


def test_gss_exposure_gate_does_not_route_future_mask_events_to_past():
    torch = pytest.importorskip("torch")
    from tcsim.v30.model import GSSExposureGate

    torch.manual_seed(17)
    gate = GSSExposureGate(
        token_dim=16, hidden_dim=8, minimum=0.25, initial=0.95,
    ).eval()
    with torch.no_grad():
        gate.mlp[-1].weight.normal_(mean=0.0, std=0.2)
    token = torch.randn(2, 6, 16)
    delta = torch.randn_like(token)
    memory = torch.zeros(2, 6, dtype=torch.bool)
    memory[:, 1] = True
    changed = memory.clone()
    changed[:, 4] = True
    with torch.no_grad():
        before = gate(token, delta, memory)
        after = gate(token, delta, changed)
    torch.testing.assert_close(before[:, :4], after[:, :4], atol=0.0, rtol=0.0)
    assert not torch.equal(before[:, 4:], after[:, 4:])


def test_gss_strength_router_has_conservative_three_anchor_initialization():
    torch = pytest.importorskip("torch")
    from tcsim.v30.model import GSSStrengthRouter

    router = GSSStrengthRouter(token_dim=16, hidden_dim=8)
    token = torch.randn(2, 6, 16)
    delta = torch.randn_like(token)
    memory = torch.zeros(2, 6, dtype=torch.bool)
    anchors = torch.rand(2, 6, 3)
    logits, weights = router(token, delta, memory, anchors)
    assert logits.shape == (2, 6, 3)
    torch.testing.assert_close(
        weights,
        torch.tensor([0.05, 0.90, 0.05]).view(1, 1, 3).expand_as(weights),
        atol=1.0e-6,
        rtol=0.0,
    )
    weights.sum().backward()
    assert router.mlp[-1].bias.grad is not None


def test_gss_strength_router_uses_strict_prefix_memory_density():
    torch = pytest.importorskip("torch")
    from tcsim.v30.model import GSSStrengthRouter

    torch.manual_seed(23)
    router = GSSStrengthRouter(token_dim=16, hidden_dim=8).eval()
    with torch.no_grad():
        router.mlp[-1].weight.normal_(mean=0.0, std=0.2)
    token = torch.randn(2, 6, 16)
    delta = torch.randn_like(token)
    anchors = torch.rand(2, 6, 3)
    memory = torch.zeros(2, 6, dtype=torch.bool)
    memory[:, 1] = True
    changed = memory.clone()
    changed[:, 4] = True
    with torch.no_grad():
        _before_logits, before = router(token, delta, memory, anchors)
        _after_logits, after = router(token, delta, changed, anchors)
    # The event at position four is not visible to its own router decision.
    torch.testing.assert_close(before[:, :5], after[:, :5], atol=0.0, rtol=0.0)
    assert not torch.equal(before[:, 5:], after[:, 5:])


def test_frozen_gss_gate_probe_exposes_only_gate_parameters():
    torch = pytest.importorskip("torch")
    from tcsim.v29.model import TCSimV29Model
    from tcsim.v29.train import _configure_frozen_gss_gate_probe

    model = TCSimV29Model(
        horizons=[16, 32], d_field=4, d_dynamic_field=4,
        d_static=32, d_dyn=32, n_heads=4, n_layers=1, ffn_dim=64,
        max_K=6, gss_mode="causal_adapter", gss_adapter_dim=8,
        gss_adapter_heads=2, gss_field_dim=2, gss_exposure_gate=True,
        gss_exposure_gate_hidden=8,
    )
    names = _configure_frozen_gss_gate_probe(model)
    assert names
    assert all(name.startswith("gss_exposure_gate.") for name in names)
    assert all(
        parameter.requires_grad == name.startswith("gss_exposure_gate.")
        for name, parameter in model.named_parameters()
    )


def test_joint_gss_v2_optimizer_stages_cover_the_full_timing_model():
    torch = pytest.importorskip("torch")
    from types import SimpleNamespace
    from tcsim.v29.model import TCSimV29Model
    from tcsim.v29.train import (
        _configure_joint_gss_v2,
        _joint_v2_optimizer_groups,
        _update_joint_v2_learning_rates,
    )

    model = TCSimV29Model(
        horizons=[16, 32], d_field=4, d_dynamic_field=4,
        d_static=32, d_dyn=32, n_heads=4, n_layers=3, ffn_dim=64,
        max_K=6, gss_mode="causal_adapter", gss_adapter_dim=8,
        gss_adapter_heads=2, gss_field_dim=2, gss_strength_router=True,
        gss_strength_router_hidden=8,
    )
    config = SimpleNamespace(train={
        "joint_v2_gate_only_steps": 2,
        "joint_v2_partial_steps": 8,
        "joint_v2_last_layers": 2,
        "joint_v2_warmup_steps": 1,
        "joint_v2_target_steps": 60,
    })
    _configure_joint_gss_v2(model)
    groups = _joint_v2_optimizer_groups(model, config)
    optimizer = torch.optim.AdamW(groups)
    at_one = _update_joint_v2_learning_rates(optimizer, 1, config, 45)
    assert at_one["router"] > 0.0
    assert all(
        value == 0.0 for name, value in at_one.items() if name != "router"
    )
    at_three = _update_joint_v2_learning_rates(optimizer, 3, config, 45)
    assert at_three["adapter"] > 0.0
    assert at_three["timing_head"] > 0.0
    assert at_three["last_qkvr"] > 0.0
    assert at_three["base_qkvr"] == 0.0
    at_nine = _update_joint_v2_learning_rates(optimizer, 9, config, 45)
    assert at_nine["base_qkvr"] > 0.0
    assert at_nine["static"] > 0.0


def test_joint_gss_v2_auxiliary_loss_is_ground_truth_only():
    torch = pytest.importorskip("torch")
    from types import SimpleNamespace
    from tcsim.v29.train import _joint_v2_auxiliary_losses

    true_time = torch.tensor([[1.0, 3.0, 6.0, 10.0]])
    anchors = torch.tensor([[[
        [1.0, 1.2, 1.5],
        [2.0, 1.8, 1.6],
        [3.0, 2.8, 2.4],
        [4.0, 4.2, 4.6],
    ]]])
    anchors = anchors.reshape(1, 4, 3)
    anchor_time = anchors.cumsum(dim=1)
    logits = torch.zeros(1, 4, 3, requires_grad=True)
    predictions = {
        "retirement_gap": anchors[..., 1],
        "commit_time": anchor_time[..., 1],
        "gss_anchor_gaps": anchors,
        "gss_anchor_commit_time": anchor_time,
        "gss_router_logits": logits,
        "gss_router_weights": logits.softmax(dim=-1),
    }
    batch = {
        "valid_uop_mask": torch.ones(1, 4, dtype=torch.bool),
        "commit_time_target": true_time,
    }
    config = SimpleNamespace(train={
        "joint_v2_gap_huber_beta": 0.2,
        "joint_v2_rank_local_fraction": 0.7,
        "joint_v2_rank_margin": 0.01,
        "joint_v2_rank_temperature": 0.05,
    })
    losses = _joint_v2_auxiliary_losses(predictions, batch, config)
    assert set(losses) == {
        "local_gap", "counterfactual_rank", "router_entropy",
        "decisive_fraction",
    }
    total = losses["local_gap"] + losses["counterfactual_rank"]
    total.backward()
    assert logits.grad is not None
