#!/usr/bin/env python3
"""Validate FST v7 token-to-virtual-page companion maps."""

from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path

import numpy as np


FST_HEADER = struct.Struct("<8sIIIIQQ4Q")
VMAP_HEADER = struct.Struct("<8sIIIIQQII")
VMAP_ENTRY = struct.Struct("<IIQQQ")
FST_MAGIC = b"FSTRC01\0"
VMAP_MAGIC = b"FSTVMP1\0"
FST_HEADER_BYTES = 72
FST_RECORD_BYTES = 64
FEATURE_VIRTUAL_PAGE_TOKENS = 1 << 0
VIRTUAL_PAGE_TOKEN_FLAG = 1 << 15
TOKEN_MASK = (1 << 31) - 1
VMAP_PHYSICAL_VALID = 1 << 0
VMAP_INITIAL_PTE_STATE_VALID = 1 << 1
VMAP_INITIAL_PTE_PRESENT = 1 << 2
VMAP_MEASUREMENT_PTE_STATE_VALID = 1 << 3
VMAP_MEASUREMENT_PTE_PRESENT = 1 << 4
VMAP_MEASUREMENT_BOUNDARY_INFLIGHT_FAULT = 1 << 5
VMAP_KNOWN_FLAGS = (
    VMAP_PHYSICAL_VALID
    | VMAP_INITIAL_PTE_STATE_VALID
    | VMAP_INITIAL_PTE_PRESENT
    | VMAP_MEASUREMENT_PTE_STATE_VALID
    | VMAP_MEASUREMENT_PTE_PRESENT
    | VMAP_MEASUREMENT_BOUNDARY_INFLIGHT_FAULT
)
RECORD_DTYPE = np.dtype(
    {
        "names": ["address", "flags", "reserved"],
        "formats": ["<u8", "<u2", "<u4"],
        "offsets": [8, 50, 60],
        "itemsize": FST_RECORD_BYTES,
    }
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fst", action="append", type=Path, default=[])
    parser.add_argument("--trace-dir", action="append", type=Path, default=[])
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--allow-missing-map",
        action="store_true",
        help="Report tokenized legacy traces without a map instead of failing.",
    )
    return parser.parse_args()


def read_fst_header(path: Path) -> tuple[int, int, int]:
    with path.open("rb") as source:
        raw = source.read(FST_HEADER.size)
    if len(raw) != FST_HEADER.size:
        raise ValueError(f"short FST header: {path}")
    values = FST_HEADER.unpack(raw)
    magic, version, header_size, record_size, core_id = values[:5]
    records, features = values[5:7]
    if (
        magic != FST_MAGIC
        or version != 7
        or header_size != FST_HEADER_BYTES
        or record_size != FST_RECORD_BYTES
    ):
        raise ValueError(f"unsupported FST: {path}")
    return int(core_id), int(records), int(features)


