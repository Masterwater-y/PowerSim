"""Functional-only, permutation-invariant v29 feature construction."""
from __future__ import annotations

import copy
import hashlib
import math
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..chunker import functional_features as v28
from .contracts import (
    BRANCH_FIELD_NAMES,
    CHUNK_SUMMARY_NAMES,
    DYNAMIC_FIELD_NAMES,
    DYNAMIC_PAD_IDS,
    FIELD_INDEX,
    FIELD_NAMES,
    FIELD_PAD_IDS,
    RELATION_FEATURE_NAMES,
    RESOURCE_FIELD_NAMES,
    RESOURCE_KEY_INDEX,
    RESOURCE_KEY_INVALID,
    RESOURCE_KEY_NAMES,
    UARCH_FEATURE_NAMES,
)
from .resource_decoder import Gem5AddressDecoder, build_decoder_for_trace


def _nested(profile: Mapping[str, Any], path: Sequence[str], default: float = 0.0) -> float:
    value: Any = profile
    for key in path:
        if not isinstance(value, Mapping):
            return float(default)
        value = value.get(key)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _log2p(value: float) -> float:
    return math.log2(max(1.0, float(value)))


def _log_bucket(value: int, maximum: int) -> int:
    value = max(0, int(value))
    return 0 if value <= 0 else min(int(maximum), 1 + int(math.log2(value)))


def _reuse_bucket(distance: Optional[int], seen_before: bool) -> int:
    if distance is None:
        return 8 if seen_before else 1
    if distance <= 8:
        return 2
    if distance <= 64:
        return 3
    if distance <= 512:
        return 4
    if distance <= 4096:
        return 5
    if distance <= 32768:
        return 6
    if distance <= 262144:
        return 7
    return 8


def _fanout_bucket(other_cores: int) -> int:
    if int(other_cores) <= 0:
        return 1
    return 1 + min(6, int(math.ceil(math.log2(int(other_cores) + 1))))


def _hhi(values: Sequence[int]) -> float:
    if not values:
        return 0.0
    counts = Counter(int(value) for value in values)
    total = float(len(values))
    return sum((count / total) ** 2 for count in counts.values())


def _physical_address(rec: Mapping[str, Any]) -> Optional[int]:
    paddr = int(rec.get("paddr", 0) or 0)
    if paddr:
        return paddr
    cacheline = int(rec.get("cacheline_paddr", 0) or 0)
    return cacheline if cacheline else None


class PhysicalResourceMapperV29:
    """Decode resources exactly while exposing no nominal IDs to the model."""

    def __init__(self, decoder: Gem5AddressDecoder) -> None:
        self.decoder = decoder
        self._mem_position = 0
        self._row_position: Dict[Tuple[int, int, int, int], int] = {}

    def encode(self, rec: Mapping[str, Any]) -> Tuple[List[int], List[int]]:
        if not v28.is_mem(rec):
            return (
                [0] * len(RESOURCE_FIELD_NAMES),
                [RESOURCE_KEY_INVALID] * len(RESOURCE_KEY_NAMES),
            )
        address = _physical_address(rec)
        if address is None:
            return (
                [1] + [0] * (len(RESOURCE_FIELD_NAMES) - 1),
                [RESOURCE_KEY_INVALID] * len(RESOURCE_KEY_NAMES),
            )
        decoded = self.decoder.decode(address)
        self._mem_position += 1
        row_key = (
            decoded.dram_channel,
            decoded.dram_rank,
            decoded.dram_bank,
            decoded.dram_row,
        )
        previous = self._row_position.get(row_key)
        row_reuse = _reuse_bucket(
            None if previous is None else self._mem_position - previous,
            previous is not None,
        )
        self._row_position[row_key] = self._mem_position
        fields = [2, row_reuse, 0, 0, 0]
        keys = [
            decoded.physical_line,
            decoded.l1_set,
            decoded.l2_set,
            decoded.llc_set,
            decoded.llc_bank,
            decoded.dram_channel,
            decoded.dram_rank,
            decoded.dram_bank,
            decoded.dram_row,
            decoded.dram_column,
        ]
        return fields, keys


