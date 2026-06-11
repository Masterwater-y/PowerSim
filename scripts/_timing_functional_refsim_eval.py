#!/usr/bin/env python3
"""Evaluate a timing-aware functional-only d-side ref-sim against oracle PMU.

This script is intentionally offline and self-contained.  It is the Phase-1
playground for the timing-aware functional ref-sim design: use only fields that
the driver can produce from functional trace plus an estimated request tick,
then compare the estimated PMU/coh distribution against gem5/Ruby oracle.

No oracle request fields are used as simulator input.  Oracle fields are read
only for evaluation.
"""
from __future__ import annotations

import argparse
import glob
import heapq
import json
import math
from collections import Counter, OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pyarrow.parquet as pq


COH_L1 = 1
COH_R_CLEAN = 2
COH_R_DIRTY = 3
COH_LLC = 4
COH_DRAM = 5
COH_WB = 6
COH_L2 = 7

COH_NAMES = {
    COH_L1: "L1_HIT",
    COH_R_CLEAN: "R_CLEAN",
    COH_R_DIRTY: "R_DIRTY",
    COH_LLC: "LLC_HIT",
    COH_DRAM: "DRAM",
    COH_WB: "WB",
    COH_L2: "L2_HIT",
}

L1_MISS_SET = {COH_R_CLEAN, COH_R_DIRTY, COH_LLC, COH_DRAM, COH_WB, COH_L2}
L2_MISS_SET = {COH_R_CLEAN, COH_R_DIRTY, COH_LLC, COH_DRAM, COH_WB}


@dataclass(frozen=True)
class Access:
    tick: int
    order: int
    core_id: int
    thread_id: int
    seq: int
    cl: int
    is_store: bool
    size: int


class SetAssocLRU:
    def __init__(self, size_b: int, assoc: int, line_b: int, capacity_factor: float = 1.0):
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
        # OrderedDict is LRU..MRU, convert to MRU position roughly matching a
        # small integer feature; exact value is only diagnostic in this script.
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

    def probe(self, core_id: int, cl: int, is_store: bool) -> tuple[int, bool, bool, int]:
        rec = self._get(cl)
        state = int(rec["state"])
        owner = int(rec["owner"])
        sharers = rec["sharers"]
        other_owns = owner >= 0 and owner != core_id
        sc = len(sharers - {core_id})
        dirty_owner = state == 3 and other_owns
        if state == 0:
            mesi_before = 0
        elif owner == core_id:
            mesi_before = state
        elif core_id in sharers:
            mesi_before = 1
        else:
            mesi_before = 0
        return mesi_before, other_owns, dirty_owner, sc

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


