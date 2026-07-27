from __future__ import annotations

import json
import os
import random

import pytest
import numpy as np

from tcsim.v29.contracts import (
    CHUNK_SUMMARY_NAMES,
    DYNAMIC_FIELD_NAMES,
    FIELD_INDEX,
    FIELD_NAMES,
    FIELD_SIZES,
    RELATION_FEATURE_NAMES,
    RESOURCE_KEY_NAMES,
    RESOURCE_KEY_INDEX,
    RESOURCE_COMPACT_INDEX,
    RESOURCE_COMPACT_NAMES,
    STATE_FEATURE_NAMES,
    UARCH_FEATURE_NAMES,
)
from tcsim.v29.dataset import (
    _apply_window_pressure_numpy,
    _context_features_numpy,
    _summarize_window_numpy,
)
from tcsim.v29.features import (
    apply_window_pressure,
    context_features,
    summarize_window,
)
from tcsim.v29 import builder as v29_builder
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


def test_numpy_window_pressure_and_summary_match_legacy_exactly():
    generator = np.random.default_rng(20260717)
    K = 256
    for n_valid in (1, 17, K):
        valid = np.zeros(K, dtype=np.uint8)
        valid[:n_valid] = 1
        fields = generator.integers(
            0, 10, size=(K, len(FIELD_NAMES)), dtype=np.int64,
        )
        resources = generator.integers(
            -1, 12, size=(K, len(RESOURCE_KEY_NAMES)), dtype=np.int64,
        )
        for name in ("physical_line", "l1_set", "l2_set", "llc_set"):
            resources[:n_valid, RESOURCE_KEY_INDEX[name]] += 4
        semantic = generator.integers(0, 256, size=K, dtype=np.uint8)
        functional_lines = generator.integers(-1, 32, size=K, dtype=np.int64)
        functional_pages = generator.integers(-1, 8, size=K, dtype=np.int64)
        producer_logs = generator.random(K, dtype=np.float32)
        macro_pcs = generator.integers(0, 64, size=K, dtype=np.uint64)
        macro_end = generator.integers(0, 2, size=K, dtype=np.uint8)

        legacy_fields = apply_window_pressure(
            fields.tolist(), resources.tolist(), valid.tolist(),
        )
        vector_fields = _apply_window_pressure_numpy(fields, resources, valid)
        np.testing.assert_array_equal(vector_fields, np.asarray(legacy_fields))
        # The default compatibility path must not mutate its caller, while
        # the deployment window builder can reuse its freshly allocated
        # staging buffer and avoid a second K x field copy.
        in_place_fields = fields.copy()
        in_place_result = _apply_window_pressure_numpy(
            in_place_fields, resources, valid, copy=False,
        )
        assert np.shares_memory(in_place_result, in_place_fields)
        np.testing.assert_array_equal(in_place_result, vector_fields)

        chunk = {
            "valid_uop_mask": valid.tolist(),
            "per_uop_fields": legacy_fields,
            "per_uop_resource_keys": resources.tolist(),
            "semantic_flags": semantic.tolist(),
            "functional_lines": functional_lines.tolist(),
            "functional_pages": functional_pages.tolist(),
            "producer_logs": producer_logs.tolist(),
            "macro_pcs": macro_pcs.tolist(),
            "macro_end": macro_end.tolist(),
        }
        legacy_summary = summarize_window(chunk, K)
        vector_summary = _summarize_window_numpy(
            vector_fields,
            resources,
            valid,
            semantic,
            functional_lines,
            functional_pages,
            producer_logs,
            macro_pcs,
            macro_end,
            K,
        )
        np.testing.assert_allclose(
            vector_summary, legacy_summary, rtol=0.0, atol=1.0e-12,
        )


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