@dataclass
class FunctionalFeatureEncoderV29:
    """Stateful program-order encoder with structural ID invariance."""

    profile: Mapping[str, Any]
    decoder: Gem5AddressDecoder

    def __post_init__(self) -> None:
        self._base = v28.FunctionalFeatureEncoder(
            uarch_profile=self.profile,
            resource_seed="unused-v29",
        )
        self._base.resource_mapper = PhysicalResourceMapperV29(self.decoder)
        self._branch_index = 0
        self._last_pc: Dict[int, int] = {}
        self._last_target: Dict[int, int] = {}
        self._last_predictor_index: Dict[int, Tuple[int, int]] = {}
        predictor = dict(self.profile.get("branch_predictor", {}) or {})
        conditional = dict(predictor.get("conditionalBranchPred", {}) or {})
        self._predictor_entries = max(1, int(
            conditional.get(
                "localhistorytablesize",
                conditional.get("localpredictorsize", 2048),
            )
        ) )
        self._predictor_shift = max(0, int(conditional.get("instshiftamt", 0)))
        ras = dict(predictor.get("ras", {}) or {})
        self._ras_capacity = max(1, int(ras.get("numentries", 16)))
        self._ras_depth = 0

    def _branch_context(self, rec: Mapping[str, Any]) -> List[int]:
        if not int(rec.get("is_branch", 0) or 0):
            return [0, 0, 0, 0]
        self._branch_index += 1
        pc = int(rec.get("macro_pc", rec.get("micro_pc", 0)) or 0)
        target = int(rec.get("branch_next_pc", 0) or 0)
        pc_previous = self._last_pc.get(pc)
        target_previous = self._last_target.get(target) if target else None
        pc_reuse = _reuse_bucket(
            None if pc_previous is None else self._branch_index - pc_previous,
            pc_previous is not None,
        )
        target_reuse = _reuse_bucket(
            None if target_previous is None else self._branch_index - target_previous,
            target_previous is not None,
        ) if target else 0
        predictor_index = (pc >> self._predictor_shift) % self._predictor_entries
        prior = self._last_predictor_index.get(predictor_index)
        if prior is None:
            alias = 1
        elif prior[0] == pc:
            alias = 2
        else:
            distance = self._branch_index - prior[1]
            alias = min(9, 3 + int(math.log2(max(1, distance))))
        ras_depth = 1 + min(self._ras_capacity, self._ras_depth)
        self._last_pc[pc] = self._branch_index
        if target:
            self._last_target[target] = self._branch_index
        self._last_predictor_index[predictor_index] = (pc, self._branch_index)
        if int(rec.get("is_call", 0) or 0):
            self._ras_depth = min(self._ras_capacity, self._ras_depth + 1)
        elif int(rec.get("is_return", 0) or 0):
            self._ras_depth = max(0, self._ras_depth - 1)
        return [pc_reuse, alias, target_reuse, ras_depth]

    def encode(self, rec: Mapping[str, Any]) -> Tuple[List[int], float, List[int]]:
        branch_context = self._branch_context(rec)
        old_fields, producer_log, resource_keys = self._base.encode(rec)
        base = [
            int(old_fields[v28.FIELD_INDEX[name]])
            for name in (
                "op_class", "reg_dependency", "mem_kind", "producer_distance",
                "reuse_distance", "stride", "macro_position", "same_core_history",
                "mem_size", "line_offset", "recent_ws_short", "recent_ws_long",
            )
        ]
        branch = [
            int(old_fields[v28.FIELD_INDEX[name]])
            for name in (
                "branch_kind", "branch_taken", "branch_successor_delta",
                "branch_history_low8", "branch_history_high8",
            )
        ] + branch_context
        resource_start = len(v28.BASE_FIELD_NAMES) + len(v28.BRANCH_FIELD_NAMES)
        resource = [int(value) for value in old_fields[resource_start:]]
        fields = base + branch + resource
        if len(fields) != len(FIELD_NAMES) or len(resource_keys) != len(RESOURCE_KEY_NAMES):
            raise RuntimeError("v29 feature encoder dimension mismatch")
        return fields, float(producer_log), [int(value) for value in resource_keys]


def load_trace_profile(
    trace_dir: str,
) -> Tuple[Dict[str, Any], Gem5AddressDecoder]:
    base = v28.load_uarch_profile(trace_dir)
    decoder = build_decoder_for_trace(trace_dir, base)
    return decoder.enriched_profile(), decoder


def predictor_hash(profile: Mapping[str, Any]) -> str:
    return v28.predictor_hash(profile)


