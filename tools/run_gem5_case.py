#!/usr/bin/env python3
"""Run one reproducible gem5 functional-trace validation case."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fastsim", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--cores",
        type=int,
        help="Override sim.cores from the configuration",
    )
    parser.add_argument(
        "--trace-glob",
        action="append",
        required=True,
        help="Aligned Parquet path/glob; repeat as needed",
    )
    parser.add_argument("--gem5-stats", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--allow-missing-branch-outcomes", action="store_true")
    parser.add_argument("--allow-missing-core-features", action="store_true")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    output = Path(args.out_dir).resolve()
    output.mkdir(parents=True, exist_ok=False)
    trace_dir = output / "trace"

    convert = [
        sys.executable,
        str(root / "tools" / "convert_aligned_parquet.py"),
    ]
    for pattern in args.trace_glob:
        convert.extend(["--input", pattern])
    convert.extend(["--out-dir", str(trace_dir)])
    if args.allow_missing_branch_outcomes:
        convert.append("--allow-missing-branch-outcomes")
    if args.allow_missing_core_features:
        convert.append("--allow-missing-core-features")
    subprocess.run(convert, check=True)

    fastsim_stats = output / "fastsim-stats.json"
    simulate = [
        str(Path(args.fastsim).resolve()),
        "simulate",
        "--measurement-scope",
        "user",
        "--config",
        str(Path(args.config).resolve()),
        "--manifest",
        str(trace_dir / "manifest.txt"),
        "--output",
        str(fastsim_stats),
    ]
    if args.cores is not None:
        simulate.extend(["--cores", str(args.cores)])
    subprocess.run(simulate, check=True)

    validation = output / "validation.json"
    validate = [
        sys.executable,
        str(root / "tools" / "validate_gem5_pmu.py"),
        "--fastsim-stats",
        str(fastsim_stats),
        "--gem5-stats",
        str(Path(args.gem5_stats).resolve()),
        "--output",
        str(validation),
    ]
    for pattern in args.trace_glob:
        validate.extend(["--aligned", pattern])
    subprocess.run(validate, check=True)
    print(f"fastsim_stats={fastsim_stats}")
    print(f"validation={validation}")


if __name__ == "__main__":
    main()
