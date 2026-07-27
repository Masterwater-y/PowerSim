from __future__ import annotations

import numpy as np
import pytest

from tcsim.branch_replay import ReplayConfig
from tcsim.v29.branch_features import (
    BRANCH_EVENT_NAMES,
    BRANCH_HISTORY_NAMES,
    build_core_features,
)
from tcsim.v29.contracts import (
    CHUNK_SUMMARY_NAMES,
    DYNAMIC_FIELD_NAMES,
    FIELD_INDEX,
    FIELD_NAMES,
    FIELD_SIZES,
    RELATION_FEATURE_NAMES,
    STATE_FEATURE_NAMES,
    UARCH_FEATURE_NAMES,
)


def _functional_arrays():
    n_uops = 12
    indices = np.asarray([1, 5, 8], dtype=np.uint32)
    fields = np.zeros((n_uops, len(FIELD_NAMES)), dtype=np.uint16)
    # direct conditional; taken, not-taken, taken
    fields[indices, FIELD_INDEX["branch_kind"]] = 3
    fields[indices, FIELD_INDEX["branch_taken"]] = np.asarray([2, 1, 2])
    macro_pc = np.arange(100, 100 + n_uops, dtype=np.uint64)
    return {
        "fields": fields,
        "macro_pc": macro_pc,
        "replay_branch_index": indices,
        "replay_branch_target": np.asarray([201, 0, 208], dtype=np.uint64),
        "replay_branch_next_pc": np.asarray([201, 106, 208], dtype=np.uint64),
        "replay_branch_history": np.asarray([0, 1, 2], dtype=np.uint16),
        "replay_branch_thread_id": np.asarray([0, 0, 0], dtype=np.uint16),
    }


def test_branch_features_are_predict_before_update_and_strict_prefix():
    event, history, report = build_core_features(
        _functional_arrays(), ReplayConfig(), n_uops=12, cold_prefix_branches=2,
    )
    assert event.shape == (3, len(BRANCH_EVENT_NAMES))
    assert event.dtype == np.uint8
    assert history.shape == (12, len(BRANCH_HISTORY_NAMES))
    assert history.dtype == np.uint8
    assert report["functional_history_mismatches"] == 0
    # The first taken conditional branch misses from the cold predictor, but
    # its own UOP cannot see that event in strict-prefix history.
    assert event[0, 0] == 1
    assert np.all(history[1] == 0)
    # The next UOP sees exactly one prior miss and its direct-cond kind (4).
    assert history[2, 0] == 1
    assert history[2, 1] == 1
    assert history[2, 2] == 1
    assert history[2, 3] == 1
    assert history[2, 4] == 4
    # cold_state is a per-branch ordinal feature, not a window-local reset.
    np.testing.assert_array_equal(event[:, 3], np.asarray([1, 1, 0]))


def _batch(torch, rows=2, K=6):
    fields = torch.stack([
        torch.randint(0, size, (rows, K)) for size in FIELD_SIZES
    ], dim=-1)
    tau = torch.arange(1, K + 1, dtype=torch.float32).repeat(rows, 1)
    prefix = (tau.unsqueeze(-1) <= torch.tensor([4.0, 8.0])).float()
    branch_mask = torch.tensor([[0, 1, 0, 0, 1, 0]] * rows).bool()
    event = torch.zeros(rows, K, 4, dtype=torch.long)
    event[:, 1, 0] = 1
    history = torch.zeros(rows, K, 5, dtype=torch.long)
    history[:, 2:, 0] = 1
    history[:, 2:, 2] = 1
    history[:, 2:, 3] = 1
    history[:, 2:, 4] = 4
    return {
        "per_uop_fields": fields,
        "dynamic_uop_fields": torch.zeros(
            rows, K, len(DYNAMIC_FIELD_NAMES), dtype=torch.long,
        ),
        "valid_uop_mask": torch.ones(rows, K, dtype=torch.bool),
        "chunk_summary": torch.zeros(rows, len(CHUNK_SUMMARY_NAMES)),
        "relation_features": torch.zeros(rows, len(RELATION_FEATURE_NAMES)),
        "uarch_features": torch.zeros(rows, len(UARCH_FEATURE_NAMES)),
        "state_features": torch.zeros(rows, len(STATE_FEATURE_NAMES)),
        "sample_ptr": torch.tensor([0, rows]),
        "commit_time_target": tau,
        "prefix_target": prefix,
        "progress_target": prefix.sum(dim=1),
        "branch_mask": branch_mask,
        "branch_miss_target": event[..., 0].float(),
        "branch_replay_event": event,
        "branch_replay_history": history,
        "row_sequence": torch.tensor([0, 0]),
        "row_sequence_step": torch.tensor([0, 0]),
        "core_slots": torch.tensor([0, 1]),
        "sample_period_cycles": 4.0,
        "horizons": torch.tensor([4.0, 8.0]),
    }


def _model(torch, branch_mode):
    from tcsim.v29.model import TCSimV29Model

    return TCSimV29Model(
        horizons=[4, 8], d_field=4, d_dynamic_field=2,
        d_static=16, d_dyn=16, n_heads=4, n_layers=1,
        ffn_dim=32, dropout=0.0, max_K=6, sdpa_backend="math",
        branch_mode=branch_mode, branch_field_dim=2, branch_hidden=8,
    ).eval()


def test_b1_b2_b3_remove_neural_head_and_zero_init_preserves_timing():
    torch = pytest.importorskip("torch")
    batch = _batch(torch)
    b1 = _model(torch, "headless")
    with torch.no_grad():
        baseline = b1(batch)
    assert "branch_miss_probability" not in baseline

    for mode in ("replay_event", "replay_event_history"):
        candidate = _model(torch, mode)
        incompatible = candidate.load_state_dict(b1.state_dict(), strict=False)
        assert not incompatible.unexpected_keys
        assert incompatible.missing_keys
        assert all("branch_" in key for key in incompatible.missing_keys)
        with torch.no_grad():
            output = candidate(batch)
        assert "branch_miss_probability" not in output
        torch.testing.assert_close(
            output["retirement_gap"], baseline["retirement_gap"],
            atol=0.0, rtol=0.0,
        )
        torch.testing.assert_close(
            output["commit_time"], baseline["commit_time"],
            atol=0.0, rtol=0.0,
        )


def test_headless_loss_has_zero_branch_terms_and_backpropagates_timing():
    torch = pytest.importorskip("torch")
    from tcsim.v29.losses import compute_v29_losses

    model = _model(torch, "replay_event_history").train()
    batch = _batch(torch)
    predictions = model(batch)
    losses = compute_v29_losses(
        predictions,
        batch,
        weights={"branch_token": 0.0, "branch_count": 0.0},
    )
    assert float(losses.branch_token) == 0.0
    assert float(losses.branch_count) == 0.0
    losses.total.backward()
    assert any(
        parameter.grad is not None and torch.isfinite(parameter.grad).all()
        for parameter in model.parameters()
    )
