#!/usr/bin/env python3
"""Audit completed gem5-FS two-phase functional-warmup matrices."""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
from collections import Counter
from pathlib import Path

from audit_fst_syscall_metadata import audit_file as audit_syscalls
from build_fst_v7_formal_dataset import (
    destination_class_coverage,
    read_header as read_fst_v7_header,
)


FST_HEADER = struct.Struct("<8sIIIIQQQQQQ")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def matrix_results(path: Path) -> list[Path]:
    status_path = path / "status.json" if path.is_dir() else path
    status = read_json(status_path)
    summary = status.get("summary", {})
    if summary.get("status") != "complete" or summary.get("return_code") != 0:
        raise ValueError(f"incomplete matrix: {status_path}")
    results = []
    for name, task in sorted(status.get("tasks", {}).items()):
        sample = task.get("sample", {})
        completed = (
            sample.get("status") == "completed"
            and sample.get("return_code") == 0
        )
        reused = (
            sample.get("status") == "skipped"
            and sample.get("reason") == "current successful result exists"
            and bool(sample.get("result_dir"))
        )
        if not (completed or reused):
            raise ValueError(f"incomplete matrix task: {status_path}:{name}")
        result_dir = sample.get("result_dir")
        if not result_dir:
            raise ValueError(f"missing result_dir: {status_path}:{name}")
        results.append(Path(result_dir).resolve())
    return results