class TimingFunctionalRefSim:
    def __init__(self, profile: dict, args: argparse.Namespace):
        cache = profile.get("cache", {})
        l1d = cache.get("l1d", {})
        l2 = cache.get("l2", {})
        l3 = cache.get("l3", {})
        self.line_b = int(l1d.get("line_b", 64))
        cores = int(profile.get("core", {}).get("num_cores", 4))
        self.l1d = [
            SetAssocLRU(
                int(l1d.get("size_b", 32768)),
                int(l1d.get("assoc", 8)),
                self.line_b,
                float(args.l1d_capacity_factor),
            )
            for _ in range(cores)
        ]
        self.l2 = [
            SetAssocLRU(int(l2.get("size_b", 262144)), int(l2.get("assoc", 8)), self.line_b)
            for _ in range(cores)
        ]
        self.llc = SetAssocLRU(
            int(l3.get("size_b", 8388608)),
            int(l3.get("assoc", 16)),
            self.line_b,
            float(args.llc_capacity_factor),
        )
        self.dir = Directory()
        self.pending_fills: list[tuple[int, int, int, bool, bool]] = []
        self.outstanding: dict[int, int] = {}
        self.prefetched_only: set[int] = set()
        self.demand_seen: set[int] = set()
        self.last_core_touch: dict[int, dict[int, int]] = {}
        self.mshr_entries = int(args.mshr_entries)
        self.remote_read_fold_to_llc = bool(args.remote_read_fold_to_llc)
        self.prefetch_visible_to_stores = bool(args.prefetch_visible_to_stores)
        self.prefetch_coverage = float(args.prefetch_coverage)
        self.store_sharing_ttl_cycles = int(args.store_sharing_ttl_cycles)
        self.coherence_actions_affect_coh = bool(args.coherence_actions_affect_coh)
        self.invalidate_private_on_store = bool(args.invalidate_private_on_store)
        self.snp_coverage = float(args.snp_coverage)
        self.prefetch_degree = int(args.prefetch_degree)
        self.prefetch_latency = int(args.prefetch_latency_cycles)
        self.lat = {
            COH_L1: int(args.l1_hit_cycles),
            COH_L2: int(args.l2_hit_cycles),
            COH_LLC: int(args.llc_hit_cycles),
            COH_DRAM: int(args.dram_cycles),
            COH_WB: int(args.store_wb_cycles),
        }
        self.load_hist = Counter()
        self.store_hist = Counter()
        self.pred_pmu = make_pmu()
        self.diag = Counter()

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

    def _schedule_fill(
        self, tick: int, cid: int, cl: int, latency: int,
        to_l1: bool = True, is_prefetch: bool = False,
    ) -> None:
        response_tick = tick + max(1, latency)
        old = self.outstanding.get(cl)
        if old is None or response_tick < old:
            self.outstanding[cl] = response_tick
        heapq.heappush(self.pending_fills, (response_tick, cid, cl, to_l1, is_prefetch))

    def _prefetch(self, req: Access) -> None:
        if self.prefetch_degree <= 0:
            return
        if self.prefetch_coverage <= 0.0:
            return
        if self.prefetch_coverage < 1.0:
            # Deterministic sampling keeps calibration reproducible while
            # allowing a continuous prefetch-effectiveness knob.
            h = ((req.cl >> 6) * 1103515245 + req.order * 12345 + req.core_id * 97) & 0xFFFFFFFF
            if (h % 10000) >= int(self.prefetch_coverage * 10000):
                return
        for i in range(1, self.prefetch_degree + 1):
            pcl = req.cl + i * self.line_b
            if self.llc.contains(pcl) or pcl in self.outstanding:
                continue
            self._schedule_fill(
                req.tick, req.core_id, pcl, self.prefetch_latency,
                to_l1=False, is_prefetch=True,
            )

    def _visible_llc_hit(self, req: Access) -> bool:
        if not self.llc.contains(req.cl):
            return False
        if req.is_store and (not self.prefetch_visible_to_stores) and req.cl in self.prefetched_only:
            return False
        return True

    def _promote_demand(self, cl: int) -> None:
        self.demand_seen.add(cl)
        self.prefetched_only.discard(cl)

    def step(self, req: Access) -> None:
        self._drain(req.tick)
        cid = req.core_id
        cl = req.cl
        _, other_owns, dirty_owner, sharer_others = self.dir.probe(cid, cl, req.is_store)
        inval_targets = self.dir.invalidation_targets(cid, cl, req.is_store)
        if req.is_store:
            self.diag["stores"] += 1
            if other_owns:
                self.diag["store_probe_other_owner"] += 1
            if dirty_owner:
                self.diag["store_probe_dirty_owner"] += 1
            if sharer_others > 0:
                self.diag["store_probe_other_sharers"] += 1
            self.diag["store_inval_targets_total"] += len(inval_targets)
            if inval_targets:
                self.diag["store_inval_events_nonempty"] += 1
        recent_other = False
        if req.is_store and self.store_sharing_ttl_cycles > 0:
            touches = self.last_core_touch.get(cl, {})
            recent_other = any(
                ocid != cid and (req.tick - otick) <= self.store_sharing_ttl_cycles
                for ocid, otick in touches.items()
            )

        l1_hit = self.l1d[cid].contains(cl)
        l2_hit = self.l2[cid].contains(cl)
        llc_hit = self._visible_llc_hit(req)
        coalesced = cl in self.outstanding

        if self.coherence_actions_affect_coh and req.is_store and (other_owns or sharer_others > 0 or recent_other):
            coh = COH_WB
            self._schedule_fill(req.tick, cid, cl, self.lat[COH_WB], to_l1=True)
        elif coalesced:
            # A same-line outstanding miss means this request should not create
            # a new LLC miss/TOR.  Encode it as LLC_HIT-like for PMU purposes.
            coh = COH_LLC
        elif l1_hit:
            coh = COH_L1
            self._promote_demand(cl)
            self.l1d[cid].touch(cl)
        elif l2_hit:
            coh = COH_L2
            self._promote_demand(cl)
            self.l2[cid].touch(cl)
            self.l1d[cid].touch(cl)
        elif llc_hit:
            coh = COH_LLC
            self._promote_demand(cl)
            self.llc.touch(cl)
            self.l2[cid].touch(cl)
            self.l1d[cid].touch(cl)
        elif (not req.is_store) and other_owns and self.remote_read_fold_to_llc:
            coh = COH_LLC
            self._schedule_fill(req.tick, cid, cl, self.lat[COH_LLC], to_l1=True)
        else:
            coh = COH_DRAM
            self._schedule_fill(req.tick, cid, cl, self.lat[COH_DRAM], to_l1=True)

        if self.invalidate_private_on_store:
            for ocid in inval_targets:
                if 0 <= ocid < len(self.l1d):
                    if self.l1d[ocid].contains(cl):
                        self.diag["private_inval_l1_lines"] += 1
                    if self.l2[ocid].contains(cl):
                        self.diag["private_inval_l2_lines"] += 1
                    self.l1d[ocid].invalidate(cl)
                    self.l2[ocid].invalidate(cl)

        self.dir.update(cid, cl, req.is_store)
        self.last_core_touch.setdefault(cl, {})[cid] = req.tick
        if not req.is_store:
            self._prefetch(req)
        self._accumulate(req, coh)

    def finish(self) -> None:
        self._drain(1 << 62)
        snp = int(round(self.dir.snp * self.snp_coverage))
        self.pred_pmu["cha.dir_lookup.snp"] = snp
        self.pred_pmu["cha.core_snp.any_one"] = snp

    def _accumulate(self, req: Access, coh: int) -> None:
        if req.is_store:
            self.store_hist[coh] += 1
            self.pred_pmu["l1d.stores"] += 1
            self.pred_pmu["cha.requests.writes"] += 1
            if coh in L1_MISS_SET:
                self.pred_pmu["l1d.store_misses"] += 1
            if coh in L2_MISS_SET:
                self.pred_pmu["l2.misses"] += 1
            if coh == COH_DRAM:
                self.pred_pmu["llc.store_misses"] += 1
            return

        self.load_hist[coh] += 1
        self.pred_pmu["l1d.loads"] += 1
        self.pred_pmu["cha.requests.reads"] += 1
        if coh in L1_MISS_SET:
            self.pred_pmu["l1d.load_misses"] += 1
        if coh in L2_MISS_SET:
            self.pred_pmu["l2.misses"] += 1
        if coh == COH_DRAM:
            self.pred_pmu["llc.load_misses"] += 1
            self.pred_pmu["cha.tor_inserts.ia_miss_drd"] += 1


