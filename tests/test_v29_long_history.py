from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from tcsim.v29.contracts import (
    CHUNK_SUMMARY_NAMES,
    DYNAMIC_FIELD_NAMES,
    FIELD_INDEX,
    FIELD_SIZES,
    RELATION_FEATURE_NAMES,
    STATE_FEATURE_NAMES,
    UARCH_FEATURE_NAMES,
)
from tcsim.v29.long_history import (
    LONG_HISTORY_BASE_FEATURE_NAMES,
    LONG_HISTORY_FEATURE_NAMES,
    assemble_context_features,
    build_core_features,
    lookup_core_features,
)


def test_long_history_is_strict_prefix_and_has_expected_dimensions():
    n_uops = 1024
    lines_a = np.full(n_uops, -1, dtype=np.int64)
    pages_a = np.full(n_uops, -1, dtype=np.int64)
    memory_indices = np.arange(0, n_uops, 4)
    lines_a[memory_indices] = np.arange(len(memory_indices)) % 32
    pages_a[memory_indices] = lines_a[memory_indices] // 8
    lines_b = lines_a.copy()
    pages_b = pages_a.copy()
    # The checkpoint at cursor 256 must not consume the UOP at 256.
    lines_b[256] = 99999
    pages_b[256] = 999

    checkpoints_a, features_a = build_core_features(
        lines_a, pages_a, n_uops=n_uops, dtlb_entries=64,
    )
    checkpoints_b, features_b = build_core_features(
        lines_b, pages_b, n_uops=n_uops, dtlb_entries=64,
    )
    row_256 = int(np.flatnonzero(checkpoints_a == 256)[0])
    row_512 = int(np.flatnonzero(checkpoints_a == 512)[0])
    assert np.array_equal(checkpoints_a, checkpoints_b)
    assert np.array_equal(features_a[row_256], features_b[row_256])
    assert not np.array_equal(features_a[row_512], features_b[row_512])
    assert features_a.shape[1] == len(LONG_HISTORY_BASE_FEATURE_NAMES)


def test_long_history_lookup_never_uses_a_future_checkpoint():
    checkpoints = np.asarray([0, 256, 512], dtype=np.int64)
    features = np.stack([
        np.full(len(LONG_HISTORY_BASE_FEATURE_NAMES), value, dtype=np.float16)
        for value in (0.0, 1.0, 2.0)
    ])
    assert np.all(lookup_core_features(checkpoints, features, 255) == 0.0)
    assert np.all(lookup_core_features(checkpoints, features, 256) == 1.0)
    assert np.all(lookup_core_features(checkpoints, features, 511) == 1.0)


def test_long_history_active_core_aggregation_is_permutation_equivariant():
    base = np.zeros((3, len(LONG_HISTORY_BASE_FEATURE_NAMES)), dtype=np.float32)
    base[:, -1] = (0.1, 0.2, 0.3)
    base[:, 18:24] = np.arange(18, 36, dtype=np.float32).reshape(3, 6)
    output = assemble_context_features(base)
    permutation = np.asarray([2, 0, 1])
    permuted = assemble_context_features(base[permutation])
    assert output.shape == (3, len(LONG_HISTORY_FEATURE_NAMES))
    assert np.allclose(output[permutation], permuted)


def test_memory_gated_e2_config_is_full_joint_training_without_auxiliary_loss():
    from tcsim.utils.config import TCSimConfig

    root = Path(__file__).resolve().parents[1]
    config = TCSimConfig.load(
        str(root / "configs" / "v29_memory_gated_e2_100m.yaml")
    )
    assert config.chunk["K"] == 256
    assert config.scheduler["target_stride"] == 256
    assert config.model["long_history_dim"] == 40
    assert (
        config.model["long_history_mode"]
        == "memory_gated_timing_correction"
    )
    assert config.train["frozen_memory_probe"] is False
    assert config.train["coverage_first_sampling"] is True
    assert "memory_aux" not in config.train.get("loss_weights", {})


