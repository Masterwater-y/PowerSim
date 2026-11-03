#!/usr/bin/env python3
"""Upgrade two-phase FST manifests to exact record-bounded slices."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def matrix_results(matrix: Path) -> list[Path]:
    status_path = matrix / "status.json" if matrix.is_dir() else matrix
    status = read_json(status_path)
    summary = status.get("summary", {})
    if summary.get("status") != "complete" or summary.get("return_code") != 0:
        raise ValueError(f"incomplete matrix: {status_path}")
    results = []
    for name, task in sorted(status.get("tasks", {}).items()):
        sample = task.get("sample", {})
        if sample.get("status") != "completed" or sample.get("return_code") != 0:
            raise ValueError(f"incomplete matrix task: {status_path}:{name}")
        results.append(Path(sample["result_dir"]).resolve())
    return results


def upgraded_manifest(result_dir: Path) -> tuple[Path, str]:
    trace_dir = result_dir / "tao_trace"
    manifest_path = trace_dir / "manifest.txt"
    metadata = read_json(trace_dir / "trace.json")
    if metadata.get("functional_warmup_enabled") is not True:
        raise ValueError(f"not a two-phase trace: {result_dir}")
    per_core = metadata.get("per_core", {})
    rows = [line.split() for line in manifest_path.read_text().splitlines() if line.strip()]
    if len(rows) != len(per_core):
        raise ValueError(f"manifest/core-count mismatch: {result_dir}")
    output = []
    for core, row in enumerate(rows):
        if (
            len(row) not in (6, 8)
            or int(row[0]) != core
            or row[1] != "fastsim-binary-warmup-slice"
            or int(row[3]) != core
        ):
            raise ValueError(f"invalid warmup manifest row: {result_dir}: {row}")
        declared = per_core[str(core)]
        instruction_counts = (
            int(declared["warmup_instructions"]),
            int(declared["measurement_instructions"]),
        )
        if (int(row[4]), int(row[5])) != instruction_counts:
            raise ValueError(f"manifest/instruction mismatch: {result_dir}:core{core}")
        output.append(
            f"{core} fastsim-binary-warmup-slice {row[2]} {core} "
            f"{instruction_counts[0]} {instruction_counts[1]} "
            f"{int(declared['warmup_records'])} "
            f"{int(declared['measurement_records'])}\n"
        )
    return manifest_path, "".join(output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", action="append", type=Path, required=True)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    results: list[Path] = []
    for matrix in args.matrix:
        results.extend(matrix_results(matrix))
    if len(results) != len(set(results)):
        raise SystemExit("duplicate result directories across matrices")
    changed = 0
    for result_dir in results:
        path, content = upgraded_manifest(result_dir)
        if path.read_text() == content:
            continue
        changed += 1
        if args.write:
            temporary = path.with_suffix(".txt.tmp-record-bounds")
            temporary.write_text(content)
            temporary.replace(path)
    print(json.dumps({"results": len(results), "changed": changed, "written": args.write}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