def make_pmu() -> dict[str, int]:
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


def iter_jsonl(path: Path) -> Iterable[dict]:
    with open(path) as f:
        for line in f:
            s = line.strip()
            if s.startswith("{"):
                yield json.loads(s)


def cacheline_bits(profile: dict) -> int:
    line_b = int(profile.get("cache", {}).get("l1d", {}).get("line_b", 64))
    return int(math.log2(line_b))


def load_oracle(mem_events: Path, bits: int) -> tuple[Counter, Counter, dict[str, int]]:
    load_hist, store_hist = Counter(), Counter()
    pmu = make_pmu()
    directory = Directory()
    for row in iter_jsonl(mem_events):
        et = row.get("event_type", "commit")
        if et == "commit":
            cid = int(row.get("core_id", 0))
            cl = int(row.get("cacheline_addr", 0)) >> bits
            is_store = int(row.get("is_store", 0)) == 1
            if is_store:
                pmu["l1d.stores"] += 1
            else:
                pmu["l1d.loads"] += 1
            directory.update(cid, cl << bits, is_store)
            continue
        if et != "request":
            continue
        coh = int(row.get("coh_oracle", 0))
        is_store = int(row.get("is_store", 0)) == 1
        if is_store:
            store_hist[coh] += 1
            pmu["cha.requests.writes"] += 1
            if coh in L1_MISS_SET:
                pmu["l1d.store_misses"] += 1
            if coh in L2_MISS_SET:
                pmu["l2.misses"] += 1
            if coh == COH_DRAM:
                pmu["llc.store_misses"] += 1
        else:
            load_hist[coh] += 1
            pmu["cha.requests.reads"] += 1
            if coh in L1_MISS_SET:
                pmu["l1d.load_misses"] += 1
            if coh in L2_MISS_SET:
                pmu["l2.misses"] += 1
            if coh == COH_DRAM:
                pmu["llc.load_misses"] += 1
                pmu["cha.tor_inserts.ia_miss_drd"] += 1
    pmu["cha.dir_lookup.snp"] = directory.snp
    pmu["cha.core_snp.any_one"] = directory.snp
    return load_hist, store_hist, pmu