def test_builder_keeps_all_retired_control_uops_inside_one_macro(
    tmp_path, monkeypatch,
):
    """x86 IDIV can retire a microcode loop with many control UOPs."""
    rows = []
    macro_pc = 0x402143
    for ordinal in range(8):
        taken = ordinal < 7
        rows.append({
            "core_id": 0,
            "macro_pc": macro_pc,
            "micro_pc": 7,
            "vaddr": 0,
            "paddr": 0,
            "cacheline_addr": 0,
            "cacheline_paddr": 0,
            "size": 0,
            "is_load": 0,
            "is_store": 0,
            "is_atomic": 0,
            "is_branch": 1,
            "is_branch_cond": 1,
            "is_branch_indirect": 0,
            "is_call": 0,
            "is_return": 0,
            "branch_taken": int(taken),
            "branch_target": macro_pc if taken else 0,
            "branch_next_pc": macro_pc,
            "branch_history": (1 << ordinal) - 1,
            "is_int": 1,
            "is_fp": 0,
            "is_simd": 0,
            "is_serialize": 0,
            "op_class": 1,
            "is_microop": 1,
            "is_last_microop": 0,
            "n_src": 1,
            "n_dst": 1,
            "producer_dists": [1, 0, 0, 0],
            "producer_classes": [1, 255, 255, 255],
            "commit_tick": ordinal + 1,
            "mispredicted": int(taken),
        })
    rows.append({
        **rows[-1],
        "micro_pc": 9,
        "is_branch": 0,
        "is_branch_cond": 0,
        "branch_taken": 0,
        "branch_target": 0,
        "branch_next_pc": 0,
        "is_last_microop": 1,
        "commit_tick": 9,
        "mispredicted": 0,
    })
    monkeypatch.setattr(
        v29_builder, "_parquet_rows",
        lambda _path, *, include_oracle: len(rows),
    )
    monkeypatch.setattr(
        v29_builder, "_iter_aligned_rows", lambda _path: iter(rows),
    )
    core_dir = tmp_path / "core0"
    result = v29_builder._build_core(
        core_id=0,
        aligned_path="synthetic-idiv.parquet",
        core_dir=str(core_dir),
        profile={},
        decoder=object(),
        roi_begin_tick=0,
        roi_end_tick=100,
        include_oracle=True,
    )
    assert result["n_uops"] == 9
    assert result["n_macros"] == 1
    assert result["n_branches"] == 8
    assert result["n_branch_misses"] == 7
    assert int(np.load(core_dir / "branch.npy").sum()) == 8
    assert int(np.load(core_dir / "branch_miss.npy").sum()) == 7
    np.testing.assert_array_equal(
        np.load(core_dir / "replay_branch_index.npy"), np.arange(8)
    )
    np.testing.assert_array_equal(
        np.load(core_dir / "replay_branch_target.npy"),
        np.asarray([macro_pc] * 7 + [0], dtype=np.uint64),
    )
    np.testing.assert_array_equal(
        np.load(core_dir / "replay_branch_next_pc.npy"),
        np.asarray([macro_pc] * 8, dtype=np.uint64),
    )
    assert int(np.load(core_dir / "replay_branch_thread_id.npy").sum()) == 0


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


def test_numpy_cross_core_features_match_legacy_exactly():
    generator = np.random.default_rng(20260718)
    K = 32
    for n_active in (1, 2, 8, 32):
        chunks = []
        for core in range(n_active):
            n_valid = int(generator.integers(1, K + 1))
            valid = np.zeros(K, dtype=np.uint8)
            valid[:n_valid] = 1
            kinds = generator.integers(0, 4, size=K, dtype=np.uint8)
            lines = generator.integers(-1, 16, size=K, dtype=np.int64)
            resources = generator.integers(
                -1, 10, size=(K, len(RESOURCE_KEY_NAMES)), dtype=np.int64,
            )
            resources[:, 0] = lines
            read_mask = (
                valid.astype(bool) & (lines >= 0)
                & ((kinds == 1) | (kinds == 3))
            )
            write_mask = (
                valid.astype(bool) & (lines >= 0)
                & ((kinds == 2) | (kinds == 3))
            )
            chunks.append({
                "read_lines": np.unique(lines[read_mask]).tolist(),
                "write_lines": np.unique(lines[write_mask]).tolist(),
                "per_uop_resource_keys": resources.tolist(),
                "valid_uop_mask": valid.tolist(),
                "per_uop_access": kinds.tolist(),
                "per_uop_lines": lines.tolist(),
                "_numpy": {
                    "resource": resources,
                    "valid_uop_mask": valid.astype(bool),
                    "access": kinds,
                    "physical_line": lines,
                },
            })
        legacy_dynamic, legacy_relations = context_features(chunks)
        vector_dynamic, vector_relations = _context_features_numpy(chunks)
        np.testing.assert_array_equal(vector_dynamic, legacy_dynamic)
        np.testing.assert_allclose(
            vector_relations, legacy_relations, rtol=0.0, atol=1.0e-15,
        )