def test_frozen_memory_probe_is_exact_at_zero_and_only_gates_memory():
    torch = pytest.importorskip("torch")
    from tcsim.utils.config import TCSimConfig
    from tcsim.v29.inference import V29ModelRunner
    from tcsim.v29.model import (
        LONG_HISTORY_MODE_MEMORY_GATED_CORRECTION,
        TCSimV29Model,
    )
    from tcsim.v29.train import _configure_frozen_memory_probe

    rows, K, history_dim = 2, 6, 5
    common = dict(
        horizons=[4, 8],
        d_field=4,
        d_dynamic_field=2,
        d_static=16,
        d_dyn=16,
        n_heads=4,
        n_layers=1,
        ffn_dim=32,
        dropout=0.0,
        max_K=K,
        sdpa_backend="math",
    )
    base = TCSimV29Model(**common).eval()
    probe = TCSimV29Model(
        **common,
        long_history_dim=history_dim,
        long_history_mode=LONG_HISTORY_MODE_MEMORY_GATED_CORRECTION,
        memory_correction_hidden=8,
    ).eval()
    incompatible = probe.load_state_dict(base.state_dict(), strict=False)
    assert not incompatible.unexpected_keys
    assert incompatible.missing_keys
    assert all(
        name.startswith((
            "memory_history_projection.",
            "memory_correction_head.",
        ))
        for name in incompatible.missing_keys
    )

    fields = torch.zeros(rows, K, len(FIELD_SIZES), dtype=torch.long)
    fields[..., FIELD_INDEX["mem_kind"]] = torch.tensor([0, 1, 2, 3, 4, 0])
    batch = {
        "per_uop_fields": fields,
        "dynamic_uop_fields": torch.zeros(
            rows, K, len(DYNAMIC_FIELD_NAMES), dtype=torch.long,
        ),
        "valid_uop_mask": torch.ones(rows, K, dtype=torch.bool),
        "branch_mask": torch.zeros(rows, K, dtype=torch.bool),
        "chunk_summary": torch.zeros(rows, len(CHUNK_SUMMARY_NAMES)),
        "relation_features": torch.zeros(rows, len(RELATION_FEATURE_NAMES)),
        "uarch_features": torch.zeros(rows, len(UARCH_FEATURE_NAMES)),
        "state_features": torch.zeros(rows, len(STATE_FEATURE_NAMES)),
        "long_history_features": torch.randn(rows, history_dim),
        "sample_ptr": torch.tensor([0, rows]),
    }
    runner = V29ModelRunner(
        probe,
        TCSimConfig(
            chunk={}, scheduler={}, uarch={}, model={}, train={},
        ),
        {"contract": {}},
        device="cpu",
        amp_dtype="fp32",
    )
    inference_batch = runner._model_batch(batch)
    assert torch.equal(
        inference_batch["long_history_features"],
        batch["long_history_features"],
    )
    with torch.no_grad():
        expected = base(batch)
        initial = probe(batch)
    for name in (
        "retirement_gap",
        "commit_time",
        "branch_miss_logit",
        "branch_miss_probability",
    ):
        assert torch.equal(initial[name], expected[name])
    assert torch.equal(
        initial["memory_correction_mask"][0],
        torch.tensor([False, True, True, True, False, False]),
    )

    output = probe.memory_correction_head[-1]
    assert isinstance(output, torch.nn.Linear)
    with torch.no_grad():
        output.bias.fill_(0.5)
        changed = probe(batch)
    memory = initial["memory_correction_mask"]
    assert torch.equal(
        changed["retirement_gap"][~memory],
        expected["retirement_gap"][~memory],
    )
    assert torch.all(
        changed["retirement_gap"][memory] > expected["retirement_gap"][memory]
    )
    assert torch.equal(
        changed["branch_miss_probability"],
        expected["branch_miss_probability"],
    )
    probe.set_memory_correction_enabled(False)
    with torch.no_grad():
        disabled = probe(batch)
    assert torch.equal(disabled["retirement_gap"], expected["retirement_gap"])
    assert torch.equal(
        disabled["branch_miss_probability"],
        expected["branch_miss_probability"],
    )

    trainable = _configure_frozen_memory_probe(probe)
    assert trainable
    assert all(name.startswith((
        "memory_history_projection.",
        "memory_correction_head.",
    )) for name in trainable)
    assert not any(
        parameter.requires_grad
        for parameter in probe.static_encoder.parameters()
    )
    assert not any(
        parameter.requires_grad
        for parameter in probe.interaction.parameters()
    )
    assert not any(parameter.requires_grad for parameter in probe.gap_head.parameters())
    assert not any(
        parameter.requires_grad for parameter in probe.branch_head.parameters()
    )
