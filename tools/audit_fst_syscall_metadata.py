#!/usr/bin/env python3
"""Strictly audit FST v7 syscall metadata and report field coverage."""

from __future__ import annotations

import argparse
import json
import struct
from collections import Counter
from pathlib import Path


HEADER = struct.Struct("<8sIIIIQQ4Q")
RECORD_PREFIX = struct.Struct("<QQ")
SYSCALL_ROW = struct.Struct("<QQQQ6QQQQIIIHBBQ")
HEADER_BYTES = 72
RECORD_BYTES = 64
SYSCALL_BYTES = 128
MAGIC = b"FSTRC01\0"
FEATURE_SYSCALL = 1 << 1
FEATURE_METADATA = 1 << 3
LINUX_X86_64 = 1
FIELDS = {
    "arguments": 1 << 0,
    "return_value": 1 << 1,
    "failure": 1 << 2,
    "errno": 1 << 3,
    "pre_timestamp": 1 << 4,
    "post_timestamp": 1 << 5,
    "pre_cpu": 1 << 6,
    "post_cpu": 1 << 7,
    "maybe_blocking": 1 << 8,
    "thread_id": 1 << 9,
}
KNOWN_FIELDS = sum(FIELDS.values())
FAILED = 1 << 0
MAYBE_BLOCKING = 1 << 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fst", action="append", type=Path, default=[])
    parser.add_argument(
        "--trace-dir", action="append", type=Path, default=[],
        help="Directory recursively searched for *.fst; repeatable.",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--require-entry-coverage", action="store_true",
        help=(
            "Fail unless every syscall has arguments, pre timestamp, and "
            "pre CPU metadata. Return-side fields remain optional."
        ),
    )
    parser.add_argument(
        "--require-semantic-plausibility", action="store_true",
        help=(
            "Fail when portable entry/return fields contradict Linux x86-64 "
            "syscall semantics. This currently gates successful mmap rows."
        ),
    )
    return parser.parse_args()


def semantic_violations(
    number: int,
    arguments: tuple[int, ...],
    argument_count: int,
    valid: int,
    flags: int,
) -> list[str]:
    """Return conservative ABI contradictions, never inferred metadata.

    These checks intentionally cover only conditions that the Linux x86-64
    kernel requires and that are visible in portable drmemtrace/TaoTrace
    fields. They are not a syscall emulator.
    """
    successful = bool(valid & FIELDS["failure"]) and not bool(flags & FAILED)
    has_arguments = bool(valid & FIELDS["arguments"])
    violations: list[str] = []

    # mmap(addr, length, prot, flags, fd, offset): a successful Linux mmap
    # must have non-zero length and exactly one MAP_TYPE value. MAP_SHARED,
    # MAP_PRIVATE and MAP_SHARED_VALIDATE encode as 1, 2 and 3 respectively.
    if number == 9 and successful and has_arguments and argument_count >= 6:
        if arguments[1] == 0:
            violations.append("successful_mmap_zero_length")
        if arguments[3] & 0x3 == 0:
            violations.append("successful_mmap_missing_map_type")
    return violations


def load_warmup_records(path: Path, core_id: int) -> int | None:
    """Return the manifest boundary when the FST has a sibling trace.json."""
    manifest_path = path.parent / "trace.json"
    if not manifest_path.is_file():
        return None
    manifest = json.loads(manifest_path.read_text())
    boundary = manifest.get("functional_boundaries", {}).get(str(core_id), {})
    value = boundary.get("warmup_records")
    if value is None:
        return None
    return int(value)