def uarch_hash(profile: Mapping[str, Any], include_topology: bool = False) -> str:
    normalized = copy.deepcopy(dict(profile))
    if not include_topology and isinstance(normalized.get("core"), dict):
        normalized["core"].pop("num_cores", None)
    blob = repr(sorted(_flatten(normalized).items())).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()


def _flatten(value: Any, prefix: str = "") -> Dict[str, Any]:
    if isinstance(value, Mapping):
        out: Dict[str, Any] = {}
        for key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
            child = f"{prefix}.{key}" if prefix else str(key)
            out.update(_flatten(item, child))
        return out
    if isinstance(value, (list, tuple)):
        return {prefix: tuple(value)}
    return {prefix: value}


def uarch_vector(profile: Mapping[str, Any]) -> List[float]:
    p = profile
    values = [
        _nested(p, ("core", "freq_ghz")),
        _log2p(_nested(p, ("core", "num_cores"), 1)),
        _log2p(_nested(p, ("core", "fetch_width"), 1)),
        _log2p(_nested(p, ("core", "decode_width"), 1)),
        _log2p(_nested(p, ("core", "issue_width"), 1)),
        _log2p(_nested(p, ("core", "commit_width"), 1)),
        _log2p(_nested(p, ("core", "rob_entries"), 1)),
        _log2p(_nested(p, ("core", "iq_entries"), 1)),
        _log2p(_nested(p, ("core", "lq_entries"), 1)),
        _log2p(_nested(p, ("core", "sq_entries"), 1)),
        _log2p(_nested(p, ("cache", "l1d", "size_b"), 1)),
        _log2p(_nested(p, ("cache", "l1d", "assoc"), 1)),
        _log2p(_nested(p, ("cache", "l2", "size_b"), 1)),
        _log2p(_nested(p, ("cache", "l2", "assoc"), 1)),
        _log2p(_nested(p, ("cache", "l3", "size_b"), 1)),
        _log2p(_nested(p, ("cache", "l3", "assoc"), 1)),
        _log2p(_nested(p, ("cache", "l3", "num_banks"), 1)),
        _log2p(_nested(p, ("tlb", "dtlb", "entries"), 1)),
        _log2p(_nested(p, ("tlb", "dtlb", "assoc"), 1)),
        _log2p(_nested(p, ("tlb", "itlb", "entries"), 1)),
        _log2p(_nested(p, ("tlb", "itlb", "assoc"), 1)),
        _log2p(_nested(p, ("mshr", "l1d_entries"), 1)),
        _log2p(_nested(p, ("mshr", "l2_entries"), 1)),
        _log2p(_nested(p, ("mshr", "l3_entries"), 1)),
        _log2p(_nested(p, ("dram", "num_channels"), 1)),
        _log2p(_nested(p, ("dram", "banks_per_rank"), 1)),
        _log2p(_nested(p, ("dram", "ranks_per_channel"), 1)),
        _log2p(_nested(p, ("dram", "row_buffer_size_b"), 1)),
        _log2p(_nested(p, ("dram", "burst_size_b"), 1)),
    ]
    if len(values) != len(UARCH_FEATURE_NAMES):
        raise RuntimeError("v29 uarch vector dimension mismatch")
    return values


def tick_per_cycle(profile: Mapping[str, Any]) -> float:
    return v28.tick_per_cycle_from_profile(profile)


def apply_window_pressure(
    fields: Sequence[Sequence[int]],
    resource_keys: Sequence[Sequence[int]],
    valid_mask: Sequence[int],
) -> List[List[int]]:
    out = [list(map(int, row)) for row in fields]
    for key_name, field_name in (
        ("l1_set", "l1_set_pressure"),
        ("l2_set", "l2_set_pressure"),
        ("llc_set", "llc_set_pressure"),
    ):
        key_index = RESOURCE_KEY_INDEX[key_name]
        counts = Counter(
            int(keys[key_index]) for keys, valid in zip(resource_keys, valid_mask)
            if valid and int(keys[key_index]) >= 0
        )
        field_index = FIELD_INDEX[field_name]
        for row, keys, valid in zip(out, resource_keys, valid_mask):
            key = int(keys[key_index])
            row[field_index] = (
                min(9, 1 + int(math.log2(counts[key])))
                if valid and key >= 0 else 0
            )
    return out


