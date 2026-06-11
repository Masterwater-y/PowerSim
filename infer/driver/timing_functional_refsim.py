#!/usr/bin/env python3
"""Timing-aware functional ref-sim backend for deploy-side inference.

This is the inference-facing version of the offline evaluator in
scripts/_timing_functional_refsim_eval.py.  It consumes only functional trace
fields available at deploy time and exposes the same Python backend surface as
ref_sim_client.py expects.
"""
from __future__ import annotations

import heapq
import glob
from collections import Counter, OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional

import pyarrow.parquet as pq


COH_L1 = 1
COH_R_CLEAN = 2
COH_R_DIRTY = 3
COH_LLC = 4
COH_DRAM = 5
COH_WB = 6
COH_L2 = 7

L1_MISS_SET = {COH_R_CLEAN, COH_R_DIRTY, COH_LLC, COH_DRAM, COH_WB, COH_L2}
L2_MISS_SET = {COH_R_CLEAN, COH_R_DIRTY, COH_LLC, COH_DRAM, COH_WB}


def _bucket_count(n: int) -> int:
    if n <= 0:
        return 0
    if n == 1:
        return 1
    if n <= 3:
        return 2
    return 3


def _path_class(coh: int) -> int:
    if coh == COH_L1:
        return 0
    if coh == COH_L2:
        return 1
    if coh == COH_LLC:
        return 2
    if coh in (COH_R_CLEAN, COH_R_DIRTY, COH_WB):
        return 3
    if coh == COH_DRAM:
        return 4
    return 0


def make_pmu() -> Dict[str, int]:
    return {
        "l1d.loads": 0,
        "l1d.stores": 0,
        "l1d.load_misses": 0,
        "l1d.store_misses": 0,
        "l2.misses": 0,
        "llc.load_misses": 0,
        "llc.store_misses": 0,
        "cha.requests.reads": 0,
        "cha.requests.writes": 0,
        "cha.tor_inserts.ia_miss_drd": 0,
        "cha.dir_lookup.snp": 0,
        "cha.core_snp.any_one": 0,
    }


@dataclass(frozen=True)
class TimingFunctionalConfig:
    row_tick_stride: int = 256
    l1_hit_cycles: int = 4
    l2_hit_cycles: int = 12
    llc_hit_cycles: int = 36
    dram_cycles: int = 180
    store_wb_cycles: int = 80
    prefetch_latency_cycles: int = 40
    llc_capacity_factor: float = 1.0
    l1d_capacity_factor: float = 1.0
    prefetch_degree: int = 1
    prefetch_coverage: float = 0.25
    prefetch_visible_to_stores: bool = False
    remote_read_fold_to_llc: bool = True
    coherence_actions_affect_coh: bool = True
    invalidate_private_on_store: bool = False
    store_sharing_ttl_cycles: int = 0
    snp_coverage: float = 0.45
    private_sideband_load_coverage: float = 0.018
    private_sideband_store_coverage: float = 0.040
    l1_load_fold_l2_coverage: float = 0.41
    l1_load_fold_llc_coverage: float = 0.14
    l1_load_fold_min_miss_rate: float = 0.01
    l1_load_fold_max_miss_rate: float = 0.05
    l1_load_fold_min_llc_l2_ratio: float = 2.0


