#!/usr/bin/env python3
"""Fail-fast audit for the query-preserving K/V training cache."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Dict, List


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--manifest", default="data/v29_global_time_dataset/manifest.json",
    )
    args = parser.parse_args()
    manifest_path = Path(args.manifest).resolve()
    with manifest_path.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    if manifest.get("schema_version") != "tcsim-v29-manifest-1":
        raise SystemExit("unsupported v29 manifest schema")
    quality = dict(manifest.get("quality", {}))
    if quality.get("status") != "pass":
        raise SystemExit(f"manifest quality is not pass: {quality}")
    splits = dict(manifest.get("splits", {}))
    selected: Dict[str, List[Dict[str, Any]]] = {}
    missing = []
    forbidden = []
    for split in ("train", "validation"):
        rows = [dict(row) for row in splits.get(split, [])]
        if not rows:
            raise SystemExit(f"manifest split is empty: {split}")
        selected[split] = rows
        for row in rows:
            role = str(row.get("workload_role", ""))
            workload = str(row.get("workload", ""))
            if "heldout" in role.lower() or "heldout" in workload.lower():
                forbidden.append(f"{split}:{workload}:{role}")
            cache_dir = os.path.abspath(str(row.get("cache_dir", "")))
            if not cache_dir or not os.path.isfile(os.path.join(cache_dir, "meta.json")):
                missing.append(cache_dir or f"{split}:{workload}")
    if forbidden:
        raise SystemExit(
            "formal heldout leaked into train/validation: "
            + ", ".join(forbidden[:8])
        )
    if missing:
        raise SystemExit(
            f"{len(missing)} cache directories are incomplete: "
            + ", ".join(missing[:8])
        )
    train_paths = {os.path.abspath(str(row["cache_dir"])) for row in selected["train"]}
    validation_paths = {
        os.path.abspath(str(row["cache_dir"])) for row in selected["validation"]
    }
    overlap = train_paths & validation_paths
    for path in overlap:
        train_rows = [
            row for row in selected["train"]
            if os.path.abspath(str(row["cache_dir"])) == path
        ]
        validation_rows = [
            row for row in selected["validation"]
            if os.path.abspath(str(row["cache_dir"])) == path
        ]
        if any(
            row.get("sample_split", {}).get("partition") != "train"
            for row in train_rows
        ) or any(
            row.get("sample_split", {}).get("partition") != "validation"
            for row in validation_rows
        ):
            raise SystemExit(f"unsafe train/validation cache overlap: {path}")
    summary = {
        "schema": "tcsim-v29-query-kv-train-cache-audit-1",
        "manifest": str(manifest_path),
        "quality": "pass",
        "train_traces": len(selected["train"]),
        "validation_traces": len(selected["validation"]),
        "train_workloads": len({row["workload"] for row in selected["train"]}),
        "validation_workloads": len({
            row["workload"] for row in selected["validation"]
        }),
        "train_core_counts": sorted({
            int(row["n_cores"]) for row in selected["train"]
        }),
        "validation_core_counts": sorted({
            int(row["n_cores"]) for row in selected["validation"]
        }),
        "heldout_in_training": False,
        "cache_directories_complete": True,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
