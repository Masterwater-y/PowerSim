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
from collections import Counter, deque
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


FIELD_NAMES = (
    "op_class",
    "reg_dependency",
    "mem_kind",
    "producer_distance",
    "reuse_distance",
    "stride",
    "branch_kind",
    "local_pc_id",
    "macro_position",
    "local_line_id",
    "same_core_history",
    "mem_size",
    "line_offset",
    "recent_ws_short",
    "recent_ws_long",
    # The final two fields are filled from the other functional chunks in the
    # current context by ``TCSimSampleDataset``.  They never use timing state.
    "xcore_role",
    "xcore_fanout",
)

# Valid ids are [0, size).  The padding id for each field is exactly ``size``.
FIELD_SIZES = (
    90, 64, 5, 17, 9, 10, 32, 16384, 5, 8192, 12, 10, 10,
    14, 18, 8, 8,
)
FIELD_PAD_IDS = tuple(FIELD_SIZES)
FIELD_INDEX = {name: idx for idx, name in enumerate(FIELD_NAMES)}

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


def _reg_bucket(rec: Mapping[str, Any]) -> int:
    n_src = int(rec.get("n_src", 0) or 0) & 0x7
    n_dst = int(rec.get("n_dst", 0) or 0) & 0x7
    h = (n_src << 3) | n_dst
    for value in list(rec.get("producer_classes", []) or [])[:4]:
        h = (h << 8) | (int(value) & 0xFF)
    return _hash_bucket(h, FIELD_SIZES[1])


def _producer_distance(rec: Mapping[str, Any]) -> Tuple[int, float]:
    vals = [int(x) for x in list(rec.get("producer_dists", []) or []) if int(x) > 0]
    if not vals:
        return 0, 0.0
    nearest = min(vals)
    return _log_bucket(nearest, FIELD_SIZES[3] - 1), math.log1p(float(nearest))


@dataclass
class FunctionalFeatureEncoder:
    """Stateful same-core program-order feature encoder."""

    mem_index: int = 0
    last_line: Optional[int] = None
    previous_macro_pc: Optional[int] = None
    previous_ended_macro: bool = True

    def __post_init__(self) -> None:
        self.last_line_position: Dict[int, int] = {}
        self.local_line_ids: Dict[int, int] = {}
        self.local_pc_ids: Dict[int, int] = {}
        self._recent_short = deque()
        self._recent_long = deque()
        self._recent_short_count: Counter = Counter()
        self._recent_long_count: Counter = Counter()

    @staticmethod
    def _push_recent(window: deque, counts: Counter, line: int, limit: int) -> None:
        window.append(line)
        counts[line] += 1
        if len(window) > limit:
            old = window.popleft()
            counts[old] -= 1
            if counts[old] <= 0:
                del counts[old]

    def encode(self, rec: Mapping[str, Any]) -> Tuple[List[int], float]:
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
                hist = min(FIELD_SIZES[10] - 1, 1 + reuse)
            if line is not None:
                if line not in self.local_line_ids:
                    # First-touch order is stable under address relocation and
                    # still preserves equality/reuse within a core.  Exact
                    # cross-core equality is handled separately from raw line
                    # keys and is not exposed as an address-identity shortcut.
                    self.local_line_ids[line] = 1 + (
                        len(self.local_line_ids) % (FIELD_SIZES[9] - 1)
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
        if not 0 <= op_class < FIELD_SIZES[0]:
            op_class = 0
        if macro_pc and macro_pc not in self.local_pc_ids:
            self.local_pc_ids[macro_pc] = 1 + (
                len(self.local_pc_ids) % (FIELD_SIZES[7] - 1)
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

        return [
            op_class,
            _reg_bucket(rec),
            mem_kind,
            producer_bucket,
            reuse,
            stride,
            _branch_bucket(rec),
            local_pc_id,
            macro_pos,
            local_line_id,
            hist,
            mem_size,
            line_offset,
            _log_bucket(len(self._recent_short_count), FIELD_SIZES[13] - 1) if mem else 0,
            _log_bucket(len(self._recent_long_count), FIELD_SIZES[14] - 1) if mem else 0,
            0,  # xcore_role: populated from the current functional context
            0,  # xcore_fanout: populated from the current functional context
        ], producer_log


def chunk_summary(
    records: Sequence[Mapping[str, Any]],
    producer_logs: Sequence[float],
    feature_rows: Sequence[Sequence[int]],
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
                if ".switch" in s and s.endswith(".core")
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
