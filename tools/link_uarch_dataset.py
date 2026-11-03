#!/usr/bin/env python3
"""Link reusable functional traces to gem5 uarch labels and audit stream counts."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Any


def load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as source:
        return json.load(source)


def trace_id(metadata: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    for core, value in sorted(
        metadata["fst_sha256"].items(), key=lambda item: int(item[0])
    ):
        digest.update(str(core).encode())
        digest.update(b"\0")
        digest.update(str(value).encode())
        digest.update(b"\0")
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument(
        "--max-boundary-uops-per-core",
        type=int,
        default=32,
        help="allowed ROI marker/switch boundary delta per core (default: 32)",
    )
    parser.add_argument("--allow-stream-mismatch", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for metrics_path in sorted(root.glob("labels/*/c*/W_*/metrics.json")):
        metrics = load(metrics_path)
        cores = int(metrics["cores"])
        seed = int(metrics["seed"])
        workload = str(metrics["workload"])
        trace_dir = root / "traces" / f"seed{seed}" / f"c{cores:02d}" / f"W_{workload}"
        trace_meta_path = trace_dir / "trace.json"
        if not trace_meta_path.is_file():
            errors.append(f"missing trace for {metrics_path}")
            continue
        trace_meta = load(trace_meta_path)
        trace_records = sum(
            int(value) for value in trace_meta["records_per_core"].values()
        )
        label_uops = int(metrics["retired_uops"])
        delta_uops = label_uops - trace_records
        delta_ppm = 1_000_000.0 * delta_uops / max(trace_records, 1)
        exact = delta_uops == 0
        tolerance = args.max_boundary_uops_per_core * cores
        matches = abs(delta_uops) <= tolerance
        if not matches:
            errors.append(
                f"stream mismatch {metrics['uarch']}/c{cores:02d}/W_{workload}: "
                f"trace={trace_records} label={label_uops} delta={delta_uops} "
                f"tolerance={tolerance}"
            )
        rows.append(
            {
                "trace_id": trace_id(trace_meta),
                "trace_manifest": str(trace_dir / "manifest.txt"),
                "syscall_sidecar": str(trace_dir / "syscalls.jsonl"),
                "uarch": metrics["uarch"],
                "workload": workload,
                "cores": cores,
                "seed": seed,
                "scale": int(metrics["scale"]),
                "trace_uops": trace_records,
                "label_uops": label_uops,
                "stream_delta_uops": delta_uops,
                "stream_delta_ppm": delta_ppm,
                "stream_exact": exact,
                "stream_match": matches,
                "aggregate_uop_cpi": metrics["aggregate_uop_cpi"],
                "aggregate_macro_cpi": metrics["aggregate_macro_cpi"],
                "metrics": str(metrics_path),
            }
        )

    manifest = {
        "schema": "fastsim-uarch-functional-label-dataset-v1",
        "root": str(root),
        "cases": rows,
        "errors": errors,
    }
    (root / "dataset-index.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    fields = list(rows[0]) if rows else [
        "trace_id", "uarch", "workload", "cores", "seed", "stream_match"
    ]
    with (root / "dataset-index.csv").open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"linked cases={len(rows)} errors={len(errors)} root={root}")
    if errors and not args.allow_stream_mismatch:
        for error in errors[:20]:
            print(f"[link:error] {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