def test_offline_compact_resource_ids_preserve_context_exactly():
    generator = np.random.default_rng(20260722)
    K = 32
    chunks = []
    for core in range(8):
        n_valid = int(generator.integers(8, K + 1))
        valid = np.arange(K) < n_valid
        kinds = generator.integers(0, 4, size=K, dtype=np.uint8)
        lines = generator.integers(-1, 16, size=K, dtype=np.int64)
        resources = generator.integers(
            0, 12, size=(K, len(RESOURCE_KEY_NAMES)), dtype=np.int64,
        )
        resources[:, RESOURCE_KEY_INDEX["physical_line"]] = lines
        resources[~valid] = -1
        # Exercise the hierarchy edge case: a bank-valid access without a
        # row may carry a larger bank coordinate than every row-valid access.
        resources[0, RESOURCE_KEY_INDEX["dram_channel"]] = 31
        resources[0, RESOURCE_KEY_INDEX["dram_row"]] = -1
        chunks.append({
            "_numpy": {
                "resource": resources,
                "valid_uop_mask": valid,
                "access": kinds,
                "physical_line": lines,
            },
        })

    specs = {
        "llc_set": ("llc_bank", "llc_set"),
        "dram_bank": ("dram_channel", "dram_rank", "dram_bank"),
        "dram_row": (
            "dram_channel", "dram_rank", "dram_bank", "dram_row",
        ),
    }
    maxima = {
        name: np.zeros(len(columns), dtype=np.int64)
        for name, columns in specs.items()
    }
    for chunk in chunks:
        resources = chunk["_numpy"]["resource"]
        for name, columns in specs.items():
            indices = [RESOURCE_KEY_INDEX[column] for column in columns]
            values = resources[:, indices]
            selected = np.all(values >= 0, axis=1)
            if np.any(selected):
                maxima[name] = np.maximum(
                    maxima[name], values[selected].max(axis=0),
                )
    maxima["dram_row"][:3] = maxima["dram_bank"]
    radices = {
        name: [int(value) + 1 for value in maxima[name]]
        for name in RESOURCE_COMPACT_NAMES
    }
    for chunk in chunks:
        resources = chunk["_numpy"]["resource"]
        compact = np.empty((K, len(RESOURCE_COMPACT_NAMES)), dtype=np.uint32)
        for name, columns in specs.items():
            indices = [RESOURCE_KEY_INDEX[column] for column in columns]
            compact[:, RESOURCE_COMPACT_INDEX[name]] = (
                v29_builder._exact_compact_codes(
                    resources[:, indices], maxima[name],
                )
            )
        chunk["_numpy"]["resource_compact"] = compact

    tuple_chunks = []
    for chunk in chunks:
        copied = {"_numpy": dict(chunk["_numpy"])}
        copied["_numpy"].pop("resource_compact")
        tuple_chunks.append(copied)
    tuple_dynamic, tuple_relations = _context_features_numpy(tuple_chunks)
    compact_dynamic, compact_relations = _context_features_numpy(
        chunks, resource_radices=radices,
    )
    np.testing.assert_array_equal(compact_dynamic, tuple_dynamic)
    np.testing.assert_array_equal(compact_relations, tuple_relations)


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
        static_tokens = model.static_encoder(batch["per_uop_fields"])
        free_output = model.forward_from_static(
            batch,
            static_tokens,
            include_horizon_outputs=False,
        )
    assert torch.all(original["commit_time"][:, 1:] >= original["commit_time"][:, :-1])
    assert "commit_probability" not in free_output
    assert "progress" not in free_output
    assert "hard_prefix" not in free_output
    assert torch.allclose(original["commit_time"], free_output["commit_time"])
    assert torch.allclose(
        original["branch_miss_probability"],
        free_output["branch_miss_probability"],
    )
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


def test_v29_prefix_sum_preserves_monotonicity_below_one_fp32_ulp():
    torch = pytest.importorskip("torch")
    from tcsim.v29.model import _monotonic_prefix_sum

    # Values captured from the stride=256 failure: at tau~=315.75 the next
    # positive gap is about 100x smaller than one FP32 ULP.  The represented
    # prefixes may be equal, but must never move backwards.
    gaps = torch.tensor(
        [[315.752747, 3.21974028e-7, 1.0]], dtype=torch.float32,
        requires_grad=True,
    )
    prefix = _monotonic_prefix_sum(gaps)
    assert prefix.dtype == torch.float32
    assert torch.all(prefix[:, 1:] >= prefix[:, :-1])
    prefix.sum().backward()
    assert torch.isfinite(gaps.grad).all()


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
