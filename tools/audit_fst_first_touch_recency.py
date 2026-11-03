#!/usr/bin/env python3
"""Audit measurement-first virtual-page touches by record recency.

This is a trace-only diagnostic.  It never uses the oracle to select pages;
the oracle page-fault count is joined after the FST-derived histograms so a
fixed startup-window heuristic can be evaluated without workload labels.
"""

from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path

import numpy as np


HEADER = struct.Struct("<8sIIIIQQ4Q")
HEADER_BYTES = 72
RECORD_BYTES = 64
MAGIC = b"FSTRC01\0"
MEMORY_FLAGS = (1 << 1) | (1 << 2) | (1 << 3)
WRITE_FLAGS = (1 << 2) | (1 << 3)
VIRTUAL_PAGE_TOKEN_FLAG = 1 << 15
TOKEN_MASK = (1 << 31) - 1
DEFAULT_BOUNDS = (
    256,
    1024,
    4096,
    16384,
    65536,
    262144,
    1048576,
    4194304,
    16777216,
    (1 << 64) - 1,
)
RECORD_DTYPE = np.dtype(
    {
        "names": ["flags", "reserved"],
        "formats": ["<u2", "<u4"],
        "offsets": [50, 60],
        "itemsize": RECORD_BYTES,
    }
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--audit",
        type=Path,
        required=True,
        help="audit_functional_warmup_matrix.py JSON output",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--bound",
        type=int,
        action="append",
        default=[],
        help="Inclusive per-core measurement-record bound; repeatable.",
    )
    return parser.parse_args()


def read_header(path: Path) -> tuple[int, int]:
    with path.open("rb") as source:
        raw = source.read(HEADER_BYTES)
    if len(raw) != HEADER_BYTES:
        raise ValueError(f"short FST header: {path}")
    values = HEADER.unpack(raw)
    magic, version, header_size, record_size, core_id, records = values[:6]
    if (
        magic != MAGIC
        or version != 7
        or header_size != HEADER_BYTES
        or record_size != RECORD_BYTES
    ):
        raise ValueError(f"unsupported canonical FST v7: {path}")
    return int(core_id), int(records)