def audit_file(path: Path, allow_missing: bool) -> dict:
    path = path.resolve()
    core_id, record_count, features = read_fst_header(path)
    tokenized = bool(features & FEATURE_VIRTUAL_PAGE_TOKENS)
    map_path = Path(str(path) + ".vmap")
    if not map_path.is_file():
        if tokenized and not allow_missing:
            raise ValueError(f"tokenized FST lacks virtual-page map: {path}")
        return {
            "path": str(path),
            "core_id": core_id,
            "records": record_count,
            "tokenized": tokenized,
            "map_present": False,
            "entries": 0,
        }

    with map_path.open("rb") as source:
        raw_header = source.read(VMAP_HEADER.size)
        if len(raw_header) != VMAP_HEADER.size:
            raise ValueError(f"short virtual-page map: {map_path}")
        values = VMAP_HEADER.unpack(raw_header)
        (
            magic,
            version,
            header_size,
            entry_size,
            map_core,
            source_records,
            entry_count,
            page_bits,
            reserved,
        ) = values
        if (
            magic != VMAP_MAGIC
            or version != 1
            or header_size != VMAP_HEADER.size
            or entry_size != VMAP_ENTRY.size
            or map_core != core_id
            or source_records != record_count
            or entry_count == 0
            or page_bits != 12
            or reserved != 0
            or map_path.stat().st_size
            != VMAP_HEADER.size + entry_count * VMAP_ENTRY.size
        ):
            raise ValueError(f"invalid virtual-page map header: {map_path}")
        mappings: dict[int, tuple[int, int, int, int]] = {}
        for _ in range(entry_count):
            raw = source.read(VMAP_ENTRY.size)
            if len(raw) != VMAP_ENTRY.size:
                raise ValueError(f"truncated virtual-page map: {map_path}")
            token, flags, first_record, vpage, ppage = VMAP_ENTRY.unpack(raw)
            if (
                token == 0
                or token > TOKEN_MASK
                or flags & ~VMAP_KNOWN_FLAGS
                or (
                    flags & VMAP_INITIAL_PTE_PRESENT
                    and not flags & VMAP_INITIAL_PTE_STATE_VALID
                )
                or (
                    flags & VMAP_MEASUREMENT_PTE_PRESENT
                    and not flags & VMAP_MEASUREMENT_PTE_STATE_VALID
                )
                or first_record >= record_count
                or token in mappings
            ):
                raise ValueError(f"invalid virtual-page map entry: {map_path}")
            mappings[token] = (flags, first_record, vpage, ppage)

    if not tokenized:
        raise ValueError(f"map accompanies FST without token feature: {path}")
    records = np.memmap(
        path,
        dtype=RECORD_DTYPE,
        mode="r",
        offset=FST_HEADER_BYTES,
        shape=(record_count,),
    )
    valid = (records["flags"] & VIRTUAL_PAGE_TOKEN_FLAG) != 0
    tokens = records["reserved"][valid] & TOKEN_MASK
    used = {int(token) for token in np.unique(tokens)}
    if used != set(mappings):
        missing = sorted(used - set(mappings))[:16]
        unused = sorted(set(mappings) - used)[:16]
        raise ValueError(
            f"virtual-page token set mismatch {path}: "
            f"missing={missing} unused={unused}"
        )
    for token, (flags, first_record, _vpage, ppage) in mappings.items():
        first = records[first_record]
        if (
            not first["flags"] & VIRTUAL_PAGE_TOKEN_FLAG
            or int(first["reserved"] & TOKEN_MASK) != token
            or (
                flags & VMAP_PHYSICAL_VALID
                and (int(first["address"]) >> 12) != ppage
            )
        ):
            raise ValueError(
                f"first-record identity mismatch token={token}: {path}"
            )
    del records
    initial_pte_known = sum(
        bool(row[0] & VMAP_INITIAL_PTE_STATE_VALID)
        for row in mappings.values()
    )
    initial_pte_present = sum(
        bool(row[0] & VMAP_INITIAL_PTE_PRESENT)
        for row in mappings.values()
    )
    measurement_pte_known = sum(
        bool(row[0] & VMAP_MEASUREMENT_PTE_STATE_VALID)
        for row in mappings.values()
    )
    measurement_pte_present = sum(
        bool(row[0] & VMAP_MEASUREMENT_PTE_PRESENT)
        for row in mappings.values()
    )
    measurement_boundary_inflight_fault = sum(
        bool(row[0] & VMAP_MEASUREMENT_BOUNDARY_INFLIGHT_FAULT)
        for row in mappings.values()
    )
    return {
        "path": str(path),
        "core_id": core_id,
        "records": record_count,
        "tokenized": True,
        "map_present": True,
        "map_path": str(map_path),
        "map_bytes": map_path.stat().st_size,
        "entries": len(mappings),
        "virtual_pages": len({row[2] for row in mappings.values()}),
        "physical_pages": len({row[3] for row in mappings.values()}),
        "initial_pte_known": initial_pte_known,
        "initial_pte_present": initial_pte_present,
        "initial_pte_nonpresent": initial_pte_known - initial_pte_present,
        "initial_pte_unknown": len(mappings) - initial_pte_known,
        "measurement_pte_known": measurement_pte_known,
        "measurement_pte_present": measurement_pte_present,
        "measurement_pte_nonpresent": (
            measurement_pte_known - measurement_pte_present
        ),
        "measurement_pte_unknown": len(mappings) - measurement_pte_known,
        "measurement_boundary_inflight_fault": (
            measurement_boundary_inflight_fault
        ),
        "_initial_pte_states": {
            row[2]: (
                bool(row[0] & VMAP_INITIAL_PTE_PRESENT)
                if row[0] & VMAP_INITIAL_PTE_STATE_VALID
                else None
            )
            for row in mappings.values()
        },
        "_measurement_pte_states": {
            row[2]: (
                bool(row[0] & VMAP_MEASUREMENT_PTE_PRESENT)
                if row[0] & VMAP_MEASUREMENT_PTE_STATE_VALID
                else None
            )
            for row in mappings.values()
        },
    }