def load_accesses(dataset_dir: Path, tick_source: str, row_tick_stride: int) -> list[Access]:
    ffiles = sorted(glob.glob(str(dataset_dir / "functional_parquet" / "functional.core*.parquet")))
    if not ffiles:
        raise SystemExit(f"missing functional parquet under {dataset_dir}")
    accesses: list[Access] = []
    order = 0
    per_core_order: dict[int, int] = {}
    for ffp in ffiles:
        core_id = int(Path(ffp).stem.split("core")[-1])
        ft = pq.read_table(ffp, columns=[
            "core_id", "thread_id", "micro_seq", "seq_num", "paddr",
            "is_load", "is_store", "is_atomic", "size",
        ])
        labels = None
        if tick_source in {"commit_tick", "issue_tick", "complete_tick"}:
            lfp = dataset_dir / "labels_parquet" / f"labels.core{core_id}.parquet"
            labels = pq.read_table(str(lfp), columns=[tick_source])
        cols = {name: ft.column(name).to_pylist() for name in ft.schema.names}
        ticks = labels.column(tick_source).to_pylist() if labels is not None else None
        for i in range(ft.num_rows):
            is_store = bool(cols["is_store"][i] or cols["is_atomic"][i])
            is_load = bool(cols["is_load"][i])
            if not (is_store or is_load):
                continue
            local_order = per_core_order.get(core_id, 0)
            per_core_order[core_id] = local_order + 1
            if tick_source == "row":
                tick = order * row_tick_stride
                sort_order = order
            elif tick_source == "per_core_row":
                tick = local_order * row_tick_stride
                sort_order = local_order
            elif tick_source == "micro_seq":
                tick = int(cols["micro_seq"][i])
                sort_order = order
            else:
                tick = int(ticks[i])
                sort_order = order
            accesses.append(Access(
                tick=tick,
                order=sort_order,
                core_id=int(cols["core_id"][i]),
                thread_id=int(cols["thread_id"][i]),
                seq=int(cols["micro_seq"][i]),
                cl=int(cols["paddr"][i]) & ~63,
                is_store=is_store,
                size=int(cols["size"][i]),
            ))
            order += 1
    accesses.sort(key=lambda a: (a.tick, a.order, a.core_id))
    return accesses


def raw_snp_from_accesses(accesses: list[Access]) -> int:
    directory = Directory()
    for req in accesses:
        directory.update(req.core_id, req.cl, req.is_store)
    return directory.snp


def sideband_snp_from_accesses(accesses: list[Access], coverage: float = 1.0) -> int:
    return int(round(raw_snp_from_accesses(accesses) * coverage))


def apply_private_sideband_correction(
    sim: TimingFunctionalRefSim,
    row_snp: int,
    per_core_snp: int,
    args: argparse.Namespace,
) -> None:
    scaled_per_core_snp = int(round(per_core_snp * args.snp_coverage))
    pressure = max(0, scaled_per_core_snp - row_snp)
    if pressure <= 0:
        return

    load_extra = int(round(pressure * args.private_sideband_load_coverage))
    store_extra = int(round(pressure * args.private_sideband_store_coverage))
    load_extra = max(0, min(load_extra, sim.load_hist.get(COH_L1, 0)))
    store_extra = max(0, min(store_extra, sim.store_hist.get(COH_L1, 0)))
    if load_extra:
        sim.load_hist[COH_L1] -= load_extra
        sim.load_hist[COH_R_DIRTY] += load_extra
        sim.pred_pmu["l1d.load_misses"] += load_extra
        sim.pred_pmu["l2.misses"] += load_extra
    if store_extra:
        sim.store_hist[COH_L1] -= store_extra
        sim.store_hist[COH_WB] += store_extra
        sim.pred_pmu["l1d.store_misses"] += store_extra
        sim.pred_pmu["l2.misses"] += store_extra
    sim.diag["private_sideband_row_snp"] = row_snp
    sim.diag["private_sideband_per_core_snp"] = per_core_snp
    sim.diag["private_sideband_scaled_per_core_snp"] = scaled_per_core_snp
    sim.diag["private_sideband_pressure"] = pressure
    sim.diag["private_sideband_load_extra"] = load_extra
    sim.diag["private_sideband_store_extra"] = store_extra