def summarize_window(chunk: Mapping[str, Any], K: int) -> List[float]:
    """Build the 38 semantic summaries from a packed lookahead window."""
    mask = [bool(value) for value in chunk["valid_uop_mask"]]
    fields = chunk["per_uop_fields"]
    resources = chunk["per_uop_resource_keys"]
    semantic_flags = chunk["semantic_flags"]
    functional_lines = chunk["functional_lines"]
    functional_pages = chunk["functional_pages"]
    producer_logs = chunk["producer_logs"]
    macro_pcs = chunk["macro_pcs"]
    macro_end = chunk["macro_end"]
    n = max(1, sum(mask))
    valid_indices = [index for index, valid in enumerate(mask) if valid]
    mem_indices = [
        index for index in valid_indices if int(semantic_flags[index]) & 0x7
    ]
    mem_den = max(1, len(mem_indices))

    def bit_count(bit: int) -> int:
        return sum(bool(int(semantic_flags[index]) & (1 << bit)) for index in valid_indices)

    def resource_values(name: str) -> List[int]:
        key_index = RESOURCE_KEY_INDEX[name]
        return [
            int(resources[index][key_index]) for index in mem_indices
            if int(resources[index][key_index]) >= 0
        ]

    opclasses = [int(fields[index][FIELD_INDEX["op_class"]]) for index in valid_indices]
    reuse = [int(fields[index][FIELD_INDEX["reuse_distance"]]) for index in valid_indices]
    strides = [int(fields[index][FIELD_INDEX["stride"]]) for index in valid_indices]
    producer = [int(fields[index][FIELD_INDEX["producer_distance"]]) for index in valid_indices]
    branches = [index for index in valid_indices if int(semantic_flags[index]) & (1 << 3)]
    branch_taken = [
        int(fields[index][FIELD_INDEX["branch_taken"]]) == 2 for index in branches
    ]
    branch_switches = sum(left != right for left, right in zip(branch_taken, branch_taken[1:]))
    branch_kind = [int(fields[index][FIELD_INDEX["branch_kind"]]) for index in valid_indices]
    l1_sets = resource_values("l1_set")
    l2_sets = resource_values("l2_set")
    llc_sets = resource_values("llc_set")
    llc_banks = resource_values("llc_bank")
    channels = resource_values("dram_channel")
    dram_banks = [
        int(resources[index][RESOURCE_KEY_INDEX["dram_channel"]]) * 65536
        + int(resources[index][RESOURCE_KEY_INDEX["dram_rank"]]) * 4096
        + int(resources[index][RESOURCE_KEY_INDEX["dram_bank"]])
        for index in mem_indices
        if min(
            int(resources[index][RESOURCE_KEY_INDEX["dram_channel"]]),
            int(resources[index][RESOURCE_KEY_INDEX["dram_rank"]]),
            int(resources[index][RESOURCE_KEY_INDEX["dram_bank"]]),
        ) >= 0
    ]
    dram_rows = [
        tuple(int(resources[index][RESOURCE_KEY_INDEX[name]]) for name in (
            "dram_channel", "dram_rank", "dram_bank", "dram_row",
        ))
        for index in mem_indices
        if all(int(resources[index][RESOURCE_KEY_INDEX[name]]) >= 0 for name in (
            "dram_channel", "dram_rank", "dram_bank", "dram_row",
        ))
    ]
    llc_counts = Counter(llc_sets)
    row_counts = Counter(dram_rows)
    valid_pcs = [int(macro_pcs[index]) for index in valid_indices]
    pc_counts = Counter(valid_pcs)
    pc_entropy = 0.0
    if len(pc_counts) > 1:
        for count in pc_counts.values():
            probability = float(count) / n
            pc_entropy -= probability * math.log(max(probability, 1e-12))
        pc_entropy /= max(math.log(len(pc_counts)), 1e-12)
    macro_lengths: List[int] = []
    current = 0
    for index in valid_indices:
        current += 1
        if int(macro_end[index]):
            macro_lengths.append(current)
            current = 0
    if current:
        macro_lengths.append(current)
    mean_macro_log = math.log1p(sum(macro_lengths) / max(1, len(macro_lengths))) / 8.0
    lines = {int(functional_lines[index]) for index in mem_indices if int(functional_lines[index]) >= 0}
    pages = {int(functional_pages[index]) for index in mem_indices if int(functional_pages[index]) >= 0}
    int_mul = sum(value == 2 for value in opclasses)
    int_div = sum(value == 3 for value in opclasses)
    fp_alu = sum(value in {4, 5, 6, 10} for value in opclasses)
    fp_fma = sum(value in {7, 8} for value in opclasses)
    fp_divsqrt = sum(value in {9, 11, 23, 24, 29} for value in opclasses)
    result = [
        bit_count(0) / n,
        bit_count(1) / n,
        bit_count(2) / n,
        bit_count(3) / n,
        bit_count(4) / n,
        bit_count(5) / n,
        bit_count(6) / n,
        bit_count(7) / n,
        int_mul / n,
        int_div / n,
        fp_alu / n,
        fp_fma / n,
        fp_divsqrt / n,
        sum(bool(value & 0x2) for value in branch_kind) / n,
        sum(bool(value & 0x4) for value in branch_kind) / n,
        len(lines) / n,
        len(pages) / n,
        sum(float(producer_logs[index]) for index in valid_indices) / n,
        max([float(producer_logs[index]) for index in valid_indices] or [0.0]) / 16.0,
        sum(reuse[index] in (2, 3) for index in range(len(reuse)) if valid_indices[index] in mem_indices) / mem_den,
        sum(reuse[index] in (1, 8) for index in range(len(reuse)) if valid_indices[index] in mem_indices) / mem_den,
        sum(strides[index] in (3, 4, 5, 6) for index in range(len(strides)) if valid_indices[index] in mem_indices) / mem_den,
        sum(strides[index] in (7, 8, 9) for index in range(len(strides)) if valid_indices[index] in mem_indices) / mem_den,
        sum(0 < value <= 4 for value in producer) / n,
        pc_entropy,
        mean_macro_log,
        sum(mask) / max(1, int(K)),
        sum(branch_taken) / max(1, len(branch_taken)),
        branch_switches / max(1, len(branch_taken) - 1),
        len(resource_values("physical_line")) / mem_den,
        len(set(l1_sets)) / mem_den,
        len(set(l2_sets)) / mem_den,
        len(set(llc_sets)) / mem_den,
        sum(max(0, count - 1) for count in llc_counts.values()) / mem_den,
        _hhi(llc_banks),
        _hhi(channels),
        _hhi(dram_banks),
        sum(max(0, count - 1) for count in row_counts.values()) / mem_den,
    ]
    if len(result) != len(CHUNK_SUMMARY_NAMES):
        raise RuntimeError("v29 summary dimension mismatch")
    return result


