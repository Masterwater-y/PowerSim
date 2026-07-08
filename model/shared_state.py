"""Deployable shared-system feature engine.

This is the first TSim-side implementation of the v27 shared-state plan.  It is
intentionally a lightweight Python state machine, not a timing simulator.  The
state uses only functional trace inputs that are available at deployment time:
core id, load/store/atomic flags, and address/cacheline.  It maintains lagged
cacheline ownership/sharer/history proxies and exposes bucketized features for
the model.

The design keeps the interface replaceable by the C++ shared_system peek API
later.  Current features are conservative proxies for coherence state; they do
not use gem5 labels such as path_class, coh_oracle, miss status, or MSHR depth.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple


SS_UOP_FIELD_NAMES = [
    "ss_owner_dist",
    "ss_last_writer_rel",
    "ss_sharer_bucket",
    "ss_line_recent_bucket",
    "ss_local_present",
    "ss_remote_recent",
    "ss_conflict_kind",
    "ss_time_since_touch",
]

SS_UOP_FIELD_SIZES = [
    5,   # nonmem / no-owner / self-owner / remote-owner / shared
    5,   # nonmem / none / self / other / unknown
    8,   # capped sharer count bucket
    12,  # log2 touch count bucket
    2,   # current core has seen/touched this line
    4,   # none / remote last touch / remote last writer / both
    8,   # no conflict plus common read/write-after-remote classes
    16,  # log2 distance since last touch
]

SS_CORE_FEATURE_KEYS = [
    "ss_core_log1p_events",
    "ss_core_mem_ema",
    "ss_core_store_ema",
    "ss_core_remote_writer_ema",
    "ss_core_owner_change_ema",
    "ss_core_multi_sharer_store_ema",
    "ss_core_hot_remote_ema",
    "ss_core_unique_lines_log",
]

SS_GLOBAL_FEATURE_KEYS = [
    "ss_log1p_events_seen",
    "ss_active_lines_log",
    "ss_active_dirty_lines_log",
    "ss_global_remote_writer_ema",
    "ss_global_owner_change_ema",
    "ss_global_multi_sharer_store_ema",
]


def is_mem_rec(rec: Mapping) -> bool:
    return bool(
        int(rec.get("is_load", 0) or 0)
        or int(rec.get("is_store", 0) or 0)
        or int(rec.get("is_atomic", 0) or 0)
        or int(rec.get("is_mem", 0) or 0)
    )


def is_store_like(rec: Mapping) -> bool:
    return bool(
        int(rec.get("is_store", 0) or 0)
        or int(rec.get("is_atomic", 0) or 0)
    )


def cacheline_key(rec: Mapping) -> int:
    for key in ("cacheline_paddr", "cacheline_addr"):
        v = int(rec.get(key, 0) or 0)
        if v:
            return v & ~63
    for key in ("paddr", "vaddr"):
        v = int(rec.get(key, 0) or 0)
        if v:
            return v & ~63
    return 0


def _log_bucket(v: float, buckets: int) -> int:
    if v <= 0:
        return 0
    return max(0, min(int(buckets) - 1, int(math.log2(v)) + 1))


def _rate_ema(old: float, value: float, alpha: float) -> float:
    return (1.0 - alpha) * float(old) + alpha * float(value)


@dataclass
class LineState:
    owner: int = -1
    last_writer: int = -1
    last_touch_core: int = -1
    last_touch_seq: int = -1
    touch_count: int = 0
    sharers: set[int] = field(default_factory=set)


@dataclass
class CoreState:
    events: int = 0
    stores: int = 0
    remote_writer: int = 0
    owner_change: int = 0
    multi_sharer_store: int = 0
    hot_remote: int = 0
    unique_lines: set[int] = field(default_factory=set)
    mem_ema: float = 0.0
    store_ema: float = 0.0
    remote_writer_ema: float = 0.0
    owner_change_ema: float = 0.0
    multi_sharer_store_ema: float = 0.0
    hot_remote_ema: float = 0.0


class SharedStateFeatureEngine:
    """Line-owner/sharer history feature engine.

    The engine is intentionally deterministic and cheap.  It supports two
    update modes:
      * advance_to_tick() for offline teacher-state dataset construction.
      * replay_window() for deploy-side online state updates.
    """

    def __init__(self, ema_alpha: float = 0.02):
        self.ema_alpha = float(ema_alpha)
        self.lines: Dict[int, LineState] = {}
        self.cores: Dict[int, CoreState] = {}
        self.events_seen = 0
        self.global_remote_writer_ema = 0.0
        self.global_owner_change_ema = 0.0
        self.global_multi_sharer_store_ema = 0.0
        self._teacher_events: List[Tuple[int, int, dict]] = []
        self._teacher_pos = 0

    @classmethod
    def from_merged_by_core(
        cls,
        merged_by_core: Mapping[int, Sequence[dict]],
        ema_alpha: float = 0.02,
    ) -> "SharedStateFeatureEngine":
        eng = cls(ema_alpha=ema_alpha)
        events: List[Tuple[int, int, dict]] = []
        for core, seq in merged_by_core.items():
            for rec in seq:
                if not is_mem_rec(rec):
                    continue
                tick = int(rec.get("_commit_tick", rec.get("commit_tick", 0)) or 0)
                if tick <= 0:
                    continue
                events.append((tick, int(core), rec))
        events.sort(key=lambda x: (x[0], x[1], int(x[2].get("micro_seq", 0) or 0)))
        eng._teacher_events = events
        return eng

    def _core(self, core: int) -> CoreState:
        core = int(core)
        st = self.cores.get(core)
        if st is None:
            st = CoreState()
            self.cores[core] = st
        return st

    def advance_to_tick(self, tick: int) -> None:
        tick = int(tick)
        events = self._teacher_events
        while self._teacher_pos < len(events) and events[self._teacher_pos][0] < tick:
            _, core, rec = events[self._teacher_pos]
            self.update_event(core, rec)
            self._teacher_pos += 1

    def peek_uop_fields(self, core: int, rec: Mapping) -> List[int]:
        if not is_mem_rec(rec):
            return [0] * len(SS_UOP_FIELD_NAMES)
        line = cacheline_key(rec)
        if not line:
            return [0] * len(SS_UOP_FIELD_NAMES)
        core = int(core)
        st = self.lines.get(line)
        if st is None:
            return [1, 1, 0, 0, 0, 0, 1, 1]

        sharer_count = len(st.sharers)
        if st.owner < 0:
            owner_dist = 4 if sharer_count > 1 else 1
        elif st.owner == core:
            owner_dist = 2
        else:
            owner_dist = 3

        if st.last_writer < 0:
            writer_rel = 1
        elif st.last_writer == core:
            writer_rel = 2
        else:
            writer_rel = 3

        local_present = int(core in st.sharers or st.owner == core)
        last_touch_other = int(st.last_touch_core >= 0 and st.last_touch_core != core)
        last_writer_other = int(st.last_writer >= 0 and st.last_writer != core)
        remote_recent = 0
        if last_touch_other:
            remote_recent = 1
        if last_writer_other:
            remote_recent = 2
        if last_touch_other and last_writer_other:
            remote_recent = 3

        store = is_store_like(rec)
        conflict = 1
        if not store and last_writer_other:
            conflict = 2
        elif store and sharer_count > 1 and core in st.sharers:
            conflict = 6
        elif store and last_writer_other:
            conflict = 4
        elif store and any(c != core for c in st.sharers):
            conflict = 3
        elif store and st.owner >= 0 and st.owner != core:
            conflict = 5
        elif remote_recent and st.touch_count >= 8:
            conflict = 7

        since = (
            self.events_seen - st.last_touch_seq
            if st.last_touch_seq >= 0 else 0
        )
        return [
            owner_dist,
            writer_rel,
            min(7, sharer_count),
            _log_bucket(st.touch_count, 12),
            local_present,
            remote_recent,
            conflict,
            _log_bucket(since, 16) if since > 0 else 1,
        ]

    def update_event(self, core: int, rec: Mapping) -> None:
        if not is_mem_rec(rec):
            return
        line = cacheline_key(rec)
        if not line:
            return
        core = int(core)
        cst = self._core(core)
        st = self.lines.get(line)
        if st is None:
            st = LineState()
            self.lines[line] = st

        store = is_store_like(rec)
        remote_writer = int(st.last_writer >= 0 and st.last_writer != core)
        owner_change = int(store and st.owner >= 0 and st.owner != core)
        multi_sharer_store = int(store and any(c != core for c in st.sharers))
        hot_remote = int(remote_writer and st.touch_count >= 8)

        self.events_seen += 1
        cst.events += 1
        cst.stores += int(store)
        cst.remote_writer += remote_writer
        cst.owner_change += owner_change
        cst.multi_sharer_store += multi_sharer_store
        cst.hot_remote += hot_remote
        cst.unique_lines.add(line)
        a = self.ema_alpha
        cst.mem_ema = _rate_ema(cst.mem_ema, 1.0, a)
        cst.store_ema = _rate_ema(cst.store_ema, float(store), a)
        cst.remote_writer_ema = _rate_ema(cst.remote_writer_ema, remote_writer, a)
        cst.owner_change_ema = _rate_ema(cst.owner_change_ema, owner_change, a)
        cst.multi_sharer_store_ema = _rate_ema(
            cst.multi_sharer_store_ema, multi_sharer_store, a)
        cst.hot_remote_ema = _rate_ema(cst.hot_remote_ema, hot_remote, a)
        self.global_remote_writer_ema = _rate_ema(
            self.global_remote_writer_ema, remote_writer, a)
        self.global_owner_change_ema = _rate_ema(
            self.global_owner_change_ema, owner_change, a)
        self.global_multi_sharer_store_ema = _rate_ema(
            self.global_multi_sharer_store_ema, multi_sharer_store, a)

        if store:
            st.owner = core
            st.last_writer = core
            st.sharers = {core}
        else:
            if st.owner < 0:
                st.sharers.add(core)
            elif st.owner == core:
                st.sharers.add(core)
            else:
                st.sharers.add(core)
                st.sharers.add(st.owner)
                st.owner = -1
        st.last_touch_core = core
        st.last_touch_seq = self.events_seen
        st.touch_count += 1

    def core_features(self, cores: Sequence[int]) -> List[List[float]]:
        rows: List[List[float]] = []
        for core in cores:
            st = self.cores.get(int(core), CoreState())
            rows.append([
                math.log1p(st.events),
                st.mem_ema,
                st.store_ema,
                st.remote_writer_ema,
                st.owner_change_ema,
                st.multi_sharer_store_ema,
                st.hot_remote_ema,
                math.log1p(len(st.unique_lines)),
            ])
        return rows

    def global_features(self) -> List[float]:
        dirty = sum(1 for st in self.lines.values() if st.owner >= 0)
        return [
            math.log1p(self.events_seen),
            math.log1p(len(self.lines)),
            math.log1p(dirty),
            self.global_remote_writer_ema,
            self.global_owner_change_ema,
            self.global_multi_sharer_store_ema,
        ]

    def window_features(
        self,
        per_core_windows: Mapping[int, Tuple[Sequence[dict], Mapping] | Sequence[dict]],
        cores: Sequence[int],
    ) -> dict:
        uop: Dict[int, List[List[int]]] = {}
        for core in cores:
            item = per_core_windows[int(core)]
            win = item[0] if isinstance(item, tuple) else item
            uop[int(core)] = [self.peek_uop_fields(int(core), rec) for rec in win]
        return {
            "uop": uop,
            "core": {int(c): row for c, row in zip(cores, self.core_features(cores))},
            "global": self.global_features(),
        }

    def replay_window(
        self,
        per_core_wins: Mapping[int, Sequence[dict]],
        cores: Sequence[int],
        start_cycles: Mapping[int, float] | None = None,
        cpi_by_core: Mapping[int, float] | None = None,
    ) -> None:
        events: List[Tuple[float, int, int, dict]] = []
        for core in cores:
            core = int(core)
            win = list(per_core_wins.get(core, []))
            mem = [(idx, rec) for idx, rec in enumerate(win) if is_mem_rec(rec)]
            if not mem:
                continue
            start = float((start_cycles or {}).get(core, 0.0))
            cpi = float((cpi_by_core or {}).get(core, 1.0))
            for local_i, (idx, rec) in enumerate(mem):
                t = start + cpi * float(idx + 1)
                # local_i stabilizes ordering when many events share a time.
                events.append((t, core, local_i, rec))
        events.sort(key=lambda x: (x[0], x[1], x[2]))
        for _, core, _, rec in events:
            self.update_event(core, rec)