def apply_l1_load_hit_fold(sim: TimingFunctionalRefSim, args: argparse.Namespace) -> None:
    l2_hits = sim.load_hist.get(COH_L2, 0)
    llc_hits = sim.load_hist.get(COH_LLC, 0)
    if l2_hits <= 0 and llc_hits <= 0:
        return

    loads = max(1, sim.pred_pmu.get("l1d.loads", 0))
    load_miss_rate = sim.pred_pmu.get("l1d.load_misses", 0) / loads
    if load_miss_rate < args.l1_load_fold_min_miss_rate:
        return
    if load_miss_rate > args.l1_load_fold_max_miss_rate:
        return
    if l2_hits <= 0:
        return
    llc_l2_ratio = llc_hits / max(1, l2_hits)
    if llc_l2_ratio < args.l1_load_fold_min_llc_l2_ratio:
        return

    l2_extra = int(round(l2_hits * args.l1_load_fold_l2_coverage))
    llc_extra = int(round(llc_hits * args.l1_load_fold_llc_coverage))
    l2_extra = max(0, min(l2_extra, l2_hits))
    llc_extra = max(0, min(llc_extra, llc_hits))
    total = l2_extra + llc_extra
    if total <= 0:
        return

    sim.load_hist[COH_L2] -= l2_extra
    sim.load_hist[COH_LLC] -= llc_extra
    sim.load_hist[COH_L1] += total
    sim.pred_pmu["l1d.load_misses"] -= total
    sim.pred_pmu["l2.misses"] -= llc_extra
    sim.diag["l1_load_fold_l2_extra"] = l2_extra
    sim.diag["l1_load_fold_llc_extra"] = llc_extra
    sim.diag["l1_load_fold_miss_rate_ppm"] = int(round(load_miss_rate * 1_000_000))
    sim.diag["l1_load_fold_llc_l2_ratio_ppm"] = int(round(llc_l2_ratio * 1_000_000))


def run_once(dataset_dir: Path, args: argparse.Namespace) -> dict:
    profile = json.loads((dataset_dir / "uarch_profile.json").read_text())
    accesses = load_accesses(dataset_dir, args.tick_source, args.row_tick_stride)
    if args.max_rows:
        accesses = accesses[: args.max_rows]
    sim = TimingFunctionalRefSim(profile, args)
    for req in accesses:
        sim.step(req)
    sim.finish()
    row_snp_for_sideband = 0
    per_core_snp_for_sideband = 0
    if args.snp_tick_source != "same":
        if args.snp_tick_source == "max_row_per_core":
            row_accesses = load_accesses(dataset_dir, "row", args.row_tick_stride)
            per_core_accesses = load_accesses(dataset_dir, "per_core_row", args.row_tick_stride)
            if args.max_rows:
                row_accesses = row_accesses[: args.max_rows]
                per_core_accesses = per_core_accesses[: args.max_rows]
            row_snp_for_sideband = raw_snp_from_accesses(row_accesses)
            per_core_snp_for_sideband = raw_snp_from_accesses(per_core_accesses)
            snp = max(
                row_snp_for_sideband,
                int(round(per_core_snp_for_sideband * args.snp_coverage)),
            )
        else:
            snp_accesses = load_accesses(dataset_dir, args.snp_tick_source, args.row_tick_stride)
            if args.max_rows:
                snp_accesses = snp_accesses[: args.max_rows]
            snp = sideband_snp_from_accesses(snp_accesses, args.snp_coverage)
        sim.pred_pmu["cha.dir_lookup.snp"] = snp
        sim.pred_pmu["cha.core_snp.any_one"] = snp
    if args.private_sideband_load_coverage > 0.0 or args.private_sideband_store_coverage > 0.0:
        if args.snp_tick_source == "max_row_per_core":
            apply_private_sideband_correction(
                sim, row_snp_for_sideband, per_core_snp_for_sideband, args
            )
        else:
            sim.diag["private_sideband_skipped_no_max_source"] = 1
    if args.l1_load_fold_l2_coverage > 0.0 or args.l1_load_fold_llc_coverage > 0.0:
        apply_l1_load_hit_fold(sim, args)
    oracle_load, oracle_store, oracle_pmu = load_oracle(
        dataset_dir / "all_mem_events.merged.jsonl", cacheline_bits(profile)
    )
    return {
        "n_accesses": len(accesses),
        "load_hist": sim.load_hist,
        "store_hist": sim.store_hist,
        "oracle_load_hist": oracle_load,
        "oracle_store_hist": oracle_store,
        "pmu": sim.pred_pmu,
        "oracle_pmu": oracle_pmu,
        "diag": dict(sim.diag),
        "loss": pmu_loss(sim.pred_pmu, oracle_pmu),
    }