def audit_result(
    result_dir: Path, require_destination_classes: bool = False
) -> dict:
    request = read_json(result_dir / "request.json")
    trace_dir = result_dir / "tao_trace"
    metadata = read_json(trace_dir / "trace.json")
    oracle = read_json(result_dir / "oracle" / "kernel_events.json")
    cores = int(request["profile"]["cores"])
    workload = request["workload_selection"]["workload"]
    if metadata.get("functional_warmup_enabled") is not True:
        raise ValueError(f"cold trace metadata: {result_dir}")
    per_core = metadata.get("per_core", {})
    boundaries = metadata.get("functional_boundaries", {})
    if len(per_core) != cores or len(boundaries) != cores:
        raise ValueError(f"trace core-count mismatch: {result_dir}")

    manifest_rows = [
        line.split()
        for line in (trace_dir / "manifest.txt").read_text().splitlines()
        if line.strip()
    ]
    if (
        len(manifest_rows) != cores
        or sorted(int(row[0]) for row in manifest_rows) != list(range(cores))
        or any(
            len(row) != 8 or row[1] != "fastsim-binary-warmup-slice"
            for row in manifest_rows
        )
    ):
        raise ValueError(f"invalid warmup manifest: {result_dir}")
    manifest_rows.sort(key=lambda row: int(row[0]))

    oracle_counts = {
        int(row["core_id"]): int(row["n_user"])
        for row in oracle.get("per_core", [])
    }
    warmup_records = 0
    warmup_instructions = 0
    measurement_records = 0
    measurement_user_records = 0
    measurement_instructions = 0
    syscall_events = 0
    syscall_field_coverage = Counter()
    destination_class_marked_records = 0
    destination_class_uops = 0
    for core in range(cores):
        declared = per_core[str(core)]
        boundary = boundaries[str(core)]
        manifest = manifest_rows[core]
        fst = trace_dir / f"core{core}.fst"
        with fst.open("rb") as handle:
            header = handle.read(FST_HEADER.size)
        (
            magic,
            version,
            header_size,
            record_size,
            source_core,
            records,
            features,
            metadata_offset,
            metadata_count,
            metadata_size,
            _syscall_abi,
        ) = FST_HEADER.unpack(header)
        records_end = FST_HEADER.size + records * record_size
        if version == 7 and (features & (1 << 3)):
            complete_size = (
                metadata_count > 0
                and metadata_offset == records_end
                and metadata_size == 128
                and fst.stat().st_size
                == metadata_offset + metadata_count * metadata_size
            )
        elif version == 7:
            complete_size = (
                metadata_offset == metadata_count == metadata_size == 0
                and fst.stat().st_size == records_end
            )
        else:
            complete_size = fst.stat().st_size == records_end
        if (
            magic != b"FSTRC01\0"
            or version not in (5, 6, 7)
            or header_size != FST_HEADER.size
            or record_size != 64
            or source_core != core
            or not complete_size
            or int(declared["records"]) != records
            or sha256(fst) != declared["sha256"]
        ):
            raise ValueError(f"invalid FST core {core}: {result_dir}")
        if version == 7:
            syscall_audit = audit_syscalls(fst)
            syscall_events += int(syscall_audit["syscalls"])
            syscall_field_coverage.update(
                syscall_audit["field_coverage"]
            )
            destination_audit = destination_class_coverage(
                fst, read_fst_v7_header(fst)
            )
            if (
                require_destination_classes
                and not destination_audit["complete"]
            ):
                raise ValueError(
                    f"incomplete destination classes core {core}: "
                    f"feature={destination_audit['feature']} "
                    f"marked={destination_audit['marked_records']}/"
                    f"{destination_audit['records']} "
                    f"missing_destination_uops="
                    f"{destination_audit['missing_destination_uops']}: "
                    f"{result_dir}"
                )
            destination_class_marked_records += int(
                destination_audit["marked_records"]
            )
            destination_class_uops += int(
                destination_audit["destination_uops"]
            )
        elif require_destination_classes:
            raise ValueError(
                f"destination classes require native FST v7 core {core}: "
                f"{result_dir}"
            )
        warm = int(boundary["warmup_records"])
        measured = int(boundary["measurement_records"])
        measured_user = int(
            boundary.get("measurement_user_records", measured)
        )
        if (
            boundary.get("measurement_started") is not True
            or boundary.get("target_reached") is not True
            or warm + measured != records
            or oracle_counts.get(core) != measured_user
            or int(declared["warmup_records"]) != warm
            or int(declared["measurement_records"]) != measured
            or int(
                declared.get("measurement_user_records", measured)
            ) != measured_user
            or int(manifest[4]) != int(boundary["warmup_instructions"])
            or int(manifest[5]) != int(boundary["measurement_instructions"])
            or int(manifest[6]) != warm
            or int(manifest[7]) != measured
        ):
            raise ValueError(f"phase/oracle mismatch core {core}: {result_dir}")
        warmup_records += warm
        warmup_instructions += int(boundary["warmup_instructions"])
        measurement_records += measured
        measurement_user_records += measured_user
        measurement_instructions += int(boundary["measurement_instructions"])
    if warmup_records <= 0 or warmup_instructions <= 0:
        raise ValueError(f"empty configuration warmup: {result_dir}")
    return {
        "result_dir": str(result_dir),
        "workload": workload,
        "cores": cores,
        "fst_files": cores,
        "warmup_records": warmup_records,
        "warmup_instructions": warmup_instructions,
        "measurement_records": measurement_records,
        "measurement_user_records": measurement_user_records,
        "measurement_instructions": measurement_instructions,
        "syscall_events": syscall_events,
        "syscall_field_coverage": dict(sorted(syscall_field_coverage.items())),
        "destination_class_marked_records": destination_class_marked_records,
        "destination_class_uops": destination_class_uops,
        "destination_classes_complete": (
            destination_class_marked_records
            == warmup_records + measurement_records
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-cases", type=int)
    parser.add_argument("--expected-fst-files", type=int)
    parser.add_argument(
        "--require-destination-classes",
        action="store_true",
        help="reject traces without exact per-record v7 destination classes",
    )
    args = parser.parse_args()
    result_dirs = []
    for matrix in args.matrix:
        result_dirs.extend(matrix_results(matrix))
    if len(result_dirs) != len(set(result_dirs)):
        raise SystemExit("duplicate result directories across matrices")
    rows = [
        audit_result(path, args.require_destination_classes)
        for path in result_dirs
    ]
    totals = {
        "cases": len(rows),
        "fst_files": sum(row["fst_files"] for row in rows),
        "warmup_records": sum(row["warmup_records"] for row in rows),
        "warmup_instructions": sum(row["warmup_instructions"] for row in rows),
        "measurement_records": sum(
            row["measurement_records"] for row in rows
        ),
        "measurement_user_records": sum(
            row["measurement_user_records"] for row in rows
        ),
        "measurement_instructions": sum(
            row["measurement_instructions"] for row in rows
        ),
        "integrity_errors": 0,
        "syscall_events": sum(row["syscall_events"] for row in rows),
        "destination_class_marked_records": sum(
            row["destination_class_marked_records"] for row in rows
        ),
        "destination_class_uops": sum(
            row["destination_class_uops"] for row in rows
        ),
        "destination_classes_complete": all(
            row["destination_classes_complete"] for row in rows
        ),
    }
    field_coverage = Counter()
    for row in rows:
        field_coverage.update(row["syscall_field_coverage"])
    totals["syscall_field_coverage"] = dict(sorted(field_coverage.items()))
    if args.expected_cases is not None and totals["cases"] != args.expected_cases:
        raise SystemExit(
            f"expected {args.expected_cases} cases, found {totals['cases']}"
        )
    if (
        args.expected_fst_files is not None
        and totals["fst_files"] != args.expected_fst_files
    ):
        raise SystemExit(
            f"expected {args.expected_fst_files} FST files, "
            f"found {totals['fst_files']}"
        )
    payload = {
        "schema": "fastsim-functional-warmup-matrix-audit-v1",
        "matrices": [str(path.resolve()) for path in args.matrix],
        "totals": totals,
        "cases": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps(totals, sort_keys=True))
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