def context_features(
    chunks: List[Mapping[str, Any]],
) -> Tuple[List[List[List[int]]], List[List[float]]]:
    """Build cross-core relations only from equality-preserving exact keys."""
    n_active = len(chunks)
    reads = [set(map(int, chunk.get("read_lines", []))) for chunk in chunks]
    writes = [set(map(int, chunk.get("write_lines", []))) for chunk in chunks]
    accesses = [reads[index] | writes[index] for index in range(n_active)]
    global_lines = set().union(*accesses) if accesses else set()
    total_uops = sum(sum(map(int, chunk["valid_uop_mask"])) for chunk in chunks)
    total_mem = sum(
        sum(int(kind) > 0 and bool(valid) for kind, valid in zip(
            chunk["per_uop_access"], chunk["valid_uop_mask"],
        )) for chunk in chunks
    )
    line_readers: Dict[int, set] = {}
    line_writers: Dict[int, set] = {}
    line_accessors: Dict[int, set] = {}
    for core in range(n_active):
        for line in reads[core]:
            line_readers.setdefault(line, set()).add(core)
            line_accessors.setdefault(line, set()).add(core)
        for line in writes[core]:
            line_writers.setdefault(line, set()).add(core)
            line_accessors.setdefault(line, set()).add(core)

    resource_rows = [chunk["per_uop_resource_keys"] for chunk in chunks]
    masks = [chunk["valid_uop_mask"] for chunk in chunks]
    kinds = [chunk["per_uop_access"] for chunk in chunks]
    for rows, mask, access in zip(resource_rows, masks, kinds):
        if len(rows) != len(mask) or len(rows) != len(access):
            raise ValueError("v29 context row/mask/access length mismatch")
        if any(len(row) != len(RESOURCE_KEY_NAMES) for row in rows):
            raise ValueError("v29 resource-key dimension mismatch")

    resource_sets: Dict[str, List[set]] = {}
    resource_accessors: Dict[str, Dict[int, set]] = {}
    for name in ("llc_set", "llc_bank", "dram_channel"):
        index = RESOURCE_KEY_INDEX[name]
        sets: List[set] = []
        accessors: Dict[int, set] = {}
        for core, (rows, mask, access) in enumerate(zip(resource_rows, masks, kinds)):
            own = {
                int(row[index]) for row, valid, kind in zip(rows, mask, access)
                if valid and int(kind) > 0 and int(row[index]) >= 0
            }
            sets.append(own)
            for value in own:
                accessors.setdefault(value, set()).add(core)
        resource_sets[name] = sets
        resource_accessors[name] = accessors

    bank_keys: List[set] = []
    row_keys: List[set] = []
    bank_accessors: Dict[tuple, set] = {}
    row_accessors: Dict[tuple, set] = {}
    rows_by_core_bank: List[Dict[tuple, set]] = []
    for core, (rows, mask, access) in enumerate(zip(resource_rows, masks, kinds)):
        own_banks: set = set()
        own_rows: set = set()
        by_bank: Dict[tuple, set] = {}
        for row, valid, kind in zip(rows, mask, access):
            if not valid or int(kind) <= 0:
                continue
            bank_key = tuple(int(row[RESOURCE_KEY_INDEX[name]]) for name in (
                "dram_channel", "dram_rank", "dram_bank",
            ))
            dram_row = int(row[RESOURCE_KEY_INDEX["dram_row"]])
            if min(bank_key + (dram_row,)) < 0:
                continue
            row_key = bank_key + (dram_row,)
            own_banks.add(bank_key)
            own_rows.add(row_key)
            by_bank.setdefault(bank_key, set()).add(dram_row)
            bank_accessors.setdefault(bank_key, set()).add(core)
            row_accessors.setdefault(row_key, set()).add(core)
        bank_keys.append(own_banks)
        row_keys.append(own_rows)
        rows_by_core_bank.append(by_bank)

    dynamic: List[List[List[int]]] = []
    relations: List[List[float]] = []
    for core in range(n_active):
        other_access = set().union(*(accesses[j] for j in range(n_active) if j != core))
        other_writes = set().union(*(writes[j] for j in range(n_active) if j != core))
        denom_access = max(1, len(accesses[core]))
        denom_read = max(1, len(reads[core]))
        denom_write = max(1, len(writes[core]))
        reader_fanout = [len(line_readers.get(line, set()) - {core}) for line in accesses[core]]
        writer_fanout = [len(line_writers.get(line, set()) - {core}) for line in accesses[core]]
        accessor_fanout = [len(line_accessors.get(line, set()) - {core}) for line in accesses[core]]
        fanout_den = max(1, n_active - 1)
        own_llc_sets = resource_sets["llc_set"][core]
        own_llc_banks = resource_sets["llc_bank"][core]
        own_channels = resource_sets["dram_channel"][core]
        own_banks = bank_keys[core]
        own_rows = row_keys[core]
        other_llc_sets = set().union(*(resource_sets["llc_set"][j] for j in range(n_active) if j != core))
        other_llc_banks = set().union(*(resource_sets["llc_bank"][j] for j in range(n_active) if j != core))
        other_channels = set().union(*(resource_sets["dram_channel"][j] for j in range(n_active) if j != core))
        other_banks = set().union(*(bank_keys[j] for j in range(n_active) if j != core))
        other_rows = set().union(*(row_keys[j] for j in range(n_active) if j != core))
        conflicts = {
            row for row in own_rows
            if any(
                rows_by_core_bank[j].get(row[:3], set()) - {row[3]}
                for j in range(n_active) if j != core
            )
        }
        llc_fanout = [
            len(resource_accessors["llc_set"].get(value, set()) - {core})
            for value in own_llc_sets
        ]
        bank_fanout = [
            len(bank_accessors.get(value, set()) - {core}) for value in own_banks
        ]
        relations.append([
            math.log1p(n_active) / 4.0,
            len(accesses[core] & other_access) / denom_access,
            len(reads[core] & other_writes) / denom_read,
            len(writes[core] & other_access) / denom_write,
            len(writes[core] & other_writes) / denom_write,
            sum(len(line_accessors.get(line, ())) > 1 for line in reads[core]) / denom_read,
            sum(len(line_accessors.get(line, ())) > 1 for line in writes[core]) / denom_write,
            (sum(reader_fanout) / max(1, len(reader_fanout))) / fanout_den,
            (sum(writer_fanout) / max(1, len(writer_fanout))) / fanout_den,
            max(accessor_fanout or [0]) / fanout_den,
            max([len(line_writers.get(line, set())) for line in writes[core]] or [0]) / max(1, n_active),
            math.log1p(1000.0 * len(global_lines) / max(1, total_uops)) / 8.0,
            len(accesses[core]) / max(1, len(global_lines)),
            total_mem / max(1, total_uops),
            len(own_llc_sets & other_llc_sets) / max(1, len(own_llc_sets)),
            len(own_llc_banks & other_llc_banks) / max(1, len(own_llc_banks)),
            len(own_channels & other_channels) / max(1, len(own_channels)),
            len(own_banks & other_banks) / max(1, len(own_banks)),
            len(own_rows & other_rows) / max(1, len(own_rows)),
            len(conflicts) / max(1, len(own_rows)),
            (sum(llc_fanout) / max(1, len(llc_fanout))) / fanout_den,
            (sum(bank_fanout) / max(1, len(bank_fanout))) / fanout_den,
        ])

        core_dynamic: List[List[int]] = []
        for row, line, kind, valid in zip(
            resource_rows[core], chunks[core]["per_uop_lines"], kinds[core], masks[core],
        ):
            if not valid:
                core_dynamic.append(list(DYNAMIC_PAD_IDS))
                continue
            values = [0] * len(DYNAMIC_FIELD_NAMES)
            if int(line) < 0 or int(kind) <= 0:
                core_dynamic.append(values)
                continue
            line = int(line)
            other_readers = line_readers.get(line, set()) - {core}
            other_writers = line_writers.get(line, set()) - {core}
            other_cores = line_accessors.get(line, set()) - {core}
            if int(kind) == 1:
                role = 4 if other_writers else 2 if other_readers else 1
            else:
                role = 6 if other_writers else 5 if other_readers else 3
            llc_set = int(row[RESOURCE_KEY_INDEX["llc_set"]])
            llc_bank = int(row[RESOURCE_KEY_INDEX["llc_bank"]])
            channel = int(row[RESOURCE_KEY_INDEX["dram_channel"]])
            bank_key = tuple(int(row[RESOURCE_KEY_INDEX[name]]) for name in (
                "dram_channel", "dram_rank", "dram_bank",
            ))
            dram_row = int(row[RESOURCE_KEY_INDEX["dram_row"]])
            row_key = bank_key + (dram_row,)
            conflict_cores = {
                other for other in bank_accessors.get(bank_key, set()) - {core}
                if rows_by_core_bank[other].get(bank_key, set()) - {dram_row}
            }
            values = [
                role,
                _fanout_bucket(len(other_cores)),
                _fanout_bucket(len(resource_accessors["llc_set"].get(llc_set, set()) - {core})),
                _fanout_bucket(len(resource_accessors["llc_bank"].get(llc_bank, set()) - {core})),
                _fanout_bucket(len(resource_accessors["dram_channel"].get(channel, set()) - {core})),
                _fanout_bucket(len(bank_accessors.get(bank_key, set()) - {core})),
                _fanout_bucket(len(row_accessors.get(row_key, set()) - {core})),
                _fanout_bucket(len(conflict_cores)),
            ]
            core_dynamic.append(values)
        dynamic.append(core_dynamic)
    if any(len(row) != len(RELATION_FEATURE_NAMES) for row in relations):
        raise RuntimeError("v29 relation dimension mismatch")
    return dynamic, relations


def semantic_flags(rec: Mapping[str, Any]) -> int:
    return (
        (int(rec.get("is_load", 0) or 0) & 1)
        | ((int(rec.get("is_store", 0) or 0) & 1) << 1)
        | ((int(rec.get("is_atomic", 0) or 0) & 1) << 2)
        | ((int(rec.get("is_branch", 0) or 0) & 1) << 3)
        | ((int(rec.get("is_int", 0) or 0) & 1) << 4)
        | ((int(rec.get("is_fp", 0) or 0) & 1) << 5)
        | ((int(rec.get("is_simd", 0) or 0) & 1) << 6)
        | ((int(rec.get("is_serialize", 0) or 0) & 1) << 7)
    )


def pad_window_rows(rows: Sequence[Sequence[int]], K: int, pad: Sequence[int]) -> List[List[int]]:
    out = [list(map(int, row)) for row in rows[:K]]
    while len(out) < K:
        out.append(list(map(int, pad)))
    return out