def pmu_loss(pred: dict[str, int], oracle: dict[str, int]) -> float:
    weights = {
        "l1d.load_misses": 2.0,
        "l1d.store_misses": 1.0,
        "l2.misses": 2.0,
        "llc.load_misses": 3.0,
        "llc.store_misses": 3.0,
        "cha.dir_lookup.snp": 1.0,
    }
    total = 0.0
    for key, w in weights.items():
        o = oracle.get(key, 0)
        p = pred.get(key, 0)
        denom = max(1, o)
        total += w * abs(p - o) / denom
    return total


def show_hist(title: str, pred: Counter, oracle: Counter) -> None:
    print(f"\n=== {title} ===")
    print(f"{'coh':<10} {'functional':>12} {'oracle':>12} {'diff':>12}")
    for key in sorted(set(pred) | set(oracle)):
        p, o = pred.get(key, 0), oracle.get(key, 0)
        print(f"{COH_NAMES.get(key, str(key)):<10} {p:>12} {o:>12} {p-o:>+12}")
    print(f"{'TOTAL':<10} {sum(pred.values()):>12} {sum(oracle.values()):>12} "
          f"{sum(pred.values())-sum(oracle.values()):>+12}")


def show_pmu(pred: dict[str, int], oracle: dict[str, int]) -> None:
    print("\n=== PMU ===")
    print(f"{'metric':<32} {'functional':>12} {'oracle':>12} {'err%':>10}")
    for key in sorted(oracle):
        p, o = pred.get(key, 0), oracle.get(key, 0)
        if o:
            err = f"{(p-o)/o*100.0:+.2f}"
        else:
            err = "0.00" if p == 0 else "inf"
        print(f"{key:<32} {p:>12} {o:>12} {err:>10}")