def main() -> int:
    args = parse_args()
    paths = list(args.fst)
    for root in args.trace_dir:
        paths.extend(root.rglob("*.fst"))
    paths = sorted({path.resolve() for path in paths})
    if not paths:
        raise SystemExit("at least one FST input is required")
    files = [audit_file(path, args.allow_missing_map) for path in paths]
    process_pages: set[int] = set()
    process_known: dict[int, bool] = {}
    process_measurement_known: dict[int, bool] = {}
    for row in files:
        states = row.pop("_initial_pte_states", {})
        measurement_states = row.pop("_measurement_pte_states", {})
        process_pages.update(states)
        for virtual_page, state in states.items():
            if state is None:
                continue
            prior = process_known.get(virtual_page)
            if prior is not None and prior != state:
                raise ValueError(
                    "conflicting initial PTE state across streams for "
                    f"virtual page {virtual_page}"
                )
            process_known[virtual_page] = state
        for virtual_page, state in measurement_states.items():
            if state is None:
                continue
            prior = process_measurement_known.get(virtual_page)
            if prior is not None and prior != state:
                raise ValueError(
                    "conflicting measurement PTE state across streams for "
                    f"virtual page {virtual_page}"
                )
            process_measurement_known[virtual_page] = state
    payload = {
        "schema": "fastsim-fst-v7-virtual-page-map-audit-v4",
        "valid": True,
        "totals": {
            "files": len(files),
            "mapped_files": sum(row["map_present"] for row in files),
            "records": sum(row["records"] for row in files),
            "entries": sum(row["entries"] for row in files),
            "map_bytes": sum(row.get("map_bytes", 0) for row in files),
            "initial_pte_known": sum(
                row.get("initial_pte_known", 0) for row in files
            ),
            "initial_pte_present": sum(
                row.get("initial_pte_present", 0) for row in files
            ),
            "initial_pte_nonpresent": sum(
                row.get("initial_pte_nonpresent", 0) for row in files
            ),
            "initial_pte_unknown": sum(
                row.get("initial_pte_unknown", 0) for row in files
            ),
            "measurement_pte_known": sum(
                row.get("measurement_pte_known", 0) for row in files
            ),
            "measurement_pte_present": sum(
                row.get("measurement_pte_present", 0) for row in files
            ),
            "measurement_pte_nonpresent": sum(
                row.get("measurement_pte_nonpresent", 0) for row in files
            ),
            "measurement_pte_unknown": sum(
                row.get("measurement_pte_unknown", 0) for row in files
            ),
            "measurement_boundary_inflight_fault": sum(
                row.get("measurement_boundary_inflight_fault", 0)
                for row in files
            ),
            "process_virtual_pages": len(process_pages),
            "process_initial_pte_known": len(process_known),
            "process_initial_pte_present": sum(process_known.values()),
            "process_initial_pte_nonpresent": sum(
                not state for state in process_known.values()
            ),
            "process_initial_pte_unknown": (
                len(process_pages) - len(process_known)
            ),
            "process_measurement_pte_known": len(
                process_measurement_known
            ),
            "process_measurement_pte_present": sum(
                process_measurement_known.values()
            ),
            "process_measurement_pte_nonpresent": sum(
                not state for state in process_measurement_known.values()
            ),
            "process_measurement_pte_unknown": (
                len(process_pages) - len(process_measurement_known)
            ),
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
