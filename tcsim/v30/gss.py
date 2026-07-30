"""Causal reference implementation of the v30 cache-only GSS.

This module is deliberately a correctness/reference engine, not the final hot
path.  It consumes physical cache-line identities and the exact set/bank IDs
already decoded by v29.  An access observes the state *before* it changes that
state, then updates private L1D/L2 and the shared LLC.

The implementation models:

* private L1D with true LRU replacement;
* private L2 with TreePLRU replacement;
* shared, banked LLC with TreePLRU replacement;
* compact access-local and lagged per-core/global summaries.

It intentionally does not model timing, MSHRs, TLBs, coherence transients,
prefetch, or DRAM queues.  Those must not be inferred from these fields.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

import numpy as np


GSS_SCHEMA_VERSION = "tcsim-v30-gss-cache-reference-1"

GSS_CATEGORICAL_FIELDS = (
    "proxy_hit_level",          # unknown/L1/L2/LLC/memory
    "l1_pre_access_position",   # recency rank; assoc means absent
    "l2_pre_access_position",
    "llc_pre_access_position",
    "proxy_miss_kind",          # none/cold/re-reference/unknown
    "proxy_eviction_level",     # none/L1/L2/LLC
    "other_core_recent_line",   # no/yes
    "access_kind",              # nonmem/load/store/atomic
    "paddr_valid",              # no/yes
)

# Position cardinalities are finalized from geometry when a model contract is
# built.  The canonical profile is L1=8, L2=8, LLC=16.
GSS_CATEGORICAL_CARDINALITIES = (5, 9, 9, 17, 4, 4, 2, 4, 2)

GSS_CONTINUOUS_FIELDS = (
    "l1_set_residency_frac",
    "l2_set_residency_frac",
    "llc_set_residency_frac",
    "core_l1_miss_rate_ema",
    "core_l2_miss_rate_ema",
    "core_llc_miss_rate_ema",
    "core_recent_llc_miss_run",
    "core_eviction_rate_ema",
    "llc_bank_occupancy_frac",
    "active_union_footprint_frac",
)

# G1 is strictly access-local.  Per-core/global history remains in the sidecar
# for audits but is not routed into the first neural probe.
GSS_G1_CONTINUOUS_FIELDS = (
    "l1_set_residency_frac",
    "l2_set_residency_frac",
    "llc_set_residency_frac",
    "llc_bank_occupancy_frac",
)


@dataclass(frozen=True)
class GSSGeometry:
    """Cache geometry consumed by the cache-only GSS."""

    l1_sets: int
    l1_ways: int
    l2_sets: int
    l2_ways: int
    llc_sets_per_bank: int
    llc_ways: int
    llc_banks: int

    @classmethod
    def from_trace_meta(cls, meta: Mapping[str, Any]) -> "GSSGeometry":
        decoder = dict(meta["resource_decoder"])
        profile = dict(meta["uarch_profile"])["cache"]
        return cls(
            l1_sets=int(decoder["l1_sets"]),
            l1_ways=int(profile["l1d"]["assoc"]),
            l2_sets=int(decoder["l2_sets"]),
            l2_ways=int(profile["l2"]["assoc"]),
            llc_sets_per_bank=int(decoder["llc_sets_per_bank"]),
            llc_ways=int(profile["l3"]["assoc"]),
            llc_banks=int(decoder["llc_banks"]),
        )

    def validate(self) -> None:
        for name, value in (
            ("l1_sets", self.l1_sets),
            ("l1_ways", self.l1_ways),
            ("l2_sets", self.l2_sets),
            ("l2_ways", self.l2_ways),
            ("llc_sets_per_bank", self.llc_sets_per_bank),
            ("llc_ways", self.llc_ways),
            ("llc_banks", self.llc_banks),
        ):
            if int(value) <= 0:
                raise ValueError(f"{name} must be positive, got {value}")
        for name, ways in (("l2", self.l2_ways), ("llc", self.llc_ways)):
            if int(ways) & (int(ways) - 1):
                raise ValueError(f"{name} TreePLRU ways must be a power of two")


@dataclass
class _SetState:
    tags: list[int]
    last_touch: list[int]
    tree_bits: Optional[list[int]]


class _SetAssociativeCache:
    """Lazy set storage with LRU or binary-tree PLRU replacement."""

    def __init__(self, sets: int, ways: int, policy: str) -> None:
        self.sets = int(sets)
        self.ways = int(ways)
        self.policy = str(policy).lower()
        if self.policy not in {"lru", "tree_plru"}:
            raise ValueError(f"unsupported replacement policy {policy!r}")
        if self.policy == "tree_plru" and self.ways & (self.ways - 1):
            raise ValueError("TreePLRU requires power-of-two associativity")
        self._sets: Dict[int, _SetState] = {}
        self._parent: Optional["_SetAssociativeCache"] = None
        self.occupancy = 0

    def fork(self) -> "_SetAssociativeCache":
        """Create a touched-set copy-on-write view of this cache.

        A rollout preview normally touches only a small fraction of the cache.
        Sharing untouched sets avoids copying the complete private/shared tag
        state for every model window while guaranteeing that preview accesses
        cannot mutate the canonical cache.
        """
        child = object.__new__(_SetAssociativeCache)
        child.sets = self.sets
        child.ways = self.ways
        child.policy = self.policy
        child._sets = {}
        child._parent = self
        child.occupancy = int(self.occupancy)
        return child

    @staticmethod
    def _copy_state(state: _SetState) -> _SetState:
        return _SetState(
            tags=list(state.tags),
            last_touch=list(state.last_touch),
            tree_bits=(
                None if state.tree_bits is None else list(state.tree_bits)
            ),
        )

    def _get(self, set_id: int, create: bool) -> Optional[_SetState]:
        set_id = int(set_id)
        if set_id < 0 or set_id >= self.sets:
            raise ValueError(f"set {set_id} outside [0,{self.sets})")
        state = self._sets.get(set_id)
        if state is None and self._parent is not None:
            parent_state = self._parent._get(set_id, False)
            if parent_state is not None:
                if not create:
                    return parent_state
                state = self._copy_state(parent_state)
                self._sets[set_id] = state
        if state is None and create:
            state = _SetState(
                tags=[-1] * self.ways,
                last_touch=[-1] * self.ways,
                tree_bits=(
                    [0] * (self.ways - 1)
                    if self.policy == "tree_plru" else None
                ),
            )
            self._sets[set_id] = state
        return state

    def peek(self, set_id: int, tag: int) -> Tuple[bool, int, int]:
        state = self._get(set_id, False)
        if state is None:
            return False, self.ways, 0
        residency = 0
        hit_way = -1
        for way, current in enumerate(state.tags):
            if current >= 0:
                residency += 1
            if current == int(tag):
                hit_way = way
        if hit_way < 0:
            return False, self.ways, residency
        timestamp = state.last_touch[hit_way]
        # TreePLRU has no total ordering.  The exposed position is therefore a
        # deterministic recency rank while victim selection still uses PLRU.
        position = sum(
            1 for current, touched in zip(state.tags, state.last_touch)
            if current >= 0 and touched > timestamp
        )
        return True, int(position), residency

    def _tree_victim(self, state: _SetState) -> int:
        assert state.tree_bits is not None
        node = 0
        way = 0
        depth = int(math.log2(self.ways))
        for _ in range(depth):
            direction = int(state.tree_bits[node])
            way = way * 2 + direction
            node = node * 2 + 1 + direction
        return way

    def _tree_touch(self, state: _SetState, way: int) -> None:
        assert state.tree_bits is not None
        node = 0
        low = 0
        span = self.ways
        while span > 1:
            half = span // 2
            direction = 0 if way < low + half else 1
            # The bit points to the subtree preferred for the next victim.
            state.tree_bits[node] = 1 - direction
            node = node * 2 + 1 + direction
            if direction:
                low += half
            span = half

    def access(self, set_id: int, tag: int, sequence: int) -> int:
        state = self._get(set_id, True)
        assert state is not None
        tag = int(tag)
        way = -1
        for index, current in enumerate(state.tags):
            if current == tag:
                way = index
                break
        evicted = -1
        if way < 0:
            for index, current in enumerate(state.tags):
                if current < 0:
                    way = index
                    self.occupancy += 1
                    break
        if way < 0:
            if self.policy == "lru":
                way = min(range(self.ways), key=state.last_touch.__getitem__)
            else:
                way = self._tree_victim(state)
            evicted = int(state.tags[way])
        state.tags[way] = tag
        state.last_touch[way] = int(sequence)
        if self.policy == "tree_plru":
            self._tree_touch(state, way)
        return evicted


@dataclass
class _CoreSummary:
    l1_miss_ema: float = 0.0
    l2_miss_ema: float = 0.0
    llc_miss_ema: float = 0.0
    eviction_ema: float = 0.0
    llc_miss_run: int = 0


@dataclass(frozen=True)
class GSSAccessFeatures:
    categorical: Tuple[int, ...]
    continuous: Tuple[float, ...]


class GSSFeatureEngine:
    """One canonical cache-only state shared by all cores in one trace."""

    def __init__(
        self,
        geometry: GSSGeometry,
        *,
        ema_alpha: float = 0.02,
        recent_horizon_events: int = 4096,
    ) -> None:
        geometry.validate()
        self.geometry = geometry
        self.ema_alpha = float(ema_alpha)
        self.recent_horizon_events = int(recent_horizon_events)
        self.l1: Dict[int, _SetAssociativeCache] = {}
        self.l2: Dict[int, _SetAssociativeCache] = {}
        self.llc = _SetAssociativeCache(
            geometry.llc_sets_per_bank * geometry.llc_banks,
            geometry.llc_ways,
            "tree_plru",
        )
        self.core_summary: Dict[int, _CoreSummary] = {}
        self.last_touch: Dict[int, Tuple[int, int]] = {}
        self.seen_lines: set[int] = set()
        self._state_parent: Optional["GSSFeatureEngine"] = None
        self._unique_lines = 0
        self.llc_bank_occupancy = [0] * geometry.llc_banks
        self.events = 0
        self.l1d_load_misses = 0
        self.l1d_store_misses = 0
        self.l2_load_misses = 0
        self.l2_store_misses = 0
        self.llc_misses = 0

    def fork(self) -> "GSSFeatureEngine":
        """Return an isolated transactional preview over canonical state.

        Cache sets use copy-on-write overlays.  Large line-history tables use
        parent lookups plus small local deltas.  Per-core scalar summaries are
        copied because their size is O(number of cores), not O(cache lines).
        The returned engine is intentionally disposable: accepted UOPs are
        replayed on the canonical engine instead of merging a full preview.
        """
        child = object.__new__(GSSFeatureEngine)
        child.geometry = self.geometry
        child.ema_alpha = self.ema_alpha
        child.recent_horizon_events = self.recent_horizon_events
        child.l1 = {core: cache.fork() for core, cache in self.l1.items()}
        child.l2 = {core: cache.fork() for core, cache in self.l2.items()}
        child.llc = self.llc.fork()
        child.core_summary = {
            core: _CoreSummary(
                l1_miss_ema=value.l1_miss_ema,
                l2_miss_ema=value.l2_miss_ema,
                llc_miss_ema=value.llc_miss_ema,
                eviction_ema=value.eviction_ema,
                llc_miss_run=value.llc_miss_run,
            )
            for core, value in self.core_summary.items()
        }
        child.last_touch = {}
        child.seen_lines = set()
        child._state_parent = self
        child._unique_lines = int(self._unique_lines)
        child.llc_bank_occupancy = list(self.llc_bank_occupancy)
        child.events = int(self.events)
        child.l1d_load_misses = int(self.l1d_load_misses)
        child.l1d_store_misses = int(self.l1d_store_misses)
        child.l2_load_misses = int(self.l2_load_misses)
        child.l2_store_misses = int(self.l2_store_misses)
        child.llc_misses = int(self.llc_misses)
        return child

    def _last_touch_of(self, line: int) -> Optional[Tuple[int, int]]:
        value = self.last_touch.get(int(line))
        if value is not None:
            return value
        if self._state_parent is not None:
            return self._state_parent._last_touch_of(int(line))
        return None

    def _has_seen_line(self, line: int) -> bool:
        if int(line) in self.seen_lines:
            return True
        return bool(
            self._state_parent is not None
            and self._state_parent._has_seen_line(int(line))
        )

    def _private(self, core: int) -> Tuple[_SetAssociativeCache, _SetAssociativeCache]:
        core = int(core)
        if core not in self.l1:
            self.l1[core] = _SetAssociativeCache(
                self.geometry.l1_sets, self.geometry.l1_ways, "lru",
            )
            self.l2[core] = _SetAssociativeCache(
                self.geometry.l2_sets, self.geometry.l2_ways, "tree_plru",
            )
            self.core_summary[core] = _CoreSummary()
        return self.l1[core], self.l2[core]

    @staticmethod
    def _ema(old: float, value: bool, alpha: float) -> float:
        return (1.0 - alpha) * float(old) + alpha * float(bool(value))

    def access(
        self,
        *,
        core: int,
        physical_line: int,
        l1_set: int,
        l2_set: int,
        llc_set: int,
        llc_bank: int,
        access_kind: int,
    ) -> GSSAccessFeatures:
        """Observe pre-access state and atomically commit one functional access."""
        core = int(core)
        line = int(physical_line)
        access_kind = int(access_kind)
        if line < 0:
            return GSSAccessFeatures(
                categorical=(0, 0, 0, 0, 3, 0, 0, access_kind, 0),
                continuous=(0.0,) * len(GSS_CONTINUOUS_FIELDS),
            )
        l1, l2 = self._private(core)
        llc_combined_set = int(llc_bank) * self.geometry.llc_sets_per_bank + int(llc_set)
        l1_hit, l1_pos, l1_res = l1.peek(l1_set, line)
        l2_hit, l2_pos, l2_res = l2.peek(l2_set, line)
        llc_hit, llc_pos, llc_res = self.llc.peek(llc_combined_set, line)
        hit_level = 1 if l1_hit else 2 if l2_hit else 3 if llc_hit else 4
        store_like = int(access_kind) in (2, 3)
        if not l1_hit:
            if store_like:
                self.l1d_store_misses += 1
            else:
                self.l1d_load_misses += 1
        if not l1_hit and not l2_hit:
            if store_like:
                self.l2_store_misses += 1
            else:
                self.l2_load_misses += 1
        if hit_level == 4:
            self.llc_misses += 1
        line_seen = self._has_seen_line(line)
        miss_kind = 0 if hit_level < 4 else (1 if not line_seen else 2)
        previous = self._last_touch_of(line)
        other_recent = int(
            previous is not None
            and int(previous[0]) != core
            and self.events - int(previous[1]) <= self.recent_horizon_events
        )
        summary = self.core_summary[core]
        bank_capacity = self.geometry.llc_sets_per_bank * self.geometry.llc_ways
        bank_occupancy = self.llc_bank_occupancy[int(llc_bank)]
        union_capacity = bank_capacity * self.geometry.llc_banks
        continuous = (
            l1_res / self.geometry.l1_ways,
            l2_res / self.geometry.l2_ways,
            llc_res / self.geometry.llc_ways,
            summary.l1_miss_ema,
            summary.l2_miss_ema,
            summary.llc_miss_ema,
            min(1.0, math.log2(1.0 + summary.llc_miss_run) / 16.0),
            summary.eviction_ema,
            bank_occupancy / max(1, bank_capacity),
            min(1.0, self._unique_lines / max(1, union_capacity)),
        )

        self.events += 1
        evicted_l1 = l1.access(l1_set, line, self.events)
        evicted_l2 = l2.access(l2_set, line, self.events)
        evicted_llc = self.llc.access(llc_combined_set, line, self.events)
        if not llc_hit and llc_res < self.geometry.llc_ways:
            self.llc_bank_occupancy[int(llc_bank)] += 1
        eviction_level = 3 if evicted_llc >= 0 else 2 if evicted_l2 >= 0 else 1 if evicted_l1 >= 0 else 0
        l1_miss = not l1_hit
        l2_miss = not l2_hit
        llc_miss = not llc_hit
        summary.l1_miss_ema = self._ema(summary.l1_miss_ema, l1_miss, self.ema_alpha)
        summary.l2_miss_ema = self._ema(summary.l2_miss_ema, l2_miss, self.ema_alpha)
        summary.llc_miss_ema = self._ema(summary.llc_miss_ema, llc_miss, self.ema_alpha)
        summary.eviction_ema = self._ema(
            summary.eviction_ema, eviction_level > 0, self.ema_alpha,
        )
        summary.llc_miss_run = summary.llc_miss_run + 1 if llc_miss else 0
        if not line_seen:
            self.seen_lines.add(line)
            self._unique_lines += 1
        self.last_touch[line] = (core, self.events)
        return GSSAccessFeatures(
            categorical=(
                hit_level,
                l1_pos,
                l2_pos,
                llc_pos,
                miss_kind,
                eviction_level,
                other_recent,
                access_kind,
                1,
            ),
            continuous=tuple(float(value) for value in continuous),
        )

    def state_summary(self) -> Mapping[str, int]:
        return {
            "events": int(self.events),
            "cores": len(self.l1),
            "unique_lines": int(self._unique_lines),
            "l1_occupancy": sum(cache.occupancy for cache in self.l1.values()),
            "l2_occupancy": sum(cache.occupancy for cache in self.l2.values()),
            "llc_occupancy": int(self.llc.occupancy),
            "l1d_load_misses": int(self.l1d_load_misses),
            "l1d_store_misses": int(self.l1d_store_misses),
            "l1d_misses": int(self.l1d_load_misses + self.l1d_store_misses),
            "l2_load_misses": int(self.l2_load_misses),
            "l2_store_misses": int(self.l2_store_misses),
            "l2_misses": int(self.l2_load_misses + self.l2_store_misses),
            "llc_misses": int(self.llc_misses),
        }

    def preview_batch(self, events: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """Reference batch API shared with the native deployment engine.

        Event columns are core, physical line, L1 set, L2 set, LLC set,
        LLC bank, and access kind.  This method is deliberately retained as a
        golden fallback; production rollout should use the native backend.
        """
        events = np.asarray(events, dtype=np.int64)
        if events.ndim != 2 or events.shape[1] != 7:
            raise ValueError("GSS events must have shape [N,7]")
        categorical = np.zeros(
            (len(events), len(GSS_CATEGORICAL_FIELDS)), dtype=np.int64,
        )
        continuous = np.zeros(
            (len(events), len(GSS_CONTINUOUS_FIELDS)), dtype=np.float32,
        )
        shadow = self.fork()
        for row, values in enumerate(events):
            feature = shadow.access(
                core=int(values[0]), physical_line=int(values[1]),
                l1_set=int(values[2]), l2_set=int(values[3]),
                llc_set=int(values[4]), llc_bank=int(values[5]),
                access_kind=int(values[6]),
            )
            categorical[row] = feature.categorical
            continuous[row] = feature.continuous
        return categorical, continuous

    def commit_batch(self, events: np.ndarray) -> None:
        events = np.asarray(events, dtype=np.int64)
        if events.ndim != 2 or events.shape[1] != 7:
            raise ValueError("GSS events must have shape [N,7]")
        for values in events:
            self.access(
                core=int(values[0]), physical_line=int(values[1]),
                l1_set=int(values[2]), l2_set=int(values[3]),
                llc_set=int(values[4]), llc_bank=int(values[5]),
                access_kind=int(values[6]),
            )