def show_diag(diag: dict[str, int]) -> None:
    if not diag:
        return
    print("\n=== DIAG ===")
    for key in sorted(diag):
        print(f"{key:<32} {diag[key]:>12}")


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset-dir", required=True)
    ap.add_argument("--tick-source", choices=["row", "per_core_row", "micro_seq", "commit_tick", "issue_tick", "complete_tick"],
                    default="row")
    ap.add_argument("--snp-tick-source", choices=["same", "row", "per_core_row", "max_row_per_core", "micro_seq", "commit_tick", "issue_tick", "complete_tick"],
                    default="same")
    ap.add_argument("--row-tick-stride", type=int, default=4)
    ap.add_argument("--max-rows", type=int, default=0)
    ap.add_argument("--l1-hit-cycles", type=int, default=4)
    ap.add_argument("--l2-hit-cycles", type=int, default=12)
    ap.add_argument("--llc-hit-cycles", type=int, default=36)
    ap.add_argument("--dram-cycles", type=int, default=180)
    ap.add_argument("--store-wb-cycles", type=int, default=80)
    ap.add_argument("--prefetch-latency-cycles", type=int, default=40)
    ap.add_argument("--mshr-entries", type=int, default=16)
    ap.add_argument("--l1d-capacity-factor", type=float, default=1.0)
    ap.add_argument("--llc-capacity-factor", type=float, default=1.0)
    ap.add_argument("--prefetch-degree", type=int, default=0)
    ap.add_argument("--prefetch-coverage", type=float, default=1.0)
    ap.add_argument("--prefetch-visible-to-stores", action=argparse.BooleanOptionalAction, default=False)
    ap.add_argument("--store-sharing-ttl-cycles", type=int, default=0)
    ap.add_argument("--coherence-actions-affect-coh", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--invalidate-private-on-store", action=argparse.BooleanOptionalAction, default=False)
    ap.add_argument("--snp-coverage", type=float, default=1.0)
    ap.add_argument("--private-sideband-load-coverage", type=float, default=0.0)
    ap.add_argument("--private-sideband-store-coverage", type=float, default=0.0)
    ap.add_argument("--l1-load-fold-l2-coverage", type=float, default=0.0)
    ap.add_argument("--l1-load-fold-llc-coverage", type=float, default=0.0)
    ap.add_argument("--l1-load-fold-min-miss-rate", type=float, default=0.01)
    ap.add_argument("--l1-load-fold-max-miss-rate", type=float, default=0.05)
    ap.add_argument("--l1-load-fold-min-llc-l2-ratio", type=float, default=2.0)
    ap.add_argument("--remote-read-fold-to-llc", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--summary-out")
    return ap


def main() -> None:
    args = build_argparser().parse_args()
    dataset_dir = Path(args.dataset_dir).resolve()
    result = run_once(dataset_dir, args)
    print(f"dataset={dataset_dir}")
    print(f"tick_source={args.tick_source} n_accesses={result['n_accesses']} loss={result['loss']:.6f}")
    show_hist("LOAD coh hist", result["load_hist"], result["oracle_load_hist"])
    show_hist("STORE coh hist", result["store_hist"], result["oracle_store_hist"])
    show_pmu(result["pmu"], result["oracle_pmu"])
    show_diag(result["diag"])
    if args.summary_out:
        out = {
            "dataset_dir": str(dataset_dir),
            "tick_source": args.tick_source,
            "n_accesses": result["n_accesses"],
            "loss": result["loss"],
            "functional_pmu": result["pmu"],
            "oracle_pmu": result["oracle_pmu"],
            "functional_load_hist": dict(result["load_hist"]),
            "functional_store_hist": dict(result["store_hist"]),
            "oracle_load_hist": dict(result["oracle_load_hist"]),
            "oracle_store_hist": dict(result["oracle_store_hist"]),
            "diag": result["diag"],
            "params": {
                "row_tick_stride": args.row_tick_stride,
                "snp_tick_source": args.snp_tick_source,
                "l1_hit_cycles": args.l1_hit_cycles,
                "l2_hit_cycles": args.l2_hit_cycles,
                "llc_hit_cycles": args.llc_hit_cycles,
                "dram_cycles": args.dram_cycles,
                "store_wb_cycles": args.store_wb_cycles,
                "mshr_entries": args.mshr_entries,
                "l1d_capacity_factor": args.l1d_capacity_factor,
                "llc_capacity_factor": args.llc_capacity_factor,
                "prefetch_degree": args.prefetch_degree,
                "prefetch_coverage": args.prefetch_coverage,
                "prefetch_visible_to_stores": args.prefetch_visible_to_stores,
                "store_sharing_ttl_cycles": args.store_sharing_ttl_cycles,
                "coherence_actions_affect_coh": args.coherence_actions_affect_coh,
                "invalidate_private_on_store": args.invalidate_private_on_store,
                "snp_coverage": args.snp_coverage,
                "private_sideband_load_coverage": args.private_sideband_load_coverage,
                "private_sideband_store_coverage": args.private_sideband_store_coverage,
                "l1_load_fold_l2_coverage": args.l1_load_fold_l2_coverage,
                "l1_load_fold_llc_coverage": args.l1_load_fold_llc_coverage,
                "l1_load_fold_min_miss_rate": args.l1_load_fold_min_miss_rate,
                "l1_load_fold_max_miss_rate": args.l1_load_fold_max_miss_rate,
                "l1_load_fold_min_llc_l2_ratio": args.l1_load_fold_min_llc_l2_ratio,
                "remote_read_fold_to_llc": args.remote_read_fold_to_llc,
            },
        }
        Path(args.summary_out).write_text(json.dumps(out, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
