from __future__ import annotations

import json
import os
import random

import pytest

from tcsim.v29.contracts import (
    CHUNK_SUMMARY_NAMES,
    DYNAMIC_FIELD_NAMES,
    FIELD_INDEX,
    FIELD_NAMES,
    FIELD_SIZES,
    RELATION_FEATURE_NAMES,
    RESOURCE_KEY_NAMES,
    STATE_FEATURE_NAMES,
    UARCH_FEATURE_NAMES,
)
from tcsim.v29.features import apply_window_pressure, context_features
from tcsim.v29.builder import _collection_provenance
from tcsim.v29.resource_decoder import AddrRangeSpec, Gem5AddressDecoder


def _profile():
    return {
        "cache": {
            "l1d": {"size_b": 32768, "assoc": 8, "line_b": 64, "num_banks": 1},
            "l2": {"size_b": 1048576, "assoc": 8, "line_b": 64, "num_banks": 1},
            "l3": {
                "size_b": 67108864, "assoc": 16, "line_b": 64,
                "num_banks": 8, "bank_select_low_bit": 6,
            },
        }
    }


def _write_config(
    path, *, channels=8, end=4294967296, device_size=1073741824,
):
    parts = []
    if channels <= 0 or channels & (channels - 1):
        raise ValueError("channels must be a positive power of two")
    masks = [64 << bit for bit in range((channels - 1).bit_length())]
    for channel in range(channels):
        range_value = (
            f"0:{end}:{channel}:" + ":".join(str(mask) for mask in masks)
            if masks else f"0:{end}"
        )
        parts.extend([
            f"[board.memory.mem_ctrl{channel}.dram]",
            "type=DRAMInterface",
            "addr_mapping=RoRaBaCoCh",
            "banks_per_rank=16",
            "ranks_per_channel=2",
            "burst_length=8",
            "device_bus_width=8",
            "devices_per_rank=8",
            "device_rowbuffer_size=1024",
            f"device_size={device_size}",
            f"range={range_value}",
            "",
        ])
    path.write_text("\n".join(parts), encoding="utf-8")


def test_addr_range_matches_gem5_remove_intlv_bits():
    spec = AddrRangeSpec.parse("0:4294967296:5:64:128:256")
    address = (1234 * 8 + 5) * 64
    assert spec.contains(address)
    assert spec.selector(address) == 5
    assert spec.get_offset(address) == 1234 * 64


def test_gem5_decoder_column_bank_rank_row_boundaries(tmp_path):
    config = tmp_path / "config.ini"
    _write_config(config)
    decoder = Gem5AddressDecoder.from_config(_profile(), str(config))

    def address(channel_line, channel=0):
        return (channel_line * 8 + channel) * 64

    assert decoder.decode(address(0, 7)).dram_channel == 7
    assert decoder.decode(address(127)).dram_column == 127
    decoded = decoder.decode(address(128))
    assert (decoded.dram_column, decoded.dram_bank, decoded.dram_rank, decoded.dram_row) == (
        0, 1, 0, 0,
    )
    decoded = decoder.decode(address(128 * 16))
    assert (decoded.dram_bank, decoded.dram_rank, decoded.dram_row) == (0, 1, 0)
    decoded = decoder.decode(address(128 * 16 * 2))
    assert (decoded.dram_bank, decoded.dram_rank, decoded.dram_row) == (0, 0, 1)
    assert decoder.metadata()["dram"]["bursts_per_row_buffer"] == 128
    assert decoder.metadata()["dram"]["assigned_capacity_per_channel_b"] == 536870912
    assert decoder.metadata()["dram"]["rows_per_bank"] == 2048


def test_rows_per_bank_uses_assigned_controller_range(tmp_path):
    config = tmp_path / "config.ini"
    # Deliberately make chip capacity smaller than the assigned address range.
    # gem5 warns about that mismatch but computes rowsPerBank from the latter.
    _write_config(
        config, channels=1, end=4294967296, device_size=64 * 1024 * 1024,
    )
    decoder = Gem5AddressDecoder.from_config(_profile(), str(config))
    assert decoder.metadata()["dram"]["rows_per_bank"] == 16384
    channel_line = 5000 * 128 * 16 * 2
    decoded = decoder.decode(channel_line * 64)
    assert decoded.dram_row == 5000


