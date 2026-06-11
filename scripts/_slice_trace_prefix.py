#!/usr/bin/env python3
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
from pathlib import Path


def core_id_of(path: str) -> int:
    name = os.path.basename(path)
    m = re.search(r"cores(\d+)", name)
    if not m:
        raise ValueError(f"cannot infer core id from {path}")
    return int(m.group(1))


def iter_jsonl(path: str):
    with open(path) as f:
        for line in f:
            s = line.strip()
            if not s.startswith("{"):
                continue
            yield json.loads(s)


def write_jsonl(path: str, rows) -> int:
    n = 0
    with open(path, "w") as f:
        for row in rows:
            f.write(json.dumps(row, separators=(",", ":")))
            f.write("\n")
            n += 1
    return n


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Slice a tao_trace directory to the first N records per core."
    )
    ap.add_argument("--in-run-dir", required=True, help="gem5 run dir containing tao_trace/")
    ap.add_argument("--out-run-dir", required=True, help="output run dir")
    ap.add_argument("--max-records-per-core", type=int, default=20000)
    ap.add_argument(
        "--copy-files",
        nargs="*",
        default=["uarch_profile.json", "stats.txt", "config.ini", "config.json", "citations.bib"],
        help="top-level files copied from in-run-dir to out-run-dir",
    )
    args = ap.parse_args()

    in_run = Path(args.in_run_dir).resolve()
    out_run = Path(args.out_run_dir).resolve()
    in_trace = in_run / "tao_trace"
    out_trace = out_run / "tao_trace"
    out_trace.mkdir(parents=True, exist_ok=True)

    records_files = sorted(glob.glob(str(in_trace / "*.records.micro.jsonl")), key=core_id_of)
    if not records_files:
        raise SystemExit(f"no records files under {in_trace}")

    cutoffs = {}
    summary = {"max_records_per_core": int(args.max_records_per_core), "cores": {}}

    for rec_path in records_files:
        cid = core_id_of(rec_path)
        lbl_path = rec_path.replace(".records.micro.jsonl", ".labels.micro.jsonl")
        out_rec = out_trace / os.path.basename(rec_path)
        out_lbl = out_trace / os.path.basename(lbl_path)

        kept_records = []
        kept_labels = []
        last_commit_tick = None
        for idx, (jr, jl) in enumerate(zip(iter_jsonl(rec_path), iter_jsonl(lbl_path)), start=1):
            if idx > args.max_records_per_core:
                break
            kept_records.append(jr)
            kept_labels.append(jl)
            last_commit_tick = int(jl.get("commit_tick", 0))

        if not kept_records:
            raise SystemExit(f"core {cid} has no records in {rec_path}")
        if len(kept_records) != len(kept_labels):
            raise SystemExit(f"core {cid} records/labels length mismatch")

        write_jsonl(str(out_rec), kept_records)
        write_jsonl(str(out_lbl), kept_labels)
        cutoffs[cid] = int(last_commit_tick)
        summary["cores"][str(cid)] = {
            "records_rows": len(kept_records),
            "labels_rows": len(kept_labels),
            "commit_tick_cutoff": int(last_commit_tick),
        }

    mem_event_counts = {}
    for mem_path in sorted(glob.glob(str(in_trace / "*.mem_events.jsonl")), key=core_id_of):
        cid = core_id_of(mem_path)
        cutoff = cutoffs[cid]
        out_mem = out_trace / os.path.basename(mem_path)
        kept = []
        for row in iter_jsonl(mem_path):
            if int(row.get("commit_tick", 0)) <= cutoff:
                kept.append(row)
        mem_event_counts[cid] = write_jsonl(str(out_mem), kept)
        summary["cores"][str(cid)]["mem_events_rows"] = mem_event_counts[cid]

    for name in args.copy_files:
        src = in_run / name
        if src.exists():
            shutil.copy2(src, out_run / name)

    merged = []
    for mem_path in sorted(glob.glob(str(out_trace / "*.mem_events.jsonl")), key=core_id_of):
        merged.extend(iter_jsonl(mem_path))
    merged.sort(key=lambda r: (int(r.get("commit_tick", 0)), int(r.get("seq", 0))))
    summary["merged_mem_events_rows"] = write_jsonl(str(out_run / "all_mem_events.merged.jsonl"), merged)

    with open(out_run / "slice_summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