def audit_file(path: Path) -> dict:
    path = path.resolve()
    with path.open("rb") as source:
        raw_header = source.read(HEADER_BYTES)
        if len(raw_header) != HEADER_BYTES:
            raise ValueError(f"short FST header: {path}")
        (
            magic, version, header_size, record_size, core_id,
            records, features, metadata_offset, metadata_count,
            metadata_size, syscall_abi,
        ) = HEADER.unpack(raw_header)
        records_end = HEADER_BYTES + records * RECORD_BYTES
        if (
            magic != MAGIC or version != 7 or header_size != HEADER_BYTES
            or record_size != RECORD_BYTES or syscall_abi != LINUX_X86_64
        ):
            raise ValueError(f"unsupported canonical FST v7 header: {path}")
        if features & FEATURE_METADATA:
            if (
                not features & FEATURE_SYSCALL
                or metadata_offset != records_end
                or metadata_count == 0
                or metadata_size != SYSCALL_BYTES
                or path.stat().st_size
                != metadata_offset + metadata_count * metadata_size
            ):
                raise ValueError(f"invalid syscall metadata table: {path}")
        elif (
            metadata_offset != 0 or metadata_count != 0
            or metadata_size != 0 or path.stat().st_size != records_end
        ):
            raise ValueError(f"invalid metadata-free FST v7: {path}")

        coverage = Counter()
        syscall_numbers = Counter()
        errno_values = Counter()
        migrations = 0
        failed = 0
        maybe_blocking = 0
        durations = []
        missing_return_numbers = Counter()
        missing_return_phases = Counter()
        missing_return_maybe_blocking = 0
        missing_return_preview = []
        semantic_violation_counts = Counter()
        semantic_violation_preview = []
        previous_record = -1
        warmup_records = load_warmup_records(path, core_id)
        for expected_ordinal in range(metadata_count):
            source.seek(metadata_offset + expected_ordinal * SYSCALL_BYTES)
            raw = source.read(SYSCALL_BYTES)
            if len(raw) != SYSCALL_BYTES:
                raise ValueError(f"truncated syscall row: {path}")
            values = SYSCALL_ROW.unpack(raw)
            record_ordinal, syscall_ordinal, thread_id, number = values[:4]
            arguments = values[4:10]
            return_value, pre_timestamp, post_timestamp = values[10:13]
            errno_value, pre_cpu, post_cpu = values[13:16]
            valid, argument_count, flags, reserved = values[16:20]
            if (
                syscall_ordinal != expected_ordinal
                or record_ordinal <= previous_record
                or record_ordinal >= records
                or valid & ~KNOWN_FIELDS
                or argument_count > 6
                or reserved != 0
                or flags & ~(FAILED | MAYBE_BLOCKING)
            ):
                raise ValueError(
                    f"invalid syscall row ordinal={expected_ordinal}: {path}"
                )
            if argument_count and not valid & FIELDS["arguments"]:
                raise ValueError(f"argument count without validity: {path}")
            if flags & FAILED and not valid & FIELDS["failure"]:
                raise ValueError(f"failed flag without validity: {path}")
            if valid & FIELDS["errno"] and not flags & FAILED:
                raise ValueError(f"errno without failed flag: {path}")
            if flags & MAYBE_BLOCKING and not valid & FIELDS["maybe_blocking"]:
                raise ValueError(f"blocking flag without validity: {path}")

            row_semantic_violations = semantic_violations(
                number, arguments, argument_count, valid, flags
            )
            semantic_violation_counts.update(row_semantic_violations)
            if row_semantic_violations and len(semantic_violation_preview) < 64:
                semantic_violation_preview.append({
                    "syscall_ordinal": syscall_ordinal,
                    "record_ordinal": record_ordinal,
                    "syscall_number": number,
                    "arguments": list(arguments[:argument_count]),
                    "return_value_raw": return_value,
                    "violations": row_semantic_violations,
                })

            source.seek(HEADER_BYTES + record_ordinal * RECORD_BYTES)
            record = source.read(RECORD_BYTES)
            pc, inline_number = RECORD_PREFIX.unpack_from(record)
            op_class = int.from_bytes(record[52:54], "little", signed=True)
            if op_class != -1 or inline_number != number:
                raise ValueError(
                    f"syscall row/hot record mismatch ordinal={record_ordinal}: "
                    f"{path}"
                )
            del pc, thread_id, arguments, return_value
            previous_record = record_ordinal
            syscall_numbers[number] += 1
            for name, bit in FIELDS.items():
                if valid & bit:
                    coverage[name] += 1
            if flags & FAILED:
                failed += 1
            if flags & MAYBE_BLOCKING:
                maybe_blocking += 1
            if valid & FIELDS["errno"]:
                errno_values[errno_value] += 1
            if (
                valid & FIELDS["pre_cpu"]
                and valid & FIELDS["post_cpu"]
                and pre_cpu != post_cpu
            ):
                migrations += 1
            if (
                valid & FIELDS["pre_timestamp"]
                and valid & FIELDS["post_timestamp"]
            ):
                if post_timestamp < pre_timestamp:
                    raise ValueError(f"negative syscall duration: {path}")
                durations.append(post_timestamp - pre_timestamp)
            if not valid & FIELDS["return_value"]:
                phase = (
                    "unknown" if warmup_records is None
                    else "warmup" if record_ordinal < warmup_records
                    else "measurement"
                )
                missing_return_numbers[number] += 1
                missing_return_phases[phase] += 1
                if valid & FIELDS["maybe_blocking"] and flags & MAYBE_BLOCKING:
                    missing_return_maybe_blocking += 1
                if len(missing_return_preview) < 64:
                    missing_return_preview.append({
                        "syscall_ordinal": syscall_ordinal,
                        "record_ordinal": record_ordinal,
                        "syscall_number": number,
                        "phase": phase,
                        "distance_to_measurement": (
                            None if warmup_records is None
                            else record_ordinal - warmup_records
                        ),
                    })

    return {
        "path": str(path),
        "core_id": core_id,
        "records": records,
        "syscalls": metadata_count,
        "field_coverage": dict(sorted(coverage.items())),
        "syscall_numbers": dict(sorted(syscall_numbers.items())),
        "warmup_records": warmup_records,
        "missing_return": sum(missing_return_numbers.values()),
        "missing_return_by_syscall": dict(sorted(missing_return_numbers.items())),
        "missing_return_by_phase": dict(sorted(missing_return_phases.items())),
        "missing_return_maybe_blocking": missing_return_maybe_blocking,
        "missing_return_other": (
            sum(missing_return_numbers.values())
            - missing_return_maybe_blocking
        ),
        "missing_return_preview": missing_return_preview,
        "semantic_violations": sum(semantic_violation_counts.values()),
        "semantic_violation_counts": dict(sorted(semantic_violation_counts.items())),
        "semantic_violation_preview": semantic_violation_preview,
        "failed": failed,
        "maybe_blocking": maybe_blocking,
        "cpu_migrations": migrations,
        "errno_values": dict(sorted(errno_values.items())),
        "duration_us": {
            "count": len(durations),
            "minimum": min(durations) if durations else None,
            "maximum": max(durations) if durations else None,
        },
    }


