#!/usr/bin/env python3
"""Audit FST v7 privilege tags and summarize user/kernel populations."""

from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path


HEADER = struct.Struct("<8sIIIIQQ4Q")
RECORD = struct.Struct("<QQQQ4IHHhBB4BI")
MAGIC = b"FSTRC01\0"
HEADER_BYTES = 72
RECORD_BYTES = 64
FEATURE_PRIVILEGE = 1 << 4
KNOWN_FEATURES = (1 << 5) - 1
RETIRE = 1 << 0
LOAD = 1 << 1
STORE = 1 << 2
ATOMIC = 1 << 3
MICRO_OP = 1 << 10
LAST_MICRO_OP = 1 << 11
SYSCALL_OP_CLASS = -1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fst", action="append", type=Path, default=[])
    parser.add_argument(
        "--trace-dir", action="append", type=Path, default=[],
        help="Directory recursively searched for *.fst; repeatable.",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-kernel", action="store_true")
    parser.add_argument("--require-user", action="store_true")
    return parser.parse_args()


def audit_file(path: Path) -> dict[str, int | str | bool]:
    path = path.resolve()
    with path.open("rb") as source:
        raw_header = source.read(HEADER_BYTES)
        if len(raw_header) != HEADER_BYTES:
            raise ValueError(f"short FST header: {path}")
        (
            magic, version, header_size, record_size, core_id,
            record_count, features, metadata_offset, metadata_count,
            metadata_size, syscall_abi,
        ) = HEADER.unpack(raw_header)
        del metadata_offset, metadata_count, metadata_size, syscall_abi
        if (
            magic != MAGIC or version != 7 or header_size != HEADER_BYTES
            or record_size != RECORD_BYTES
        ):
            raise ValueError(f"unsupported canonical FST v7 header: {path}")
        if features & ~KNOWN_FEATURES:
            raise ValueError(f"unknown FST feature bits: {path}")

        result: dict[str, int | str | bool] = {
            "path": str(path),
            "core_id": core_id,
            "records": record_count,
            "privilege_feature": bool(features & FEATURE_PRIVILEGE),
            "user_records": 0,
            "kernel_records": 0,
            "syscall_markers": 0,
            "user_retired_uops": 0,
            "kernel_retired_uops": 0,
            "user_retired_instructions": 0,
            "kernel_retired_instructions": 0,
            "user_memory_uops": 0,
            "kernel_memory_uops": 0,
        }
        for ordinal in range(record_count):
            raw = source.read(RECORD_BYTES)
            if len(raw) != RECORD_BYTES:
                raise ValueError(f"truncated record {ordinal}: {path}")
            values = RECORD.unpack(raw)
            flags = values[9]
            op_class = values[10]
            kernel = op_class < SYSCALL_OP_CLASS
            if kernel and not features & FEATURE_PRIVILEGE:
                raise ValueError(
                    f"kernel record {ordinal} lacks privilege feature: {path}"
                )
            domain = "kernel" if kernel else "user"
            result[f"{domain}_records"] += 1
            if op_class == SYSCALL_OP_CLASS:
                result["syscall_markers"] += 1
            if flags & RETIRE:
                result[f"{domain}_retired_uops"] += 1
                if not flags & MICRO_OP or flags & LAST_MICRO_OP:
                    result[f"{domain}_retired_instructions"] += 1
                if flags & (LOAD | STORE | ATOMIC):
                    result[f"{domain}_memory_uops"] += 1

        if bool(result["kernel_records"]) != bool(
            features & FEATURE_PRIVILEGE
        ):
            raise ValueError(
                f"privilege feature/population mismatch: {path}"
            )
        return result


def main() -> int:
    args = parse_args()
    paths = {path.resolve() for path in args.fst}
    for directory in args.trace_dir:
        paths.update(path.resolve() for path in directory.rglob("*.fst"))
    if not paths:
        raise SystemExit("no FST inputs; use --fst or --trace-dir")

    files = [audit_file(path) for path in sorted(paths)]
    totals = {
        key: sum(int(row[key]) for row in files)
        for key in (
            "records", "user_records", "kernel_records", "syscall_markers",
            "user_retired_uops", "kernel_retired_uops",
            "user_retired_instructions", "kernel_retired_instructions",
            "user_memory_uops", "kernel_memory_uops",
        )
    }
    if args.require_kernel and totals["kernel_records"] == 0:
        raise SystemExit("privilege audit failed: no kernel records")
    if args.require_user and totals["user_records"] == 0:
        raise SystemExit("privilege audit failed: no user records")
    report = {"schema": "fst-privilege-audit-v1", "files": files,
              "totals": totals}
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(encoded)
    else:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
