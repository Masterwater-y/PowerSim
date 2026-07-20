#!/usr/bin/env python3
"""Rebuild v2 static dictionaries from every observed dynamic macro PC.

Unlike the historical Phase-0 builder, this tool unions ``macro_pc.npy`` from
the packed v29 traces before decoding.  Every observed dynamic instruction PC
therefore becomes a required targeted-decode seed.  Coverage is checked again
against every scanned array after the parquet is written.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Dict, Iterable

import numpy as np
import pyarrow.parquet as pq

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from data.build_static_dict import (  # noqa: E402
    STATIC_DICT_SCHEMA_VERSION,
    build_binary,
)


def normalized_binary_name(value: str) -> str:
    name = str(value).strip()
    if name.startswith("W_"):
        name = name[2:]
    if not name.startswith("v28_"):
        raise ValueError(f"unsupported workload/binary name {value!r}")
    return name


def discover_pc_arrays(
    traces_root: Path,
    *,
    core0_only: bool,
) -> Dict[str, list[Path]]:
    result: Dict[str, list[Path]] = {}
    for workload_dir in sorted(traces_root.glob("*/W_v28_*")):
        if not (workload_dir / "meta.json").is_file():
            continue
        binary_name = normalized_binary_name(workload_dir.name)
        pattern = "cores/0/macro_pc.npy" if core0_only else "cores/*/macro_pc.npy"
        paths = sorted(workload_dir.glob(pattern))
        if not paths:
            raise RuntimeError(f"no macro_pc arrays under {workload_dir}")
        result.setdefault(binary_name, []).extend(paths)
    return result


def unique_dynamic_pcs(paths: Iterable[Path]) -> tuple[set[int], int]:
    unique: set[int] = set()
    values_seen = 0
    for path in paths:
        values = np.load(path, mmap_mode="r")
        if values.ndim != 1 or values.dtype.kind not in {"u", "i"}:
            raise RuntimeError(f"invalid macro_pc array {path}: {values.shape}/{values.dtype}")
        values_seen += int(values.size)
        unique.update(int(value) for value in np.unique(values))
    return unique, values_seen


def parquet_coverage(parquet: Path, required_pcs: set[int]) -> Dict[str, Any]:
    table = pq.read_table(
        parquet, columns=["schema_version", "module_pc", "mnemonic"],
    )
    schemas = {str(value) for value in table["schema_version"].to_pylist()}
    if schemas != {STATIC_DICT_SCHEMA_VERSION}:
        raise RuntimeError(f"unexpected static schemas in {parquet}: {schemas}")
    rows = {
        int(pc): str(mnemonic).strip().lower()
        for pc, mnemonic in zip(
            table["module_pc"].to_pylist(),
            table["mnemonic"].to_pylist(),
        )
    }
    missing = sorted(required_pcs - set(rows))
    invalid = sorted(
        pc for pc in required_pcs
        if rows.get(pc, "") in {"", "(bad)", ".byte"}
    )
    return {
        "required_unique_pcs": len(required_pcs),
        "missing": missing,
        "invalid": invalid,
        "coverage_fraction": (
            (len(required_pcs) - len(missing) - len(invalid))
            / max(1, len(required_pcs))
        ),
    }


def load_manifest(path: Path) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                result[str(row["binary_name"])] = row
    return result


def write_manifest(path: Path, rows: Dict[str, Dict[str, Any]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as handle:
        for name in sorted(rows):
            handle.write(json.dumps(rows[name], sort_keys=True) + "\n")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--traces-root",
        default="/data00/yinhaolang/TCSim/data/v29_global_time_dataset/traces",
    )
    parser.add_argument(
        "--workload-bin", default="/data00/yinhaolang/TSim/workloads/bin",
    )
    parser.add_argument(
        "--out", default="/data00/yinhaolang/LLMSim/data/v28_1/static_dict",
    )
    parser.add_argument(
        "--workload", action="append", default=[],
        help="limit to a v28_* binary or W_v28_* workload; may be repeated",
    )
    parser.add_argument(
        "--core0-only", action="store_true",
        help="diagnostic shortcut; full acceptance must scan every core",
    )
    parser.add_argument(
        "--reuse", action="store_true",
        help="do not force rebuilding an existing hash-named parquet",
    )
    parser.add_argument("--report", default="")
    args = parser.parse_args()

    traces_root = Path(args.traces_root)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    discovered = discover_pc_arrays(
        traces_root, core0_only=bool(args.core0_only),
    )
    selected = (
        {normalized_binary_name(value) for value in args.workload}
        if args.workload else set(discovered)
    )
    missing_workloads = selected - set(discovered)
    if missing_workloads:
        raise RuntimeError(
            f"no packed traces for workloads {sorted(missing_workloads)}"
        )
    manifest_path = out_dir / "manifest.jsonl"
    manifest = load_manifest(manifest_path)
    reports = []
    failures = []
    started = time.perf_counter()
    for binary_name in sorted(selected):
        paths = discovered[binary_name]
        required, values_seen = unique_dynamic_pcs(paths)
        binary_path = Path(args.workload_bin) / binary_name
        if not binary_path.is_file():
            raise FileNotFoundError(binary_path)
        begin = time.perf_counter()
        binary_hash, parquet_text, n_rows = build_binary(
            str(binary_path),
            str(out_dir),
            force=not bool(args.reuse),
            required_pcs=required,
        )
        coverage = parquet_coverage(Path(parquet_text), required)
        elapsed = time.perf_counter() - begin
        row = {
            "binary_name": binary_name,
            "binary_path": str(binary_path),
            "binary_hash": binary_hash,
            "schema_version": STATIC_DICT_SCHEMA_VERSION,
            "parquet": parquet_text,
            "n_rows": int(n_rows),
            "n_required_dynamic_pcs": len(required),
            "n_scanned_pc_arrays": len(paths),
            "n_scanned_dynamic_pc_records": values_seen,
            "scan_scope": "core0_only" if args.core0_only else "all_cores",
            "elapsed_s": elapsed,
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        manifest[binary_name] = row
        report_row = {**row, "coverage": coverage}
        reports.append(report_row)
        if coverage["missing"] or coverage["invalid"]:
            failures.append(
                f"{binary_name}: missing={len(coverage['missing'])} "
                f"invalid={len(coverage['invalid'])}"
            )
        print(
            f"[macro static] {binary_name}: arrays={len(paths)} "
            f"pc_records={values_seen} unique_pc={len(required)} rows={n_rows} "
            f"coverage={coverage['coverage_fraction']:.6f} dt={elapsed:.1f}s",
            flush=True,
        )
    write_manifest(manifest_path, manifest)
    report = {
        "status": "PASS" if not failures else "FAIL",
        "schema_version": STATIC_DICT_SCHEMA_VERSION,
        "scan_scope": "core0_only" if args.core0_only else "all_cores",
        "n_workloads": len(reports),
        "elapsed_s": time.perf_counter() - started,
        "workloads": reports,
        "failures": failures,
    }
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.report:
        destination = Path(args.report)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(rendered + "\n")
        os.replace(temporary, destination)
    print(rendered)
    return 0 if not failures else 2


if __name__ == "__main__":
    sys.exit(main())
