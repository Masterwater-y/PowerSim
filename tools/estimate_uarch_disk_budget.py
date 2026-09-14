#!/usr/bin/env python3
"""Estimate storage for a uarch matrix from a completed reference sweep."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


GIB = 1 << 30
TIB = 1 << 40


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def du_bytes(path: Path) -> int:
    result = subprocess.run(
        ["du", "-sb", str(path)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return int(result.stdout.split()[0])


def human(value: int) -> str:
    return f"{value / TIB:.3f} TiB"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--reference-root", type=Path, required=True)
    parser.add_argument("--reference-cases", type=int, required=True)
    parser.add_argument("--filesystem-path", type=Path, required=True)
    parser.add_argument("--hard-reserve-bytes", type=int, default=TIB)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    matrix = load(args.matrix.resolve())
    planned_cases = (
        len(matrix["profiles"]) * len(matrix["workloads"]) * len(matrix["core_counts"])
    )
    reference_bytes = du_bytes(args.reference_root.resolve())
    formal_bytes = (reference_bytes * planned_cases + args.reference_cases - 1) // args.reference_cases
    # Short smoke traces are small, but source warmup/checkpoint material is not
    # proportional to ROI length.  Reserve 12% of formal size with a 64 GiB floor.
    smoke_bytes = max((formal_bytes * 12 + 99) // 100, 64 * GIB)
    # Conversion scratch, failed-attempt retention and replay outputs share the
    # filesystem.  A 20% margin prevents the point estimate from consuming the
    # operational reserve.
    transient_bytes = (formal_bytes * 20 + 99) // 100
    estimated_additional = formal_bytes + smoke_bytes + transient_bytes
    usage = shutil.disk_usage(args.filesystem_path.resolve())
    projected_free = usage.free - estimated_additional
    safe = projected_free >= args.hard_reserve_bytes
    result = {
        "schema": "fastsim-uarch-disk-budget-v1",
        "matrix": str(args.matrix.resolve()),
        "planned_cases": planned_cases,
        "reference_root": str(args.reference_root.resolve()),
        "reference_cases": args.reference_cases,
        "reference_bytes": reference_bytes,
        "estimated_formal_bytes": formal_bytes,
        "estimated_smoke_bytes": smoke_bytes,
        "estimated_transient_headroom_bytes": transient_bytes,
        "estimated_additional_bytes": estimated_additional,
        "current_free_bytes": usage.free,
        "projected_free_bytes": projected_free,
        "hard_reserve_bytes": args.hard_reserve_bytes,
        "safe_to_start": safe,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "disk-budget.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    lines = [
        "# Microarchitecture sweep disk budget",
        "",
        f"- Planned cases: {planned_cases}",
        f"- Reference: {args.reference_cases} cases, {human(reference_bytes)}",
        f"- Estimated formal data: {human(formal_bytes)}",
        f"- Estimated smoke data: {human(smoke_bytes)}",
        f"- Conversion/retry headroom: {human(transient_bytes)}",
        f"- Estimated additional peak: {human(estimated_additional)}",
        f"- Current free: {human(usage.free)}",
        f"- Projected free: {human(projected_free)}",
        f"- Hard reserve: {human(args.hard_reserve_bytes)}",
        f"- Safe to start: {'yes' if safe else 'no'}",
    ]
    (args.out / "disk-budget.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return 0 if safe else 2


if __name__ == "__main__":
    raise SystemExit(main())