def test_random_decoder_matches_independent_gem5_and_ruby_formulas(tmp_path):
    config = tmp_path / "config.ini"
    _write_config(config)
    decoder = Gem5AddressDecoder.from_config(_profile(), str(config))
    generator = random.Random(20260716)
    for _ in range(10000):
        physical_line = generator.randrange(0, 4294967296 // 64)
        address = physical_line * 64
        channel = physical_line % 8
        channel_line = physical_line // 8
        column = channel_line % 128
        value = channel_line // 128
        bank = value % 16
        value //= 16
        rank = value % 2
        row = (value // 2) % 2048
        decoded = decoder.decode(address)
        assert (
            decoded.dram_channel,
            decoded.dram_rank,
            decoded.dram_bank,
            decoded.dram_row,
            decoded.dram_column,
        ) == (channel, rank, bank, row, column)
        # MESI_Three_Level Ruby controllers use line-indexed L1/L2 sets,
        # then low line bits for the LLC bank and the following bits for the
        # per-bank LLC set.
        assert decoded.l1_set == physical_line % 64
        assert decoded.l2_set == physical_line % 2048
        assert decoded.llc_bank == physical_line % 8
        assert decoded.llc_set == (physical_line // 8) % 8192


def test_nominal_local_and_resource_ids_are_structurally_absent():
    forbidden = {
        "local_pc_id", "local_line_id", "core_id", "workload_id", "trace_id",
        "l1_set", "l2_set", "llc_set", "llc_bank", "dram_channel",
        "dram_rank", "dram_bank",
    }
    assert forbidden.isdisjoint(FIELD_NAMES)


def test_ff_atomic_provenance_is_distinct_from_roi_atomic_uops(tmp_path):
    workload = tmp_path / "W_test"
    trace = workload / "tao_trace"
    trace.mkdir(parents=True)
    (workload / "collect.meta").write_text(
        "num_cores=4\nff_atomic=1\n", encoding="utf-8",
    )
    (workload / "gem5.log").write_text(
        "command line: gem5 run_mt_mvp.py --require-roi --ff-atomic\n"
        "[run_mt_mvp] first WORKBEGIN -> switch Atomic -> O3+Ruby\n",
        encoding="utf-8",
    )
    provenance = _collection_provenance(str(trace))
    assert provenance["ff_atomic_verified"] is True
    assert provenance["pre_roi_cpu"] == "AtomicSimpleCPU"
    assert provenance["roi_cpu"] == "O3+Ruby"


def _chunk(line, row, K=4):
    invalid = [-1] * len(RESOURCE_KEY_NAMES)
    keys = [
        [line, line % 64, line % 2048, 7, 1, 2, 1, 3, row, 4],
        [line + 1, (line + 1) % 64, (line + 1) % 2048, 8, 2, 2, 1, 4, row + 1, 5],
    ] + [invalid] * (K - 2)
    return {
        "read_lines": [line],
        "write_lines": [line + 1],
        "per_uop_resource_keys": keys,
        "valid_uop_mask": [1, 1] + [0] * (K - 2),
        "per_uop_access": [1, 2] + [0] * (K - 2),
        "per_uop_lines": [line, line + 1] + [-1] * (K - 2),
    }


def _relabel_chunks(chunks):
    maps = [dict() for _ in RESOURCE_KEY_NAMES]
    line_map = {}
    out = []
    for chunk in chunks:
        copied = dict(chunk)
        rows = []
        for row in chunk["per_uop_resource_keys"]:
            new = []
            for index, value in enumerate(row):
                if value < 0:
                    new.append(value)
                    continue
                mapping = maps[index]
                mapping.setdefault(value, 100000 + index * 1000 + len(mapping) * 17)
                new.append(mapping[value])
            rows.append(new)
        for value in chunk["per_uop_lines"]:
            if value >= 0:
                line_map.setdefault(value, 900000 + len(line_map) * 31)
        copied["per_uop_resource_keys"] = rows
        copied["per_uop_lines"] = [line_map.get(value, value) for value in chunk["per_uop_lines"]]
        copied["read_lines"] = [line_map[value] for value in chunk["read_lines"]]
        copied["write_lines"] = [line_map[value] for value in chunk["write_lines"]]
        out.append(copied)
    return out


def test_context_features_are_resource_relabel_invariant():
    chunks = [_chunk(10, 20), _chunk(10, 21)]
    dynamic, relation = context_features(chunks)
    relabeled_dynamic, relabeled_relation = context_features(_relabel_chunks(chunks))
    assert dynamic == relabeled_dynamic
    for original, changed in zip(relation, relabeled_relation):
        assert changed == pytest.approx(original)


def test_llc_set_contention_key_includes_llc_bank():
    left = _chunk(10, 20)
    right = _chunk(20, 30)
    # Same per-bank set number but different LLC banks must not contend.
    for row in left["per_uop_resource_keys"][:2]:
        row[3], row[4] = 7, 1
    for row in right["per_uop_resource_keys"][:2]:
        row[3], row[4] = 7, 2
    _dynamic, relation = context_features([left, right])
    same_set = RELATION_FEATURE_NAMES.index("same_llc_set_frac")
    assert relation[0][same_set] == 0.0
    assert relation[1][same_set] == 0.0

    fields = [[0] * len(FIELD_NAMES) for _ in range(2)]
    pressure = apply_window_pressure(
        fields,
        [left["per_uop_resource_keys"][0], right["per_uop_resource_keys"][0]],
        [1, 1],
    )
    index = FIELD_INDEX["llc_set_pressure"]
    assert [row[index] for row in pressure] == [1, 1]

    # Once bank and set both match, the relation and pressure must see a pair.
    for row in right["per_uop_resource_keys"][:2]:
        row[4] = 1
    _dynamic, relation = context_features([left, right])
    assert relation[0][same_set] == 1.0
    pressure = apply_window_pressure(
        fields,
        [left["per_uop_resource_keys"][0], right["per_uop_resource_keys"][0]],
        [1, 1],
    )
    assert [row[index] for row in pressure] == [2, 2]


def test_v29_model_is_monotonic_and_core_permutation_equivariant():
    torch = pytest.importorskip("torch")
    from tcsim.v29.model import TCSimV29Model

    torch.manual_seed(7)
    rows, K = 3, 8
    fields = torch.stack([
        torch.randint(0, size, (rows, K)) for size in FIELD_SIZES
    ], dim=-1)
    batch = {
        "per_uop_fields": fields,
        "dynamic_uop_fields": torch.zeros(rows, K, len(DYNAMIC_FIELD_NAMES), dtype=torch.long),
        "valid_uop_mask": torch.ones(rows, K, dtype=torch.bool),
        "chunk_summary": torch.randn(rows, len(CHUNK_SUMMARY_NAMES)),
        "relation_features": torch.randn(rows, len(RELATION_FEATURE_NAMES)),
        "uarch_features": torch.randn(rows, len(UARCH_FEATURE_NAMES)),
        "state_features": torch.randn(rows, len(STATE_FEATURE_NAMES)),
        "sample_ptr": torch.tensor([0, rows]),
    }
    model = TCSimV29Model(
        horizons=[4, 8, 16], d_field=4, d_dynamic_field=2,
        d_static=16, d_dyn=16, n_heads=4, n_layers=1,
        ffn_dim=32, dropout=0.0, max_K=K, sdpa_backend="math",
    ).eval()
    with torch.no_grad():
        original = model(batch)
    assert torch.all(original["commit_time"][:, 1:] >= original["commit_time"][:, :-1])
    permutation = torch.tensor([2, 0, 1])
    permuted = {
        key: value.index_select(0, permutation)
        if isinstance(value, torch.Tensor) and value.ndim > 0 and value.shape[0] == rows
        else value
        for key, value in batch.items()
    }
    with torch.no_grad():
        changed = model(permuted)
    inverse = torch.argsort(permutation)
    assert torch.allclose(
        original["commit_time"], changed["commit_time"].index_select(0, inverse),
        atol=2e-5, rtol=2e-5,
    )
    assert torch.allclose(
        original["branch_miss_probability"],
        changed["branch_miss_probability"].index_select(0, inverse),
        atol=2e-5, rtol=2e-5,
    )


def test_v29_loss_backward():
    torch = pytest.importorskip("torch")
    from tcsim.v29.losses import compute_v29_losses
    from tcsim.v29.model import TCSimV29Model

    rows, K, H = 2, 6, 2
    model = TCSimV29Model(
        horizons=[4, 8], d_field=4, d_dynamic_field=2,
        d_static=16, d_dyn=16, n_heads=4, n_layers=1,
        ffn_dim=32, dropout=0.0, max_K=K, sdpa_backend="math",
    )
    fields = torch.stack([
        torch.randint(0, size, (rows, K)) for size in FIELD_SIZES
    ], dim=-1)
    tau = torch.arange(1, K + 1, dtype=torch.float32).repeat(rows, 1)
    prefix = (tau.unsqueeze(-1) <= torch.tensor([4.0, 8.0])).float()
    batch = {
        "per_uop_fields": fields,
        "dynamic_uop_fields": torch.zeros(rows, K, len(DYNAMIC_FIELD_NAMES), dtype=torch.long),
        "valid_uop_mask": torch.ones(rows, K, dtype=torch.bool),
        "chunk_summary": torch.zeros(rows, len(CHUNK_SUMMARY_NAMES)),
        "relation_features": torch.zeros(rows, len(RELATION_FEATURE_NAMES)),
        "uarch_features": torch.zeros(rows, len(UARCH_FEATURE_NAMES)),
        "state_features": torch.zeros(rows, len(STATE_FEATURE_NAMES)),
        "sample_ptr": torch.tensor([0, rows]),
        "commit_time_target": tau,
        "prefix_target": prefix,
        "progress_target": prefix.sum(dim=1),
        "branch_mask": torch.tensor([[0, 1, 0, 0, 1, 0]] * rows).bool(),
        "branch_miss_target": torch.tensor([[0, 1, 0, 0, 0, 0]] * rows).float(),
        "row_sequence": torch.tensor([0, 0]),
        "row_sequence_step": torch.tensor([0, 0]),
        "core_slots": torch.tensor([0, 1]),
        "sample_period_cycles": 4.0,
        "horizons": torch.tensor([4.0, 8.0]),
    }
    predictions = model(batch)
    losses = compute_v29_losses(predictions, batch, weights={})
    losses.total.backward()
    assert torch.isfinite(losses.total)
    assert losses.monotonic_violations == 0
