#!/usr/bin/env python3
"""Diagnose driver d-side coh distribution vs oracle request coh distribution.

The driver consumes a functional_parquet (retire stream). For every load/store
row we call ref_sim_py.LocalRefSim.on_mem_access_speculative once and bucket
the returned coh_oracle. Output:

    LOAD  coh hist (driver vs oracle)
    STORE coh hist (driver vs oracle)

Cross-core ordering is reproduced by interleaving all cores' rows by global
micro_seq, then per-core by micro_seq, matching the driver's quantum-internal
order in W11_stream_mix_4c_u100000.

Usage:
    _driver_dside_coh_hist.py \
        --dataset-dir <data/W11_stream_mix_4c_u100000> \
        --ref-sim-module-dir <build dir>
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import Counter
from pathlib import Path

import pyarrow.parquet as pq


COH_NAMES = {
    0: "UNKNOWN",
    1: "L1_HIT",
    2: "R_CLEAN",
    3: "R_DIRTY",
    4: "LLC_HIT",
    5: "DRAM",
    6: "WB",
    7: "L2_HIT",
}


def load_oracle_request_hist(mem_events_path: Path):
    load_h, store_h = Counter(), Counter()
    with open(mem_events_path) as f:
        for ln in f:
            if not ln.startswith("{"):
                continue
            ev = json.loads(ln)
            if ev.get("event_type") != "request":
                continue
            coh = int(ev.get("coh_oracle", 0))
            is_store = int(ev.get("is_store", 0)) == 1
            if is_store:
                store_h[coh] += 1
            else:
                load_h[coh] += 1
    return load_h, store_h


def load_functional_rows(functional_dir: Path):
    files = sorted(glob.glob(str(functional_dir / "functional.core*.parquet")))
    if not files:
        raise SystemExit(f"no functional.core*.parquet under {functional_dir}")
    rows = []
    for fp in files:
        t = pq.read_table(fp, columns=[
            "core_id", "thread_id", "micro_seq", "paddr",
            "is_load", "is_store", "is_atomic", "size",
        ])
        cid = t.column("core_id").to_pylist()
        tid = t.column("thread_id").to_pylist()
        ms = t.column("micro_seq").to_pylist()
        pa = t.column("paddr").to_pylist()
        il = t.column("is_load").to_pylist()
        is_ = t.column("is_store").to_pylist()
        ia = t.column("is_atomic").to_pylist()
        sz = t.column("size").to_pylist()
        for i in range(len(cid)):
            if il[i] or is_[i] or ia[i]:
                rows.append((int(ms[i]), int(cid[i]), int(tid[i]),
                             int(pa[i]), bool(is_[i] or ia[i]),
                             bool(il[i]), int(sz[i])))
    rows.sort(key=lambda r: (r[0], r[1]))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset-dir", required=True)
    ap.add_argument("--ref-sim-module-dir", required=True)
    ap.add_argument("--max-rows", type=int, default=0,
                    help="optional cap on rows for debugging (0=all)")
    args = ap.parse_args()

    dataset_dir = Path(args.dataset_dir).resolve()
    functional_dir = dataset_dir / "functional_parquet"
    mem_events = dataset_dir / "all_mem_events.merged.jsonl"
    profile = dataset_dir / "uarch_profile.json"
    sys.path.insert(0, str(Path(args.ref_sim_module_dir).resolve()))
    import ref_sim_py  # noqa

    coord = ref_sim_py.Coordinator(str(profile))
    locals_: dict = {}

    def _local(cid: int):
        if cid not in locals_:
            locals_[cid] = coord.local(cid)
        return locals_[cid]

    rows = load_functional_rows(functional_dir)
    if args.max_rows:
        rows = rows[: args.max_rows]
    print(f"functional d-side rows: {len(rows)}")

    drv_load_h, drv_store_h = Counter(), Counter()
    for ms, cid, tid, paddr, is_store, is_load, size in rows:
        d = _local(cid).on_mem_access_speculative(
            int(paddr), bool(is_store), int(size), int(ms), int(tid))
        coh = int(d["coh_oracle"])
        if is_store:
            drv_store_h[coh] += 1
        else:
            drv_load_h[coh] += 1

    or_load_h, or_store_h = load_oracle_request_hist(mem_events)

    def _show(title, drv, orc):
        print(f"\n=== {title} ===")
        keys = sorted(set(drv) | set(orc))
        print(f"  {'coh':<10} {'driver':>10} {'oracle':>10} {'diff':>10}")
        tot_d = sum(drv.values())
        tot_o = sum(orc.values())
        for k in keys:
            print(f"  {COH_NAMES.get(k,str(k)):<10} "
                  f"{drv.get(k,0):>10} {orc.get(k,0):>10} "
                  f"{drv.get(k,0)-orc.get(k,0):>+10}")
        print(f"  {'TOTAL':<10} {tot_d:>10} {tot_o:>10} {tot_d-tot_o:>+10}")

    _show("LOAD coh hist",  drv_load_h,  or_load_h)
    _show("STORE coh hist", drv_store_h, or_store_h)


if __name__ == "__main__":
    main()