class SetAssocLRU:
    def __init__(self, size_b: int, assoc: int, line_b: int,
                 capacity_factor: float = 1.0):
        sets = max(1, int((size_b * capacity_factor) // max(1, assoc * line_b)))
        self.num_sets = sets
        self.assoc = max(1, int(assoc))
        self.lines = [OrderedDict() for _ in range(self.num_sets)]

    def _set(self, cl: int) -> OrderedDict:
        return self.lines[(cl // 64) % self.num_sets]

    def contains(self, cl: int) -> bool:
        return cl in self._set(cl)

    def touch(self, cl: int) -> bool:
        s = self._set(cl)
        hit = cl in s
        if hit:
            s.move_to_end(cl)
            return True
        s[cl] = None
        if len(s) > self.assoc:
            s.popitem(last=False)
        return False

    def invalidate(self, cl: int) -> None:
        self._set(cl).pop(cl, None)

    def peek_set_state(self, cl: int) -> tuple[int, int]:
        s = self._set(cl)
        if cl not in s:
            return len(s), len(s)
        keys = list(s.keys())
        return len(s), len(keys) - 1 - keys.index(cl)


class Directory:
    def __init__(self) -> None:
        self.lines: dict[int, dict] = {}
        self.snp = 0

    def _get(self, cl: int) -> dict:
        rec = self.lines.get(cl)
        if rec is None:
            rec = {"state": 0, "owner": -1, "sharers": set()}
            self.lines[cl] = rec
        return rec

    def probe(self, core_id: int, cl: int) -> tuple[int, bool, bool, int]:
        rec = self._get(cl)
        state = int(rec["state"])
        owner = int(rec["owner"])
        sharers = rec["sharers"]
        other_owns = owner >= 0 and owner != core_id
        dirty_owner = state == 3 and other_owns
        if state == 0:
            mesi_before = 0
        elif owner == core_id:
            mesi_before = state
        elif core_id in sharers:
            mesi_before = 1
        else:
            mesi_before = 0
        return mesi_before, other_owns, dirty_owner, len(sharers - {core_id})

    def invalidation_targets(self, core_id: int, cl: int, is_store: bool) -> set[int]:
        if not is_store:
            return set()
        rec = self._get(cl)
        state = int(rec["state"])
        owner = int(rec["owner"])
        sharers = rec["sharers"]
        if state in (2, 3):
            return {owner} if owner >= 0 and owner != core_id else set()
        if state == 1:
            return set(sharers - {core_id})
        return set()

    def update(self, core_id: int, cl: int, is_store: bool) -> None:
        rec = self._get(cl)
        state = int(rec["state"])
        owner = int(rec["owner"])
        sharers = rec["sharers"]
        if not is_store:
            if state == 0:
                rec["state"] = 2
                rec["owner"] = core_id
                rec["sharers"] = {core_id}
            elif state in (2, 3):
                if owner != core_id:
                    self.snp += 1
                    rec["state"] = 1
                    rec["owner"] = -1
                    rec["sharers"] = ({owner} if owner >= 0 else set()) | {core_id}
                else:
                    sharers.add(core_id)
            elif state == 1:
                sharers.add(core_id)
            return

        if state == 0:
            rec["state"] = 3
            rec["owner"] = core_id
            rec["sharers"] = {core_id}
        elif state in (2, 3):
            if owner != core_id:
                self.snp += 1
            rec["state"] = 3
            rec["owner"] = core_id
            rec["sharers"] = {core_id}
        elif state == 1:
            others = sharers - {core_id}
            if others:
                self.snp += 1
            rec["state"] = 3
            rec["owner"] = core_id
            rec["sharers"] = {core_id}


class TimingFunctionalBackend:
    """Functional-trace-only backend for inference_driver.py.

    State is advanced on probe, matching the current pybind backend semantics.
    """

    def __init__(self, profile: dict, config: Optional[TimingFunctionalConfig] = None):
        self.profile = profile
        self.cfg = config or TimingFunctionalConfig()
        cache = profile.get("cache", {})
        l1d = cache.get("l1d", {})
        l2 = cache.get("l2", {})
        l3 = cache.get("l3", {})
        self.line_b = int(l1d.get("line_b", 64))
        cores = int(profile.get("core", {}).get("num_cores", 4))
        self.l1d = [
            SetAssocLRU(int(l1d.get("size_b", 32768)), int(l1d.get("assoc", 8)),
                        self.line_b, self.cfg.l1d_capacity_factor)
            for _ in range(cores)
        ]
        self.l2 = [
            SetAssocLRU(int(l2.get("size_b", 262144)), int(l2.get("assoc", 8)),
                        self.line_b)
            for _ in range(cores)
        ]
        self.llc = SetAssocLRU(
            int(l3.get("size_b", 8388608)),
            int(l3.get("assoc", 16)),
            self.line_b,
            self.cfg.llc_capacity_factor,
        )
        self.dir = Directory()
        self.pending_fills: list[tuple[int, int, int, bool, bool]] = []
        self.outstanding: dict[int, int] = {}
        self.prefetched_only: set[int] = set()
        self.demand_seen: set[int] = set()
        self.last_core_touch: dict[int, dict[int, int]] = {}
        self.core_order: Counter[int] = Counter()
        self.global_order = 0
        self.pmu = make_pmu()
        self._snp_suppressed = 0
        self.load_hist = Counter()
        self.store_hist = Counter()
        self.diag = Counter()
        self.sideband_applied = False
        self.precomputed: Optional[dict[tuple[int, int, int], Dict]] = None
        self.sideband_row_snp: Optional[int] = None
        self.sideband_per_core_snp: Optional[int] = None

    def _tick(self, core_id: int) -> int:
        self.core_order[int(core_id)] += 1
        order = self.global_order
        self.global_order += 1
        return int(order * self.cfg.row_tick_stride)

    def _drain(self, tick: int) -> None:
        while self.pending_fills and self.pending_fills[0][0] <= tick:
            _, cid, cl, to_l1, is_prefetch = heapq.heappop(self.pending_fills)
            self.outstanding.pop(cl, None)
            self.llc.touch(cl)
            if is_prefetch:
                if cl not in self.demand_seen:
                    self.prefetched_only.add(cl)
            else:
                self.demand_seen.add(cl)
                self.prefetched_only.discard(cl)
            if (not is_prefetch) or to_l1:
                self.l2[cid].touch(cl)
            if to_l1 and not is_prefetch:
                self.l1d[cid].touch(cl)

    def _schedule_fill(self, tick: int, cid: int, cl: int, latency: int,
                       to_l1: bool = True, is_prefetch: bool = False) -> None:
        response_tick = tick + max(1, int(latency))
        old = self.outstanding.get(cl)
        if old is None or response_tick < old:
            self.outstanding[cl] = response_tick
        heapq.heappush(self.pending_fills, (response_tick, cid, cl, to_l1, is_prefetch))

    def _prefetch(self, tick: int, cid: int, cl: int, order: int) -> None:
        if self.cfg.prefetch_degree <= 0 or self.cfg.prefetch_coverage <= 0.0:
            return
        if self.cfg.prefetch_coverage < 1.0:
            h = ((cl >> 6) * 1103515245 + order * 12345 + cid * 97) & 0xFFFFFFFF
            if (h % 10000) >= int(self.cfg.prefetch_coverage * 10000):
                return
        for i in range(1, self.cfg.prefetch_degree + 1):
            pcl = cl + i * self.line_b
            if self.llc.contains(pcl) or pcl in self.outstanding:
                continue
            self._schedule_fill(tick, cid, pcl, self.cfg.prefetch_latency_cycles,
                                to_l1=False, is_prefetch=True)

    def _visible_llc_hit(self, cl: int, is_store: bool) -> bool:
        if not self.llc.contains(cl):
            return False
        if is_store and not self.cfg.prefetch_visible_to_stores and cl in self.prefetched_only:
            return False
        return True

    def _accumulate(self, is_store: bool, coh: int, count_pmu: bool = True) -> None:
        if not count_pmu:
            return
        if is_store:
            self.store_hist[coh] += 1
            self.pmu["l1d.stores"] += 1
            self.pmu["cha.requests.writes"] += 1
            if coh in L1_MISS_SET:
                self.pmu["l1d.store_misses"] += 1
            if coh in L2_MISS_SET:
                self.pmu["l2.misses"] += 1
            if coh == COH_DRAM:
                self.pmu["llc.store_misses"] += 1
            return
        self.load_hist[coh] += 1
        self.pmu["l1d.loads"] += 1
        self.pmu["cha.requests.reads"] += 1
        if coh in L1_MISS_SET:
            self.pmu["l1d.load_misses"] += 1
        if coh in L2_MISS_SET:
            self.pmu["l2.misses"] += 1
        if coh == COH_DRAM:
            self.pmu["llc.load_misses"] += 1
            self.pmu["cha.tor_inserts.ia_miss_drd"] += 1

    def _mem_access(self, core_id: int, paddr: int, is_store: bool,
                    size: int, seq: int = 0, thread_id: int = 0,
                    count_pmu: bool = True) -> Dict:
        cid = int(core_id)
        cl = int(paddr) & ~(self.line_b - 1)
        order = self.global_order
        tick = self._tick(cid)
        self._drain(tick)

        mesi_before, other_owns, dirty_owner, sharer_others = self.dir.probe(cid, cl)
        inval_targets = self.dir.invalidation_targets(cid, cl, bool(is_store))
        recent_other = False
        if is_store and self.cfg.store_sharing_ttl_cycles > 0:
            touches = self.last_core_touch.get(cl, {})
            recent_other = any(
                ocid != cid and (tick - otick) <= self.cfg.store_sharing_ttl_cycles
                for ocid, otick in touches.items()
            )

        l1_hit = self.l1d[cid].contains(cl)
        l2_hit = self.l2[cid].contains(cl)
        llc_hit = self._visible_llc_hit(cl, bool(is_store))
        coalesced = cl in self.outstanding

        if (self.cfg.coherence_actions_affect_coh and is_store
                and (other_owns or sharer_others > 0 or recent_other)):
            coh = COH_WB
            self._schedule_fill(tick, cid, cl, self.cfg.store_wb_cycles, to_l1=True)
        elif coalesced:
            coh = COH_LLC
        elif l1_hit:
            coh = COH_L1
            self.demand_seen.add(cl)
            self.prefetched_only.discard(cl)
            self.l1d[cid].touch(cl)
        elif l2_hit:
            coh = COH_L2
            self.demand_seen.add(cl)
            self.prefetched_only.discard(cl)
            self.l2[cid].touch(cl)
            self.l1d[cid].touch(cl)
        elif llc_hit:
            coh = COH_LLC
            self.demand_seen.add(cl)
            self.prefetched_only.discard(cl)
            self.llc.touch(cl)
            self.l2[cid].touch(cl)
            self.l1d[cid].touch(cl)
        elif (not is_store) and other_owns and self.cfg.remote_read_fold_to_llc:
            coh = COH_LLC
            self._schedule_fill(tick, cid, cl, self.cfg.llc_hit_cycles, to_l1=True)
        else:
            coh = COH_DRAM
            self._schedule_fill(tick, cid, cl, self.cfg.dram_cycles, to_l1=True)

        if self.cfg.invalidate_private_on_store:
            for ocid in inval_targets:
                if 0 <= ocid < len(self.l1d):
                    self.l1d[ocid].invalidate(cl)
                    self.l2[ocid].invalidate(cl)

        snp_before = self.dir.snp
        self.dir.update(cid, cl, bool(is_store))
        if not count_pmu:
            self._snp_suppressed += max(0, self.dir.snp - snp_before)
        self.last_core_touch.setdefault(cl, {})[cid] = tick
        if not is_store:
            self._prefetch(tick, cid, cl, order)
        self._accumulate(bool(is_store), coh, count_pmu=count_pmu)

        llc_res, llc_lru = self.llc.peek_set_state(cl)
        d_bank_id = (int(paddr) >> 6) & 15
        return {
            "mesi_before": int(mesi_before),
            "coh_oracle": int(coh),
            "sharer_bucket": _bucket_count(sharer_others),
            "owner_dist": 2 if other_owns else 0,
            "dirty_owner": 1 if dirty_owner else 0,
            "path_class": _path_class(coh),
            "inval_fanout": min(len(inval_targets), 15),
            "same_line_recent": 0,
            "oracle_source": 1,
            "d_mshr_depth": 1 if coalesced else 0,
            "dtlb_hit": 1,
            "d_walker_levels": 0,
            "d_walker_dram_misses": 0,
            "d_bank_id": int(d_bank_id),
            "d_llc_set_residency": min(int(llc_res), 255),
            "d_llc_set_lru_pos": min(int(llc_lru), 255),
            "i_path_class": 0,
            "i_coh_oracle": 0,
            "i_mesi_before": 0,
            "i_oracle_source": 1,
        }

    def _zero_attrs(self) -> Dict:
        return {
            "mesi_before": 0, "coh_oracle": 0, "sharer_bucket": 0,
            "owner_dist": 0, "dirty_owner": 0, "path_class": 0,
            "inval_fanout": 0, "same_line_recent": 0, "oracle_source": 1,
            "d_mshr_depth": 0, "dtlb_hit": 0, "d_walker_levels": 0,
            "d_walker_dram_misses": 0, "d_bank_id": 0,
            "d_llc_set_residency": 0, "d_llc_set_lru_pos": 0,
            "i_path_class": 0, "i_coh_oracle": 0, "i_mesi_before": 0,
            "i_oracle_source": 1,
        }

    def precompute_functional_dir(self, functional_dir: str,
                                  warmup_records_per_core: int = 0) -> None:
        """Advance state once in offline evaluator order and cache attrs by row id.

        The infer driver probes by per-core quantum chunks.  Advancing this
        backend during those probes changes cross-core ordering.  Precomputing
        in functional file order keeps deploy-side features aligned with the
        PMU calibration/evaluation script.

        The first warmup_records_per_core rows of each core are replayed into
        cache/directory state, but do not contribute to PMU counters.
        """
        out: dict[tuple[int, int, int], Dict] = {}
        per_core_order: dict[int, int] = {}
        per_core_accesses: list[tuple[int, int, int, bool, bool]] = []
        warmup_n = max(0, int(warmup_records_per_core))
        files = sorted(glob.glob(str(Path(functional_dir) / "functional.core*.parquet")))
        if not files:
            raise FileNotFoundError(f"no functional.core*.parquet under {functional_dir}")
        cols = [
            "core_id", "thread_id", "micro_seq", "paddr", "is_load",
            "is_store", "is_atomic", "size",
        ]
        for fp in files:
            table = pq.read_table(fp, columns=cols)
            for row in table.to_pylist():
                cid = int(row.get("core_id", 0))
                tid = int(row.get("thread_id", 0))
                seq = int(row.get("micro_seq", 0))
                key = (cid, tid, seq)
                is_load = bool(row.get("is_load", False))
                is_store = bool(row.get("is_store", False)) or bool(row.get("is_atomic", False))
                if is_load or is_store:
                    local_order = per_core_order.get(cid, 0)
                    per_core_order[cid] = local_order + 1
                    count_pmu = local_order >= warmup_n
                    cl = int(row.get("paddr", 0)) & ~(self.line_b - 1)
                    per_core_accesses.append((local_order, cid, cl, is_store, count_pmu))
                    out[key] = self._mem_access(
                        cid, int(row.get("paddr", 0)), is_store,
                        int(row.get("size", 0)), seq, tid,
                        count_pmu=count_pmu)
                else:
                    out[key] = self._zero_attrs()
        self.precomputed = out
        self.sideband_row_snp = max(0, int(self.dir.snp) - int(self._snp_suppressed))
        per_core_dir = Directory()
        per_core_snp_suppressed = 0
        for _, cid, cl, is_store, count_pmu in sorted(per_core_accesses, key=lambda x: (x[0], x[1])):
            snp_before = per_core_dir.snp
            per_core_dir.update(cid, cl, is_store)
            if not count_pmu:
                per_core_snp_suppressed += max(0, per_core_dir.snp - snp_before)
        self.sideband_per_core_snp = max(
            0, int(per_core_dir.snp) - int(per_core_snp_suppressed))

    def on_ifetch(self, core_id: int, macro_pc_cl: int) -> Dict:
        return {
            "i_path_class": 0,
            "i_coh_oracle": 0,
            "i_mesi_before": 0,
            "i_oracle_source": 1,
        }

    def on_mem_access(self, core_id: int, paddr: int, is_store: bool,
                      size: int, seq: int = 0, thread_id: int = 0) -> Dict:
        return self._mem_access(core_id, paddr, is_store, size, seq, thread_id)

    def on_request(self, core_id: int, paddr: int, is_store: bool,
                   size: int, seq: int = 0, thread_id: int = 0) -> Dict:
        return self.on_mem_access(core_id, paddr, is_store, size, seq, thread_id)

    def on_commit(self, core_id: int, seq: int = 0) -> None:
        pass

    def batch_probe(self, core_id: int, fields: Iterable[tuple]) -> list[Dict]:
        out = []
        for f in fields:
            # fields layout:
            # macro_pc, paddr, cl_paddr, is_load, is_store, is_atomic,
            # is_branch, size, micro_seq, thread_id
            paddr = int(f[1])
            is_store = bool(f[4]) or bool(f[5])
            is_load = bool(f[3])
            if self.precomputed is not None:
                key = (int(core_id), int(f[9]), int(f[8]))
                out.append(dict(self.precomputed.get(key, self._zero_attrs())))
                continue
            if is_load or is_store:
                out.append(self._mem_access(
                    int(core_id), paddr, is_store, int(f[7]), int(f[8]), int(f[9])))
            else:
                out.append(self._zero_attrs())
        return out

    def batch_window_update(self, core_id: int, fields, committed_mask) -> None:
        pass

    def reconcile(self, deltas=None, results=None) -> None:
        pass

    def _apply_final_corrections(self) -> None:
        if self.sideband_applied:
            return
        self.sideband_applied = True
        row_snp = int(self.sideband_row_snp if self.sideband_row_snp is not None else self.dir.snp)
        per_core_snp = int(
            self.sideband_per_core_snp
            if self.sideband_per_core_snp is not None
            else self.dir.snp
        )
        scaled_per_core_snp = int(round(per_core_snp * self.cfg.snp_coverage))
        snp = max(row_snp, scaled_per_core_snp)
        self.pmu["cha.dir_lookup.snp"] = snp
        self.pmu["cha.core_snp.any_one"] = snp
        self._apply_private_sideband(row_snp, per_core_snp, scaled_per_core_snp)
        self._apply_l1_load_fold()

    def _apply_private_sideband(
        self, row_snp: int, per_core_snp: int, scaled_per_core_snp: int
    ) -> None:
        pressure = max(0, scaled_per_core_snp - row_snp)
        if pressure <= 0:
            return
        load_extra = int(round(pressure * self.cfg.private_sideband_load_coverage))
        store_extra = int(round(pressure * self.cfg.private_sideband_store_coverage))
        load_extra = max(0, min(load_extra, self.load_hist.get(COH_L1, 0)))
        store_extra = max(0, min(store_extra, self.store_hist.get(COH_L1, 0)))
        if load_extra:
            self.load_hist[COH_L1] -= load_extra
            self.load_hist[COH_R_DIRTY] += load_extra
            self.pmu["l1d.load_misses"] += load_extra
            self.pmu["l2.misses"] += load_extra
        if store_extra:
            self.store_hist[COH_L1] -= store_extra
            self.store_hist[COH_WB] += store_extra
            self.pmu["l1d.store_misses"] += store_extra
            self.pmu["l2.misses"] += store_extra
        self.diag["private_sideband_row_snp"] = row_snp
        self.diag["private_sideband_per_core_snp"] = per_core_snp
        self.diag["private_sideband_scaled_per_core_snp"] = scaled_per_core_snp
        self.diag["private_sideband_pressure"] = pressure
        self.diag["private_sideband_load_extra"] = load_extra
        self.diag["private_sideband_store_extra"] = store_extra

    def _apply_l1_load_fold(self) -> None:
        l2_hits = self.load_hist.get(COH_L2, 0)
        llc_hits = self.load_hist.get(COH_LLC, 0)
        if l2_hits <= 0:
            return
        loads = max(1, self.pmu.get("l1d.loads", 0))
        miss_rate = self.pmu.get("l1d.load_misses", 0) / loads
        if miss_rate < self.cfg.l1_load_fold_min_miss_rate:
            return
        if miss_rate > self.cfg.l1_load_fold_max_miss_rate:
            return
        ratio = llc_hits / max(1, l2_hits)
        if ratio < self.cfg.l1_load_fold_min_llc_l2_ratio:
            return
        l2_extra = max(0, min(int(round(l2_hits * self.cfg.l1_load_fold_l2_coverage)), l2_hits))
        llc_extra = max(0, min(int(round(llc_hits * self.cfg.l1_load_fold_llc_coverage)), llc_hits))
        total = l2_extra + llc_extra
        if total <= 0:
            return
        self.load_hist[COH_L2] -= l2_extra
        self.load_hist[COH_LLC] -= llc_extra
        self.load_hist[COH_L1] += total
        self.pmu["l1d.load_misses"] -= total
        self.pmu["l2.misses"] -= llc_extra
        self.diag["l1_load_fold_l2_extra"] = l2_extra
        self.diag["l1_load_fold_llc_extra"] = llc_extra

    def drain_counters(self) -> Dict:
        self._drain(1 << 62)
        self._apply_final_corrections()
        return {
            "pmu": dict(self.pmu),
            "load_hist": {str(k): int(v) for k, v in self.load_hist.items()},
            "store_hist": {str(k): int(v) for k, v in self.store_hist.items()},
            "diag": dict(self.diag),
            "backend": "timing-functional",
        }