def core_histogram(
    path: Path,
    warmup_records: int,
    measurement_records: int,
    bounds: tuple[int, ...],
) -> dict:
    core_id, records = read_header(path)
    end = warmup_records + measurement_records
    if warmup_records < 0 or measurement_records <= 0 or end > records:
        raise ValueError(f"invalid manifest record slice for {path}")
    trace = np.memmap(
        path,
        dtype=RECORD_DTYPE,
        mode="r",
        offset=HEADER_BYTES,
        shape=(records,),
    )

    def memory_tokens(rows: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        flags = np.asarray(rows["flags"])
        valid = (
            ((flags & MEMORY_FLAGS) != 0)
            & ((flags & VIRTUAL_PAGE_TOKEN_FLAG) != 0)
        )
        return (
            np.asarray(rows["reserved"])[valid] & TOKEN_MASK,
            flags[valid],
        )

    warm_tokens, _ = memory_tokens(trace[:warmup_records])
    warm_unique = np.unique(warm_tokens)
    measurement = trace[warmup_records:end]
    measurement_tokens, measurement_flags = memory_tokens(measurement)
    if measurement_tokens.size:
        unique_tokens, first_memory_indices = np.unique(
            measurement_tokens, return_index=True
        )
        unseen = ~np.isin(unique_tokens, warm_unique, assume_unique=True)
        first_memory_indices = first_memory_indices[unseen]
        first_flags = measurement_flags[first_memory_indices]

        # np.unique above indexes the compacted memory stream. Recover the
        # original measurement-record ordinal for the fixed startup windows.
        all_flags = np.asarray(measurement["flags"])
        memory_mask = (
            ((all_flags & MEMORY_FLAGS) != 0)
            & ((all_flags & VIRTUAL_PAGE_TOKEN_FLAG) != 0)
        )
        memory_record_indices = np.flatnonzero(memory_mask)
        first_record_positions = memory_record_indices[first_memory_indices] + 1
        writes = (first_flags & WRITE_FLAGS) != 0
    else:
        first_record_positions = np.empty(0, dtype=np.uint64)
        writes = np.empty(0, dtype=bool)

    write_positions = first_record_positions[writes]
    read_positions = first_record_positions[~writes]
    del trace
    return {
        "core_id": core_id,
        "path": str(path.resolve()),
        "warmup_records": warmup_records,
        "measurement_records": measurement_records,
        "warmup_virtual_pages": int(warm_unique.size),
        "measurement_first_touch_pages": int(first_record_positions.size),
        "measurement_first_touch_reads": int(read_positions.size),
        "measurement_first_touch_writes": int(write_positions.size),
        "cumulative_first_touch_reads": [
            int(np.count_nonzero(read_positions <= bound)) for bound in bounds
        ],
        "cumulative_first_touch_writes": [
            int(np.count_nonzero(write_positions <= bound)) for bound in bounds
        ],
    }


def load_manifest(result_dir: Path) -> list[tuple[Path, int, int]]:
    manifest = result_dir / "tao_trace" / "manifest.txt"
    rows = []
    for line in manifest.read_text().splitlines():
        fields = line.split()
        if not fields:
            continue
        if len(fields) != 8 or fields[1] != "fastsim-binary-warmup-slice":
            raise ValueError(f"unsupported formal manifest row: {line}")
        path = Path(fields[2])
        if not path.is_absolute():
            path = manifest.parent / path
        rows.append((path, int(fields[6]), int(fields[7])))
    return rows


def main() -> int:
    args = parse_args()
    audit = json.loads(args.audit.read_text())
    bounds = tuple(sorted(set(args.bound))) if args.bound else DEFAULT_BOUNDS
    if not bounds or any(value <= 0 for value in bounds):
        raise SystemExit("record bounds must be positive")
    cases = []
    for source_case in audit.get("cases", []):
        result_dir = Path(source_case["result_dir"])
        cores = [
            core_histogram(path, warmup, measurement, bounds)
            for path, warmup, measurement in load_manifest(result_dir)
        ]
        oracle_path = result_dir / "oracle" / "kernel_events.json"
        oracle = json.loads(oracle_path.read_text())
        reference = int(oracle["aggregate"]["event_counts"]["page_fault"])
        cumulative_writes = [
            sum(core["cumulative_first_touch_writes"][index] for core in cores)
            for index in range(len(bounds))
        ]
        cases.append(
            {
                "workload": source_case["workload"],
                "cores": int(source_case["cores"]),
                "result_dir": str(result_dir.resolve()),
                "oracle_page_fault_events": reference,
                "measurement_first_touch_pages": sum(
                    core["measurement_first_touch_pages"] for core in cores
                ),
                "measurement_first_touch_reads": sum(
                    core["measurement_first_touch_reads"] for core in cores
                ),
                "measurement_first_touch_writes": sum(
                    core["measurement_first_touch_writes"] for core in cores
                ),
                "cumulative_first_touch_writes": cumulative_writes,
                "absolute_error_by_bound": [
                    abs(value - reference) for value in cumulative_writes
                ],
                "cores_detail": cores,
            }
        )

    payload = {
        "schema": "fastsim-fst-v7-first-touch-recency-audit-v1",
        "source_audit": str(args.audit.resolve()),
        "record_bounds": list(bounds),
        "cases": cases,
        "aggregate": {
            "cases": len(cases),
            "reference_page_fault_events": sum(
                case["oracle_page_fault_events"] for case in cases
            ),
            "cumulative_first_touch_writes": [
                sum(case["cumulative_first_touch_writes"][index] for case in cases)
                for index in range(len(bounds))
            ],
            "absolute_error_by_bound": [
                sum(case["absolute_error_by_bound"][index] for case in cases)
                for index in range(len(bounds))
            ],
        },
    }
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text)
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