def main() -> int:
    args = parse_args()
    paths = list(args.fst)
    for root in args.trace_dir:
        paths.extend(root.rglob("*.fst"))
    paths = sorted({path.resolve() for path in paths})
    if not paths:
        raise SystemExit("at least one --fst or --trace-dir containing FST is required")
    files = [audit_file(path) for path in paths]
    coverage = Counter()
    numbers = Counter()
    missing_return_numbers = Counter()
    missing_return_phases = Counter()
    semantic_violation_counts = Counter()
    for row in files:
        coverage.update(row["field_coverage"])
        numbers.update({int(key): value for key, value in row["syscall_numbers"].items()})
        missing_return_numbers.update({
            int(key): value for key, value in row["missing_return_by_syscall"].items()
        })
        missing_return_phases.update(row["missing_return_by_phase"])
        semantic_violation_counts.update(row["semantic_violation_counts"])
    total_syscalls = sum(row["syscalls"] for row in files)
    if args.require_entry_coverage:
        missing = {
            name: total_syscalls - coverage[name]
            for name in ("arguments", "pre_timestamp", "pre_cpu")
            if coverage[name] != total_syscalls
        }
        if missing:
            raise ValueError(f"incomplete syscall entry metadata: {missing}")
    if args.require_semantic_plausibility and semantic_violation_counts:
        raise ValueError(
            "semantically contradictory syscall metadata: "
            f"{dict(sorted(semantic_violation_counts.items()))}"
        )
    payload = {
        "schema": "fastsim-fst-v7-syscall-audit-v1",
        "valid": True,
        "totals": {
            "files": len(files),
            "records": sum(row["records"] for row in files),
            "syscalls": total_syscalls,
            "field_coverage": dict(sorted(coverage.items())),
            "field_coverage_fraction": {
                name: (coverage[name] / total_syscalls if total_syscalls else 0.0)
                for name in FIELDS
            },
            "syscall_numbers": dict(sorted(numbers.items())),
            "missing_return": sum(missing_return_numbers.values()),
            "missing_return_by_syscall": dict(sorted(missing_return_numbers.items())),
            "missing_return_by_phase": dict(sorted(missing_return_phases.items())),
            "missing_return_maybe_blocking": sum(
                row["missing_return_maybe_blocking"] for row in files
            ),
            "missing_return_other": sum(
                row["missing_return_other"] for row in files
            ),
            "semantic_violations": sum(semantic_violation_counts.values()),
            "semantic_violation_counts": dict(
                sorted(semantic_violation_counts.items())
            ),
            "failed": sum(row["failed"] for row in files),
            "maybe_blocking": sum(row["maybe_blocking"] for row in files),
            "cpu_migrations": sum(row["cpu_migrations"] for row in files),
        },
        "files": files,
    }
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
