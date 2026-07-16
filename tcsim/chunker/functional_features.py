"""Functional-only feature extraction for fixed chunks.

The model-facing fields in this module are derived from architectural trace
records and same-core program-order history only.  No tick, PMU, cache outcome,
MESI oracle, or predicted timing state is consumed.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import configparser
import copy
import re
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


FEATURE_SCHEMA_VERSION = "v28.1-base14-branch5-resource11-dynamic8-summary38-relation22"
PACKED_SCHEMA_VERSION = "functional-v28.1-packed-3-resource-context"
MODEL_INPUT_CONTRACT = "functional_only_v28_1_four_branch"
BRANCH_CONTRACT_VERSION = "all_retired_branches_v28.1"
RAW_TRACE_SCHEMA_VERSION = "v28.1-branch-roi-percore"
PREDICTOR_HASH_SCHEMA_VERSION = "tcsim-branch-predictor-semantic-v2"

# Static fields are grouped explicitly.  They are encoded by three independent
# branches before being combined, so adding resource fields cannot silently
# change the meaning of the existing base/branch embedding weights.
BASE_FIELD_NAMES = (
    "op_class",
    "reg_dependency",
    "mem_kind",
    "producer_distance",
    "reuse_distance",
    "stride",
    "local_pc_id",
    "macro_position",
    "local_line_id",
    "same_core_history",
    "mem_size",
    "line_offset",
    "recent_ws_short",
    "recent_ws_long",
)

BASE_FIELD_SIZES = (
    90, 64, 5, 17, 9, 10, 16384, 5, 8192, 12, 10, 10, 14, 18,
)

BRANCH_FIELD_NAMES = (
    "branch_kind",
    "branch_taken",
    "branch_successor_delta",
    "branch_history_low8",
    "branch_history_high8",
)
BRANCH_FIELD_SIZES = (32, 3, 34, 257, 257)

RESOURCE_FIELD_NAMES = (
    "paddr_valid",
    "l1_set",
    "l2_set",
    "llc_set",
    "llc_bank",
    "dram_channel",
    "dram_bank",
    "dram_row_reuse",
    "l1_set_pressure",
    "l2_set_pressure",
    "llc_set_pressure",
)
# Set identifiers are trace-permuted and bounded.  Exact equality/competition
# is computed from the separate int64 resource-key tensor, never from a
# collision-prone embedding bucket.
RESOURCE_FIELD_SIZES = (3, 2049, 8193, 32769, 257, 65, 257, 10, 10, 10, 10)

FIELD_NAMES = BASE_FIELD_NAMES + BRANCH_FIELD_NAMES + RESOURCE_FIELD_NAMES
FIELD_SIZES = BASE_FIELD_SIZES + BRANCH_FIELD_SIZES + RESOURCE_FIELD_SIZES

# Valid ids are [0, size).  The padding id for each field is exactly ``size``.
FIELD_PAD_IDS = tuple(FIELD_SIZES)
FIELD_INDEX = {name: idx for idx, name in enumerate(FIELD_NAMES)}
FIELD_GROUP_INDICES = {
    "base": tuple(FIELD_INDEX[name] for name in BASE_FIELD_NAMES),
    "branch": tuple(FIELD_INDEX[name] for name in BRANCH_FIELD_NAMES),
    "resource": tuple(FIELD_INDEX[name] for name in RESOURCE_FIELD_NAMES),
}

DYNAMIC_FIELD_NAMES = (
    "xcore_line_role",
    "xcore_line_fanout",
    "llc_set_fanout",
    "llc_bank_fanout",
    "dram_channel_fanout",
    "dram_bank_fanout",
    "same_row_support",
    "different_row_conflict",
)
DYNAMIC_FIELD_SIZES = (8, 8, 8, 8, 8, 8, 8, 8)
DYNAMIC_PAD_IDS = tuple(DYNAMIC_FIELD_SIZES)
DYNAMIC_FIELD_INDEX = {name: idx for idx, name in enumerate(DYNAMIC_FIELD_NAMES)}

RESOURCE_KEY_NAMES = (
    "physical_line",
    "l1_set",
    "l2_set",
    "llc_set",
    "llc_bank",
    "dram_channel",
    "dram_bank",
    "dram_row",
)
RESOURCE_KEY_INDEX = {name: idx for idx, name in enumerate(RESOURCE_KEY_NAMES)}
RESOURCE_KEY_INVALID = -1


def feature_contract_metadata(predictor_hash_value: str = "") -> Dict[str, Any]:
    return {
        "raw_trace_schema": RAW_TRACE_SCHEMA_VERSION,
        "packed_schema": PACKED_SCHEMA_VERSION,
        "model_input_contract": MODEL_INPUT_CONTRACT,
        "feature_schema": FEATURE_SCHEMA_VERSION,
        "branch_contract": BRANCH_CONTRACT_VERSION,
        "predictor_hash": str(predictor_hash_value),
        "dimensions": {
            "static_fields": len(FIELD_NAMES),
            "dynamic_fields": len(DYNAMIC_FIELD_NAMES),
            "resource_keys": len(RESOURCE_KEY_NAMES),
            "chunk_summary": len(CHUNK_SUMMARY_NAMES),
            "relation": len(RELATION_FEATURE_NAMES),
            "uarch": len(UARCH_FEATURE_NAMES),
        },
    }

CHUNK_SUMMARY_NAMES = (
    "load_frac",
    "store_frac",
    "atomic_frac",
    "branch_frac",
    "int_frac",
    "fp_frac",
    "simd_frac",
    "serialize_frac",
    "int_mul_frac",
    "int_div_frac",
    "fp_alu_frac",
    "fp_fma_frac",
    "fp_divsqrt_frac",
    "conditional_branch_frac",
    "indirect_branch_frac",
    "distinct_lines_per_uop",
    "distinct_pages_per_uop",
    "mean_log1p_producer_distance",
    "max_log1p_producer_distance",
    "mem_hot_frac",
    "mem_cold_frac",
    "stream_stride_frac",
    "large_stride_frac",
    "short_dependency_frac",
    "pc_entropy",
    "mean_basic_block_len_log",
    "tail_fraction",
    "taken_branch_frac",
    "branch_direction_switch_rate",
    "paddr_valid_mem_frac",
    "distinct_l1_sets_per_mem",
    "distinct_l2_sets_per_mem",
    "distinct_llc_sets_per_mem",
    "llc_set_conflict_frac",
    "llc_bank_hhi",
    "dram_channel_hhi",
    "dram_bank_hhi",
    "dram_row_reuse_frac",
)

RELATION_FEATURE_NAMES = (
    "log1p_active_cores",
    "shared_line_frac",
    "read_after_other_write_frac",
    "write_to_other_access_frac",
    "multiwriter_frac",
    "shared_read_line_frac",
    "shared_write_line_frac",
    "mean_other_reader_fanout",
    "mean_other_writer_fanout",
    "max_other_accessor_fanout",
    "writer_core_coverage",
    "global_lines_per_kuop_log",
    "core_global_line_coverage",
    "aggregate_mem_density",
    "same_llc_set_frac",
    "same_llc_bank_frac",
    "same_dram_channel_frac",
    "same_dram_bank_frac",
    "same_dram_row_frac",
    "different_row_same_bank_frac",
    "mean_llc_set_other_fanout",
    "mean_dram_bank_other_fanout",
)

UARCH_FEATURE_NAMES = (
    "freq_ghz",
    "log2_num_cores",
    "log2_fetch_width",
    "log2_decode_width",
    "log2_issue_width",
    "log2_commit_width",
    "log2_rob_entries",
    "log2_iq_entries",
    "log2_lq_entries",
    "log2_sq_entries",
    "log2_l1d_size",
    "log2_l1d_assoc",
    "log2_l2_size",
    "log2_l2_assoc",
    "log2_l3_size",
    "log2_l3_assoc",
    "log2_l3_banks",
    "log2_dtlb_entries",
    "log2_dtlb_assoc",
    "log2_itlb_entries",
    "log2_itlb_assoc",
    "log2_l1d_mshr",
    "log2_l2_mshr",
    "log2_l3_mshr",
    "log2_dram_channels",
    "log2_dram_banks_per_channel",
    "log2_dram_row_size",
    "log2_dram_burst",
)


def _hash_bucket(x: int, n: int) -> int:
    x = int(x) & 0xFFFFFFFFFFFFFFFF
    x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9 & 0xFFFFFFFFFFFFFFFF
    x = (x ^ (x >> 27)) * 0x94D049BB133111EB & 0xFFFFFFFFFFFFFFFF
    x ^= x >> 31
    return int(x % max(1, int(n)))


def functional_line(rec: Mapping[str, Any]) -> Optional[int]:
    vaddr = int(rec.get("vaddr", 0) or 0)
    if vaddr:
        return vaddr >> 6
    line = int(rec.get("cacheline_addr", 0) or 0)
    return line if line else None


def physical_line(rec: Mapping[str, Any]) -> Optional[int]:
    """Return the physical cache-line key used only for resource equality.

    The key is serialized in the non-model-facing resource tensor.  Raw
    addresses are never embedded or returned as model fields.
    """
    paddr = int(rec.get("paddr", 0) or 0)
    if paddr:
        return paddr >> 6
    line = int(rec.get("cacheline_paddr", 0) or 0)
    return line if line else None


def functional_page(rec: Mapping[str, Any]) -> Optional[int]:
    vaddr = int(rec.get("vaddr", 0) or 0)
    if vaddr:
        return vaddr >> 12
    return None


def is_mem(rec: Mapping[str, Any]) -> bool:
    return bool(
        int(rec.get("is_load", 0) or 0)
        or int(rec.get("is_store", 0) or 0)
        or int(rec.get("is_atomic", 0) or 0)
    )


def is_write(rec: Mapping[str, Any]) -> bool:
    return bool(
        int(rec.get("is_store", 0) or 0)
        or int(rec.get("is_atomic", 0) or 0)
    )


def _log_bucket(value: int, max_bucket: int) -> int:
    value = max(0, int(value))
    if value <= 0:
        return 0
    return min(int(max_bucket), 1 + int(math.log2(value)))


def _reuse_bucket(distance: Optional[int], seen_before: bool) -> int:
    if distance is None:
        return 8 if seen_before else 1  # far / cold
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


def _stride_bucket(delta: Optional[int]) -> int:
    if delta is None:
        return 1
    if delta == 0:
        return 2
    if delta == 1:
        return 3
    if delta == -1:
        return 4
    if 2 <= delta <= 8:
        return 5
    if -8 <= delta <= -2:
        return 6
    if 9 <= delta <= 64:
        return 7
    if -64 <= delta <= -9:
        return 8
    return 9


def _branch_bucket(rec: Mapping[str, Any]) -> int:
    return (
        (int(rec.get("is_branch", 0) or 0) & 1)
        | ((int(rec.get("is_branch_cond", 0) or 0) & 1) << 1)
        | ((int(rec.get("is_branch_indirect", 0) or 0) & 1) << 2)
        | ((int(rec.get("is_call", 0) or 0) & 1) << 3)
        | ((int(rec.get("is_return", 0) or 0) & 1) << 4)
    )


def _signed_log_bucket(value: int) -> int:
    """34-way signed log bucket: non-branch=0 is assigned by the caller."""
    value = int(value)
    if value == 0:
        return 1
    magnitude = min(15, int(math.log2(abs(value))))
    return 2 + magnitude if value > 0 else 18 + magnitude


def _fanout_bucket(other_cores: int) -> int:
    if int(other_cores) <= 0:
        return 1
    return 1 + min(6, int(math.ceil(math.log2(int(other_cores) + 1))))


def _hhi(values: Sequence[int]) -> float:
    if not values:
        return 0.0
    counts = Counter(int(x) for x in values)
    total = float(len(values))
    return sum((count / total) ** 2 for count in counts.values())


def _positive_int(value: float, default: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(1, parsed)


@dataclass
class PhysicalResourceMapper:
    """Map paddr to target-uarch resources without exposing raw addresses."""

    profile: Mapping[str, Any] = field(default_factory=dict)
    permutation_seed: str = ""

    def __post_init__(self) -> None:
        p = self.profile or {}
        line_b = _positive_int(_nested(p, ("cache", "l1d", "line_b"), 64), 64)
        # The trace contract is cache-line based.  A non-64B target would need
        # a new raw/packed schema because physical_line is currently paddr>>6.
        if line_b != 64:
            raise ValueError(f"v28.1 resource mapping requires 64B lines, got {line_b}")
        self.l1_sets = self._cache_sets("l1d")
        self.l2_sets = self._cache_sets("l2")
        self.llc_banks = _positive_int(_nested(p, ("cache", "l3", "num_banks"), 1))
        self.llc_sets = max(1, self._cache_sets("l3") // self.llc_banks)
        self.dram_channels = _positive_int(_nested(p, ("dram", "num_channels"), 1))
        self.dram_banks = _positive_int(_nested(p, ("dram", "banks_per_channel"), 1))
        row_b = _positive_int(_nested(p, ("dram", "row_size_b"), 8192), 8192)
        self.lines_per_row = max(1, row_b // 64)
        self._row_position: Dict[Tuple[int, int, int], int] = {}
        self._mem_position = 0

    def _cache_sets(self, level: str) -> int:
        size = _positive_int(_nested(self.profile, ("cache", level, "size_b"), 64), 64)
        assoc = _positive_int(_nested(self.profile, ("cache", level, "assoc"), 1))
        return max(1, size // (assoc * 64))

    def _permuted(self, value: int, count: int, field_name: str) -> int:
        """Return a stable per-trace category while preserving equality."""
        capacity = int(RESOURCE_FIELD_SIZES[RESOURCE_FIELD_NAMES.index(field_name)]) - 1
        count = max(1, int(count))
        if count > capacity:
            salt = int.from_bytes(hashlib.sha256(
                f"{self.permutation_seed}:{field_name}:overflow".encode("utf-8")
            ).digest()[:8], "little")
            return 1 + _hash_bucket(
                int(value) ^ salt,
                capacity,
            )
        if count == 1:
            return 1
        digest = hashlib.sha256(
            f"{self.permutation_seed}:{field_name}".encode("utf-8")
        ).digest()
        a = 1 + int.from_bytes(digest[:8], "little") % (count - 1)
        while math.gcd(a, count) != 1:
            a = 1 + (a % (count - 1))
        b = int.from_bytes(digest[8:16], "little") % count
        return 1 + ((a * int(value) + b) % count)

    def encode(self, rec: Mapping[str, Any]) -> Tuple[List[int], List[int]]:
        if not is_mem(rec):
            return [0] * len(RESOURCE_FIELD_NAMES), [RESOURCE_KEY_INVALID] * len(RESOURCE_KEY_NAMES)
        line = physical_line(rec)
        if line is None:
            # 1 means a memory UOP whose physical mapping was unavailable.
            return [1] + [0] * (len(RESOURCE_FIELD_NAMES) - 1), [RESOURCE_KEY_INVALID] * len(RESOURCE_KEY_NAMES)

        l1_set = line % self.l1_sets
        l2_set = line % self.l2_sets
        llc_bank = line % self.llc_banks
        llc_set = (line // self.llc_banks) % self.llc_sets
        dram_channel = line % self.dram_channels
        channel_line = line // self.dram_channels
        dram_bank = channel_line % self.dram_banks
        dram_row = channel_line // (self.dram_banks * self.lines_per_row)

        self._mem_position += 1
        row_key = (dram_channel, dram_bank, dram_row)
        previous = self._row_position.get(row_key)
        row_distance = self._mem_position - previous if previous is not None else None
        row_reuse = _reuse_bucket(row_distance, previous is not None)
        self._row_position[row_key] = self._mem_position

        keys = [
            line, l1_set, l2_set, llc_set, llc_bank,
            dram_channel, dram_bank, dram_row,
        ]
        fields = [
            2,
            self._permuted(l1_set, self.l1_sets, "l1_set"),
            self._permuted(l2_set, self.l2_sets, "l2_set"),
            self._permuted(llc_set, self.llc_sets, "llc_set"),
            self._permuted(llc_bank, self.llc_banks, "llc_bank"),
            self._permuted(dram_channel, self.dram_channels, "dram_channel"),
            self._permuted(dram_bank, self.dram_banks, "dram_bank"),
            row_reuse,
            0, 0, 0,  # filled from complete chunk occupancy in _pack_chunk
        ]
        return fields, keys


def _reg_bucket(rec: Mapping[str, Any]) -> int:
    n_src = int(rec.get("n_src", 0) or 0) & 0x7
    n_dst = int(rec.get("n_dst", 0) or 0) & 0x7
    h = (n_src << 3) | n_dst
    for value in list(rec.get("producer_classes", []) or [])[:4]:
        h = (h << 8) | (int(value) & 0xFF)
    return _hash_bucket(h, FIELD_SIZES[FIELD_INDEX["reg_dependency"]])


def _producer_distance(rec: Mapping[str, Any]) -> Tuple[int, float]:
    vals = [int(x) for x in list(rec.get("producer_dists", []) or []) if int(x) > 0]
    if not vals:
        return 0, 0.0
    nearest = min(vals)
    return (
        _log_bucket(nearest, FIELD_SIZES[FIELD_INDEX["producer_distance"]] - 1),
        math.log1p(float(nearest)),
    )


@dataclass
class FunctionalFeatureEncoder:
    """Stateful same-core program-order feature encoder."""

    mem_index: int = 0
    last_line: Optional[int] = None
    previous_macro_pc: Optional[int] = None
    previous_ended_macro: bool = True
    uarch_profile: Mapping[str, Any] = field(default_factory=dict)
    resource_seed: str = ""

    def __post_init__(self) -> None:
        self.last_line_position: Dict[int, int] = {}
        self.local_line_ids: Dict[int, int] = {}
        self.local_pc_ids: Dict[int, int] = {}
        self._recent_short = deque()
        self._recent_long = deque()
        self._recent_short_count: Counter = Counter()
        self._recent_long_count: Counter = Counter()
        self.resource_mapper = PhysicalResourceMapper(
            self.uarch_profile, permutation_seed=self.resource_seed,
        )

    @staticmethod
    def _push_recent(window: deque, counts: Counter, line: int, limit: int) -> None:
        window.append(line)
        counts[line] += 1
        if len(window) > limit:
            old = window.popleft()
            counts[old] -= 1
            if counts[old] <= 0:
                del counts[old]

    def encode(self, rec: Mapping[str, Any]) -> Tuple[List[int], float, List[int]]:
        missing_branch = {
            "branch_taken", "branch_target", "branch_next_pc", "branch_history",
        } - set(rec)
        if missing_branch:
            raise RuntimeError(
                "raw trace predates v28.1 branch schema; missing "
                f"{sorted(missing_branch)}. Recollect raw data."
            )
        branch_flag = int(rec.get("is_branch", 0) or 0)
        subtype_flag = any(int(rec.get(name, 0) or 0) for name in (
            "is_branch_cond", "is_branch_indirect", "is_call", "is_return",
        ))
        taken_value = int(rec.get("branch_taken", 0) or 0)
        target_value = int(rec.get("branch_target", 0) or 0)
        next_pc_value = int(rec.get("branch_next_pc", 0) or 0)
        history_value = int(rec.get("branch_history", 0) or 0)
        if subtype_flag and not branch_flag:
            raise RuntimeError("branch subtype without is_branch in v28.1 raw trace")
        if taken_value not in (0, 1) or not 0 <= history_value <= 0xFFFF:
            raise RuntimeError("invalid branch_taken/history in v28.1 raw trace")
        if branch_flag and (
            next_pc_value <= 0
            or (taken_value and target_value != next_pc_value)
            or (not taken_value and target_value != 0)
        ):
            raise RuntimeError("inconsistent branch target/successor in v28.1 raw trace")
        mem = is_mem(rec)
        line = functional_line(rec) if mem else None
        producer_bucket, producer_log = _producer_distance(rec)

        if int(rec.get("is_atomic", 0) or 0):
            mem_kind = 3
        elif int(rec.get("is_load", 0) or 0):
            mem_kind = 1
        elif int(rec.get("is_store", 0) or 0):
            mem_kind = 2
        elif int(rec.get("is_serialize", 0) or 0):
            mem_kind = 4
        else:
            mem_kind = 0

        if mem:
            self.mem_index += 1
            prev_pos = self.last_line_position.get(line) if line is not None else None
            distance = self.mem_index - prev_pos if prev_pos is not None else None
            reuse = _reuse_bucket(distance, prev_pos is not None)
            stride = _stride_bucket(
                None if line is None or self.last_line is None else line - self.last_line
            )
            if line is None:
                hist = 1
            elif prev_pos is None:
                hist = 1
            else:
                hist = min(
                    FIELD_SIZES[FIELD_INDEX["same_core_history"]] - 1,
                    1 + reuse,
                )
            if line is not None:
                if line not in self.local_line_ids:
                    # First-touch order is stable under address relocation and
                    # still preserves equality/reuse within a core.  Exact
                    # cross-core equality is handled separately from raw line
                    # keys and is not exposed as an address-identity shortcut.
                    self.local_line_ids[line] = 1 + (
                        len(self.local_line_ids)
                        % (FIELD_SIZES[FIELD_INDEX["local_line_id"]] - 1)
                    )
                self.last_line_position[line] = self.mem_index
                self.last_line = line
                self._push_recent(
                    self._recent_short, self._recent_short_count, line, 4096,
                )
                self._push_recent(
                    self._recent_long, self._recent_long_count, line, 65536,
                )
        else:
            reuse = 0
            stride = 0
            hist = 0

        macro_pc = int(rec.get("macro_pc", rec.get("micro_pc", 0)) or 0)
        is_micro = bool(int(rec.get("is_microop", 0) or 0))
        is_last = bool(int(rec.get("is_last_microop", 0) or 0))
        is_head = self.previous_ended_macro or self.previous_macro_pc != macro_pc
        if not is_micro or (is_head and is_last):
            macro_pos = 1  # single
        elif is_head:
            macro_pos = 2  # first
        elif is_last:
            macro_pos = 4  # last
        else:
            macro_pos = 3  # middle
        self.previous_macro_pc = macro_pc
        self.previous_ended_macro = (not is_micro) or is_last

        op_class = int(rec.get("op_class", 0) or 0)
        if not 0 <= op_class < FIELD_SIZES[FIELD_INDEX["op_class"]]:
            op_class = 0
        if macro_pc and macro_pc not in self.local_pc_ids:
            self.local_pc_ids[macro_pc] = 1 + (
                len(self.local_pc_ids)
                % (FIELD_SIZES[FIELD_INDEX["local_pc_id"]] - 1)
            )
        local_pc_id = self.local_pc_ids.get(macro_pc, 0)
        local_line_id = self.local_line_ids.get(line, 0) if line is not None else 0

        size = int(rec.get("size", 0) or 0)
        if not mem:
            mem_size = 0
            line_offset = 0
        else:
            if size <= 0:
                mem_size = 1
            elif size <= 1:
                mem_size = 2
            elif size <= 2:
                mem_size = 3
            elif size <= 4:
                mem_size = 4
            elif size <= 8:
                mem_size = 5
            elif size <= 16:
                mem_size = 6
            elif size <= 32:
                mem_size = 7
            elif size <= 64:
                mem_size = 8
            else:
                mem_size = 9
            vaddr = int(rec.get("vaddr", 0) or 0)
            line_offset = 1 if vaddr == 0 else 2 + ((vaddr & 63) // 8)

        base_fields = [
            op_class,
            _reg_bucket(rec),
            mem_kind,
            producer_bucket,
            reuse,
            stride,
            local_pc_id,
            macro_pos,
            local_line_id,
            hist,
            mem_size,
            line_offset,
            _log_bucket(
                len(self._recent_short_count),
                FIELD_SIZES[FIELD_INDEX["recent_ws_short"]] - 1,
            ) if mem else 0,
            _log_bucket(
                len(self._recent_long_count),
                FIELD_SIZES[FIELD_INDEX["recent_ws_long"]] - 1,
            ) if mem else 0,
        ]

        branch = bool(int(rec.get("is_branch", 0) or 0))
        if branch:
            history = int(rec.get("branch_history", 0) or 0) & 0xFFFF
            next_pc = int(rec.get("branch_next_pc", 0) or 0)
            successor_delta = _signed_log_bucket(next_pc - macro_pc)
            branch_fields = [
                _branch_bucket(rec),
                2 if int(rec.get("branch_taken", 0) or 0) else 1,
                successor_delta,
                1 + (history & 0xFF),
                1 + ((history >> 8) & 0xFF),
            ]
        else:
            branch_fields = [0] * len(BRANCH_FIELD_NAMES)
        resource_fields, resource_keys = self.resource_mapper.encode(rec)
        return base_fields + branch_fields + resource_fields, producer_log, resource_keys


def chunk_summary(
    records: Sequence[Mapping[str, Any]],
    producer_logs: Sequence[float],
    feature_rows: Sequence[Sequence[int]],
    resource_rows: Sequence[Sequence[int]],
    K: int,
) -> List[float]:
    n = max(1, len(records))
    lines = {functional_line(r) for r in records if is_mem(r)} - {None}
    pages = {functional_page(r) for r in records if is_mem(r)} - {None}
    count = lambda key: sum(int(r.get(key, 0) or 0) for r in records)
    opclasses = [int(r.get("op_class", 0) or 0) for r in records]
    reuse = [int(row[FIELD_INDEX["reuse_distance"]]) for row in feature_rows]
    strides = [int(row[FIELD_INDEX["stride"]]) for row in feature_rows]
    producer = [int(row[FIELD_INDEX["producer_distance"]]) for row in feature_rows]
    mem_rows = [i for i, r in enumerate(records) if is_mem(r)]
    mem_den = max(1, len(mem_rows))
    valid_resource_rows = [
        resource_rows[i] for i in mem_rows
        if int(resource_rows[i][RESOURCE_KEY_INDEX["physical_line"]]) >= 0
    ]

    def resource_values(name: str) -> List[int]:
        idx = RESOURCE_KEY_INDEX[name]
        return [int(row[idx]) for row in valid_resource_rows if int(row[idx]) >= 0]

    l1_sets = resource_values("l1_set")
    l2_sets = resource_values("l2_set")
    llc_sets = resource_values("llc_set")
    llc_banks = resource_values("llc_bank")
    dram_channels = resource_values("dram_channel")
    dram_banks = [
        int(row[RESOURCE_KEY_INDEX["dram_channel"]]) * 4096
        + int(row[RESOURCE_KEY_INDEX["dram_bank"]])
        for row in valid_resource_rows
    ]
    dram_rows = [
        (
            int(row[RESOURCE_KEY_INDEX["dram_channel"]]),
            int(row[RESOURCE_KEY_INDEX["dram_bank"]]),
            int(row[RESOURCE_KEY_INDEX["dram_row"]]),
        )
        for row in valid_resource_rows
    ]
    branch_directions = [
        int(r.get("branch_taken", 0) or 0)
        for r in records if int(r.get("is_branch", 0) or 0)
    ]
    direction_switches = sum(
        int(left != right)
        for left, right in zip(branch_directions, branch_directions[1:])
    )
    llc_counts = Counter(llc_sets)
    row_counts = Counter(dram_rows)
    pcs = [int(r.get("macro_pc", r.get("micro_pc", 0)) or 0) for r in records]
    pc_counts: Dict[int, int] = {}
    for pc in pcs:
        pc_counts[pc] = pc_counts.get(pc, 0) + 1
    pc_entropy = 0.0
    if len(pc_counts) > 1:
        for value in pc_counts.values():
            p = float(value) / n
            pc_entropy -= p * math.log(max(p, 1e-12))
        pc_entropy /= max(math.log(len(pc_counts)), 1e-12)
    bb_lengths: List[int] = []
    current_bb = 0
    for rec in records:
        current_bb += 1
        if int(rec.get("is_branch", 0) or 0):
            bb_lengths.append(current_bb)
            current_bb = 0
    if current_bb:
        bb_lengths.append(current_bb)
    mean_bb_log = math.log1p(sum(bb_lengths) / max(1, len(bb_lengths))) / 8.0

    int_mul = sum(1 for x in opclasses if x == 2)
    int_div = sum(1 for x in opclasses if x == 3)
    fp_alu = sum(1 for x in opclasses if x in {4, 5, 6, 10})
    fp_fma = sum(1 for x in opclasses if x in {7, 8})
    fp_divsqrt = sum(1 for x in opclasses if x in {9, 11, 23, 24, 29})
    return [
        count("is_load") / n,
        count("is_store") / n,
        count("is_atomic") / n,
        count("is_branch") / n,
        count("is_int") / n,
        count("is_fp") / n,
        count("is_simd") / n,
        count("is_serialize") / n,
        int_mul / n,
        int_div / n,
        fp_alu / n,
        fp_fma / n,
        fp_divsqrt / n,
        count("is_branch_cond") / n,
        count("is_branch_indirect") / n,
        len(lines) / n,
        len(pages) / n,
        sum(float(x) for x in producer_logs) / n,
        max([float(x) for x in producer_logs] or [0.0]) / 16.0,
        sum(1 for i in mem_rows if reuse[i] in (2, 3)) / mem_den,
        sum(1 for i in mem_rows if reuse[i] in (1, 8)) / mem_den,
        sum(1 for i in mem_rows if strides[i] in (3, 4, 5, 6)) / mem_den,
        sum(1 for i in mem_rows if strides[i] in (7, 8, 9)) / mem_den,
        sum(1 for value in producer if 0 < value <= 4) / n,
        pc_entropy,
        mean_bb_log,
        len(records) / max(1, int(K)),
        sum(branch_directions) / max(1, len(branch_directions)),
        direction_switches / max(1, len(branch_directions) - 1),
        len(valid_resource_rows) / mem_den,
        len(set(l1_sets)) / mem_den,
        len(set(l2_sets)) / mem_den,
        len(set(llc_sets)) / mem_den,
        sum(max(0, value - 1) for value in llc_counts.values()) / mem_den,
        _hhi(llc_banks),
        _hhi(dram_channels),
        _hhi(dram_banks),
        sum(max(0, value - 1) for value in row_counts.values()) / mem_den,
    ]


def load_uarch_profile(trace_dir: str) -> Dict[str, Any]:
    path = os.path.join(os.path.dirname(trace_dir.rstrip("/")), "uarch_profile.json")
    if not os.path.exists(path):
        profile: Dict[str, Any] = {}
    else:
        with open(path, "r", encoding="utf-8") as fh:
            profile = json.load(fh)

    # The collected v27 profile omits O3 widths/queue capacities even though
    # they are required to distinguish future ROB/backend pivots.  Recover
    # those architectural knobs from gem5's config.ini when available.
    config_path = os.path.join(os.path.dirname(trace_dir.rstrip("/")), "config.ini")
    if os.path.exists(config_path):
        parser = configparser.RawConfigParser(interpolation=None, strict=False)
        try:
            parser.read(config_path)
            core_sections = [
                s for s in parser.sections()
                if (".switch" in s or ".cores" in s) and s.endswith(".core")
            ]
            if core_sections:
                section = sorted(core_sections)[0]
                core = profile.setdefault("core", {})
                mapping = {
                    "fetch_width": "fetchWidth",
                    "decode_width": "decodeWidth",
                    "issue_width": "issueWidth",
                    "commit_width": "commitWidth",
                    "rob_entries": "numROBEntries",
                    "lq_entries": "LQEntries",
                    "sq_entries": "SQEntries",
                }
                for dst, src in mapping.items():
                    if parser.has_option(section, src):
                        core[dst] = parser.getint(section, src)
                iq_section = section + ".instQueues"
                if parser.has_option(iq_section, "numEntries"):
                    core["iq_entries"] = parser.getint(iq_section, "numEntries")
                bp_prefix = section + ".branchPred"
                predictor: Dict[str, Dict[str, str]] = {}
                for bp_section in parser.sections():
                    if bp_section == bp_prefix or bp_section.startswith(bp_prefix + "."):
                        relative = bp_section[len(bp_prefix):].lstrip(".") or "root"
                        predictor[relative] = {
                            key: value for key, value in parser.items(bp_section)
                            if key not in {"eventq_index", "power_model", "power_state"}
                        }
                if predictor:
                    profile["branch_predictor"] = predictor
        except (configparser.Error, OSError, ValueError):
            # Audit code reports missing dimensions; feature extraction itself
            # remains usable for synthetic/minimal traces.
            pass
    return profile


def uarch_hash(profile: Mapping[str, Any], include_topology: bool = False) -> str:
    normalized = copy.deepcopy(dict(profile or {}))
    if not include_topology and isinstance(normalized.get("core"), dict):
        normalized["core"].pop("num_cores", None)
    blob = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


_PREDICTOR_NON_SEMANTIC_KEYS = frozenset({
    "children",
    "clk_domain",
    "eventq_index",
    "power_model",
    "power_state",
})
_PREDICTOR_OBJECT_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:[A-Za-z0-9_\[\]-]+\.)+branchPred(?=\.|$)",
    flags=re.IGNORECASE,
)


def _canonical_predictor_value(value: Any) -> Any:
    """Remove gem5 instance naming from a predictor configuration value."""
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_predictor_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_predictor_value(item) for item in value]
    if isinstance(value, str):
        return _PREDICTOR_OBJECT_PATH_RE.sub("$BRANCH_PREDICTOR", value)
    return value


def canonical_predictor_config(profile: Mapping[str, Any]) -> Dict[str, Any]:
    """Return only semantic branch-predictor configuration.

    gem5 assigns topology-dependent SimObject names such as ``switch``,
    ``switch0`` and ``switch00``.  Those names can change with core count even
    when every predictor parameter is identical, so they must not affect the
    provenance hash.
    """
    predictor = dict((profile or {}).get("branch_predictor", {}) or {})
    if not predictor:
        raise RuntimeError(
            "v28.1 requires branch predictor provenance in uarch_profile/config.ini"
        )
    normalized: Dict[str, Any] = {}
    for section, raw_params in sorted(predictor.items(), key=lambda pair: str(pair[0])):
        section_name = str(section)
        if section_name.lower() == "power_state" or section_name.lower().endswith(
            ".power_state"
        ):
            continue
        if not isinstance(raw_params, Mapping):
            normalized[section_name] = _canonical_predictor_value(raw_params)
            continue
        params = {
            str(key): _canonical_predictor_value(value)
            for key, value in sorted(raw_params.items(), key=lambda pair: str(pair[0]))
            if str(key).lower() not in _PREDICTOR_NON_SEMANTIC_KEYS
        }
        normalized[section_name] = params
    return normalized


def predictor_hash(profile: Mapping[str, Any]) -> str:
    payload = {
        "schema": PREDICTOR_HASH_SCHEMA_VERSION,
        "config": canonical_predictor_config(profile),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _nested(profile: Mapping[str, Any], path: Sequence[str], default: float = 0.0) -> float:
    cur: Any = profile
    for key in path:
        if not isinstance(cur, Mapping):
            return float(default)
        cur = cur.get(key)
    try:
        return float(cur)
    except (TypeError, ValueError):
        return float(default)


def _log2p(value: float) -> float:
    return math.log2(max(1.0, float(value)))


def uarch_vector(profile: Mapping[str, Any]) -> List[float]:
    p = profile or {}
    return [
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
        _log2p(_nested(p, ("dram", "banks_per_channel"), 1)),
        _log2p(_nested(p, ("dram", "row_size_b"), 1)),
        _log2p(_nested(p, ("dram", "burst_b"), 1)),
    ]


def tick_per_cycle_from_profile(profile: Mapping[str, Any], fallback: float = 500.0) -> float:
    freq = _nested(profile or {}, ("core", "freq_ghz"), 0.0)
    # gem5 tick clocks in this corpus are integral (3 GHz -> 333 ticks), while
    # 1000/3 is repeating.  Use the configured integral clock rather than
    # silently introducing a 0.1% label-scale error.
    return float(round(1000.0 / freq)) if freq > 0 else float(fallback)
