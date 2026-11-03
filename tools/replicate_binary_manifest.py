#!/usr/bin/env python3
"""Replicate a dense FastSim binary manifest without copying trace data."""

from __future__ import annotations

import argparse
from pathlib import Path


def read_manifest(path: Path) -> list[tuple[int, Path, int]]:
    entries = []
    for line_number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.partition("#")[0].strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) not in (3, 4):
            raise SystemExit(f"{path}:{line_number}: invalid manifest row")
        core_id = int(fields[0])
        if fields[1] not in ("fastsim-binary", "binary"):
            raise SystemExit(
                f"{path}:{line_number}: only binary traces can be replicated"
            )
        trace = Path(fields[2])
        if not trace.is_absolute():
            trace = (path.parent / trace).resolve()
        source_core = int(fields[3]) if len(fields) == 4 else core_id
        entries.append((core_id, trace, source_core))
    entries.sort()
    if [entry[0] for entry in entries] != list(range(len(entries))):
        raise SystemExit(f"{path}: core IDs must be dense from zero")
    return entries


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--copies", required=True, type=int)
    args = parser.parse_args()

    if args.copies < 1:
        raise SystemExit("--copies must be positive")
    source = Path(args.input).resolve()
    entries = read_manifest(source)
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as manifest:
        manifest.write(
            "# <core-id> <format> <path> <source-core-id>\n"
        )
        target_core = 0
        for _ in range(args.copies):
            for _, trace, source_core in entries:
                manifest.write(
                    f"{target_core} fastsim-binary {trace} "
                    f"{source_core}\n"
                )
                target_core += 1
    print(f"cores={target_core} manifest={output}")


if __name__ == "__main__":
    main()
