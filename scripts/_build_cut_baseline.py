#!/usr/bin/env python3
from __future__ import annotations

import argparse
import glob
import json
import os
import re
from pathlib import Path


def core_id_of(path: str) -> int:
    name = os.path.basename(path)
    m = re.search(r"cores?(\d+)", name)
    if not m:
        raise ValueError(f"cannot infer core id from {path}")
    return int(m.group(1))


def iter_jsonl(path: Path):
    with open(path) as f:
        for line in f:
            s = line.strip()
            if not s.startswith("{"):
                continue
            yield json.loads(s)


def infer_workload_name(dataset_dir: Path) -> str:
    name = dataset_dir.name
    m = re.match(r"(W\d+_[A-Za-z0-9_]+)_\dc_u\d+$", name)
    if m:
        return m.group(1)
    return name


def parse_stats(stats_path: Path) -> dict[int, dict[str, float]]:
    if not stats_path.is_file():
        return {}

    stats: dict[int, dict[str, float]] = {}
    patterns = [
        (re.compile(r"board\.processor\.cores(\d+)\.core\.numCycles\s+([0-9.eE+-]+)"), "num_cycles"),
        (re.compile(r"board\.processor\.cores(\d+)\.core\.cpi\s+([0-9.eE+-]+)"), "core_cpi"),
        (re.compile(r"board\.processor\.cores(\d+)\.core\.ipc\s+([0-9.eE+-]+)"), "core_ipc"),
        (
            re.compile(
                r"board\.processor\.cores(\d+)\.core\.branchPred\.committed_\d+::total\s+([0-9.eE+-]+)"
            ),
            "branch_committed",
        ),
        (
            re.compile(
                r"board\.processor\.cores(\d+)\.core\.branchPred\.mispredicted_\d+::total\s+([0-9.eE+-]+)"
            ),
            "branch_mispred",
        ),
    ]

    with open(stats_path) as f:
        for line in f:
            for pat, key in patterns:
                m = pat.search(line)
                if not m:
                    continue
                cid = int(m.group(1))
                stats.setdefault(cid, {})[key] = float(m.group(2))
                break
    return stats


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Build a cut-window baseline JSON from sliced tao_trace and full-run stats."
    )
    ap.add_argument("--dataset-dir", required=True)
    ap.add_argument("--out")
    ap.add_argument("--ticks-per-cycle", type=float, default=333.0)
    return ap


def main() -> None:
    args = build_argparser().parse_args()
    dataset_dir = Path(args.dataset_dir).resolve()
    trace_dir = dataset_dir / "tao_trace"
    if not trace_dir.is_dir():
        raise SystemExit(f"missing tao_trace under {dataset_dir}")

    records_files = sorted(glob.glob(str(trace_dir / "*.records.micro.jsonl")), key=core_id_of)
    labels_files = sorted(glob.glob(str(trace_dir / "*.labels.micro.jsonl")), key=core_id_of)
    if not records_files or len(records_files) != len(labels_files):
        raise SystemExit(f"records/labels files mismatch under {trace_dir}")

    full_run_stats = parse_stats(dataset_dir / "stats.txt")
    workload = infer_workload_name(dataset_dir)

    per_core: dict[str, dict] = {}
    agg_records_rows = 0
    agg_labels_rows = 0
    agg_macro = 0
    agg_cycles = 0.0
    agg_branch = 0
    agg_mispred = 0

    for rec_path, lbl_path in zip(records_files, labels_files):
        cid = core_id_of(rec_path)
        records_rows = 0
        labels_rows = 0
        macro_count = 0
        branch_committed = 0
        branch_mispred = 0
        first_fetch_tick = None
        last_commit_tick = None

        with open(rec_path) as frec, open(lbl_path) as flbl:
            for rec_line, lbl_line in zip(frec, flbl):
                jr = json.loads(rec_line)
                jl = json.loads(lbl_line)
                records_rows += 1
                labels_rows += 1

                fetch_tick = int(jl["fetch_tick"])
                commit_tick = int(jl["commit_tick"])
                if first_fetch_tick is None:
                    first_fetch_tick = fetch_tick
                last_commit_tick = commit_tick

                is_macro = int(jr.get("is_last_microop", 0)) > 0 or int(jr.get("is_microop", 0)) == 0
                if is_macro:
                    macro_count += 1
                if int(jr.get("is_branch", 0)) > 0 and is_macro:
                    branch_committed += 1
                    branch_mispred += int(jl.get("mispredicted", 0))

        if first_fetch_tick is None or last_commit_tick is None:
            raise SystemExit(f"no rows found for core {cid} under {trace_dir}")

        approx_cycles = (last_commit_tick - first_fetch_tick) / float(args.ticks_per_cycle)
        approx_cpi = (approx_cycles / macro_count) if macro_count else None

        rec = {
            "records_rows": records_rows,
            "labels_rows": labels_rows,
            "macro_count": macro_count,
            "first_fetch_tick": first_fetch_tick,
            "last_commit_tick": last_commit_tick,
            "approx_cycles_from_labels": approx_cycles,
            "approx_cpi_macro_from_labels": approx_cpi,
            "branch_committed": branch_committed,
            "branch_mispred": branch_mispred,
            "branch_miss_rate": (branch_mispred / branch_committed) if branch_committed else None,
        }
        if cid in full_run_stats:
            rec["full_run_stats_reference"] = full_run_stats[cid]
        per_core[str(cid)] = rec

        agg_records_rows += records_rows
        agg_labels_rows += labels_rows
        agg_macro += macro_count
        agg_cycles += approx_cycles
        agg_branch += branch_committed
        agg_mispred += branch_mispred

    baseline = {
        "baseline_type": "cut-window-derived",
        "dataset_dir": str(dataset_dir),
        "workload": workload,
        "ticks_per_cycle_assumption": float(args.ticks_per_cycle),
        "notes": [
            "该文件对应裁切窗口的近似 baseline，而不是 gem5 原生 cut-window stats。",
            "CPI 基于 labels 的 first_fetch_tick / last_commit_tick 换算得到。",
            "branch_mispred 来自裁切窗口内已提交 branch 的 labels.mispredicted。",
            "full_run_stats_reference 仅作为原始 full-run 参考，不能与 cut-window 结果直接混用。",
        ],
        "aggregate": {
            "records_rows": agg_records_rows,
            "labels_rows": agg_labels_rows,
            "macro_count": agg_macro,
            "approx_cycles_from_labels_sum": agg_cycles,
            "approx_cpi_macro_from_labels": (agg_cycles / agg_macro) if agg_macro else None,
            "branch_committed": agg_branch,
            "branch_mispred": agg_mispred,
            "branch_miss_rate": (agg_mispred / agg_branch) if agg_branch else None,
        },
        "per_core": per_core,
    }

    out_path = Path(args.out).resolve() if args.out else (dataset_dir / "cut_baseline.json")
    out_path.write_text(json.dumps(baseline, indent=2, sort_keys=True))
    print(json.dumps(baseline, indent=2, sort_keys=True))
    print(f"cut_baseline -> {out_path}")


if __name__ == "__main__":
    main()
