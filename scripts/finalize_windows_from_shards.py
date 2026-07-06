#!/usr/bin/env python3
"""Finalize an existing data/build_windows.py .shards directory.

This reuses already-built workload shards, then applies the same optional
dedup and per-workload cap logic as data/build_windows.py before writing
<out>/windows.jsonl.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import importlib.util
import json
import os
import random
import sys
from pathlib import Path
from typing import Optional


def _load_build_windows(repo: Path):
    path = repo / "data" / "build_windows.py"
    spec = importlib.util.spec_from_file_location("_llmsim_build_windows", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="Directory containing .shards")
    ap.add_argument("--repo", default="/data00/yinhaolang/LLMSim")
    ap.add_argument("--workloads", nargs="*", default=None)
    ap.add_argument("--per-workload-cap", default="")
    ap.add_argument("--per-workload-cap-seed", type=int, default=0)
    ap.add_argument("--dedup-threshold", type=float, default=0.0)
    ap.add_argument("--dedup-jobs", type=int, default=1)
    ap.add_argument("--dedup-report", default="")
    args = ap.parse_args()

    repo = Path(args.repo)
    mod = _load_build_windows(repo)
    out_dir = Path(args.out)
    shard_dir = out_dir / ".shards"
    if not shard_dir.is_dir():
        raise SystemExit(f"[error] missing shard dir: {shard_dir}")

    if args.workloads:
        workloads = list(args.workloads)
    else:
        workloads = sorted(p.stem for p in shard_dir.glob("*.jsonl"))
    if not workloads:
        raise SystemExit("[error] no workload shards found")

    missing = [wd for wd in workloads if not (shard_dir / f"{wd}.jsonl").is_file()]
    if missing:
        raise SystemExit(f"[error] missing shards: {' '.join(missing)}")

    dedup_keep: dict[str, Optional[set[int]]] = {}
    dedup_stats: dict[str, dict] = {}
    dedup_thr = float(args.dedup_threshold or 0.0)
    if dedup_thr > 0.0:
        jobs = max(1, int(args.dedup_jobs or 1))
        print(f"[dedup] threshold={dedup_thr} jobs={jobs} workloads={len(workloads)}")
        with cf.ProcessPoolExecutor(max_workers=jobs) as ex:
            futs = {
                ex.submit(mod._dedup_shard, str(shard_dir / f"{wd}.jsonl"), dedup_thr): wd
                for wd in workloads
            }
            for fut in cf.as_completed(futs):
                wd = futs[fut]
                keep_idx, stats = fut.result()
                dedup_keep[wd] = set(keep_idx)
                dedup_stats[wd] = stats
                frac = stats["n_dropped"] / stats["n_total"] if stats["n_total"] else 0.0
                print(
                    f"[dedup] {wd}: {stats['n_total']} -> {stats['n_kept']} "
                    f"(drop={stats['n_dropped']}, {frac:.1%}) thr={dedup_thr}"
                )

    cap_default, cap_by_name = mod._parse_per_workload_cap(args.per_workload_cap)
    cap_rng = random.Random(int(args.per_workload_cap_seed))
    out_path = out_dir / "windows.jsonl"
    tmp_path = out_dir / "windows.jsonl.tmp"
    total = 0
    with tmp_path.open("w") as fout:
        for wd in workloads:
            shard_path = shard_dir / f"{wd}.jsonl"
            with shard_path.open() as fin:
                lines = fin.readlines()
            nsamp = len(lines)
            keep_set = dedup_keep.get(wd) if dedup_thr > 0.0 else None
            effective_n = len(keep_set) if keep_set is not None else nsamp
            cap = cap_by_name.get(wd, cap_default)
            if cap is None or effective_n <= cap:
                indices = range(nsamp) if keep_set is None else sorted(keep_set)
            else:
                pool = range(nsamp) if keep_set is None else sorted(keep_set)
                indices = sorted(cap_rng.sample(list(pool), int(cap)))
                print(
                    f"[cap] workload={wd} nsamp={nsamp} "
                    f"dedup_kept={effective_n} -> kept={len(indices)} (cap={cap})"
                )
            for idx in indices:
                fout.write(lines[idx])
            total += len(indices)
    os.replace(tmp_path, out_path)

    if dedup_thr > 0.0:
        report_path = Path(args.dedup_report) if args.dedup_report else out_dir / "dedup_report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with report_path.open("w") as f:
            json.dump({
                "threshold": dedup_thr,
                "total_pre_dedup": sum(s["n_total"] for s in dedup_stats.values()),
                "total_post_dedup": sum(s["n_kept"] for s in dedup_stats.values()),
                "per_workload": dedup_stats,
            }, f, indent=2)
        print(f"[dedup] report -> {report_path}")

    print(f"[done] workloads={len(workloads)} total samples={total} -> {out_path}")


if __name__ == "__main__":
    main()
