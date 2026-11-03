#!/usr/bin/env python3
"""Validate FST v7 virtual-page and address-space companion maps."""

from __future__ import annotations

import argparse
import json
import struct
from bisect import bisect_right
from pathlib import Path

import numpy as np


FST_HEADER = struct.Struct("<8sIIIIQQ4Q")
VMAP_HEADER = struct.Struct("<8sIIIIQQII")
VMAP_ENTRY_V1 = struct.Struct("<IIQQQ")
VMAP_ENTRY_V2 = struct.Struct("<IIQQQBBBB4B8Q8Q")
ASMAP_HEADER = struct.Struct("<8sIIIIQQQ")
ASMAP_ENTRY = struct.Struct("<QQ")
FST_MAGIC = b"FSTRC01\0"
VMAP_MAGIC_V1 = b"FSTVMP1\0"
VMAP_MAGIC_V2 = b"FSTVMP2\0"
ASMAP_MAGIC = b"FSTASM1\0"
FST_HEADER_BYTES = 72
FST_RECORD_BYTES = 64
FEATURE_VIRTUAL_PAGE_TOKENS = 1 << 0
VIRTUAL_PAGE_TOKEN_FLAG = 1 << 15
TOKEN_MASK = (1 << 31) - 1
VMAP_PHYSICAL_VALID = 1 << 0
VMAP_INITIAL_PTE_STATE_VALID = 1 << 1
VMAP_INITIAL_PTE_PRESENT = 1 << 2
VMAP_ROI_ENTRY_PAGE_STATE_VALID = 1 << 3
VMAP_ROI_ENTRY_PAGE_PRESENT = 1 << 4
VMAP_ROI_ENTRY_INFLIGHT_PAGE_FAULT = 1 << 5
VMAP_INITIAL_PTE_PATH_VALID = 1 << 6
VMAP_ROI_ENTRY_PTE_PATH_VALID = 1 << 7
VMAP_KNOWN_FLAGS_V1 = (
    VMAP_PHYSICAL_VALID
    | VMAP_INITIAL_PTE_STATE_VALID
    | VMAP_INITIAL_PTE_PRESENT
    | VMAP_ROI_ENTRY_PAGE_STATE_VALID
    | VMAP_ROI_ENTRY_PAGE_PRESENT
    | VMAP_ROI_ENTRY_INFLIGHT_PAGE_FAULT
)
VMAP_KNOWN_FLAGS_V2 = (
    VMAP_KNOWN_FLAGS_V1
    | VMAP_INITIAL_PTE_PATH_VALID
    | VMAP_ROI_ENTRY_PTE_PATH_VALID
)
RECORD_DTYPE = np.dtype(
    {
        "names": ["address", "flags", "op_class", "reserved"],
        "formats": ["<u8", "<u2", "<i2", "<u4"],
        "offsets": [8, 50, 52, 60],
        "itemsize": FST_RECORD_BYTES,
    }
)


def valid_pte_path(
    valid: bool,
    levels: int,
    page_size_bits: int,
    addresses: tuple[int, ...],
) -> bool:
    if len(addresses) != 8:
        return False
    if not valid:
        return levels == 0 and page_size_bits == 0 and not any(addresses)
    if levels < 1 or levels > 8 or page_size_bits not in (0, 12, 21, 30):
        return False
    return all(
        (address & 7) == 0 if level < levels else address == 0
        for level, address in enumerate(addresses)
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


def read_address_space_map(
    path: Path, core_id: int, record_count: int
) -> tuple[Path, list[tuple[int, int]]] | None:
    map_path = Path(str(path) + ".asmap")
    if not map_path.is_file():
        return None
    with map_path.open("rb") as source:
        raw_header = source.read(ASMAP_HEADER.size)
        if len(raw_header) != ASMAP_HEADER.size:
            raise ValueError(f"short address-space map: {map_path}")
        (
            magic,
            version,
            header_size,
            entry_size,
            map_core,
            source_records,
            entry_count,
            reserved,
        ) = ASMAP_HEADER.unpack(raw_header)
        if (
            magic != ASMAP_MAGIC
            or version != 1
            or header_size != ASMAP_HEADER.size
            or entry_size != ASMAP_ENTRY.size
            or map_core != core_id
            or source_records != record_count
            or entry_count == 0
            or reserved != 0
            or record_count == 0
            or map_path.stat().st_size
            != ASMAP_HEADER.size + entry_count * ASMAP_ENTRY.size
        ):
            raise ValueError(f"invalid address-space map header: {map_path}")
        transitions: list[tuple[int, int]] = []
        for index in range(entry_count):
            raw = source.read(ASMAP_ENTRY.size)
            if len(raw) != ASMAP_ENTRY.size:
                raise ValueError(f"truncated address-space map: {map_path}")
            ordinal, address_space_id = ASMAP_ENTRY.unpack(raw)
            if (
                address_space_id == 0
                or ordinal >= record_count
                or (index == 0 and ordinal != 0)
                or (
                    transitions
                    and (
                        ordinal <= transitions[-1][0]
                        or address_space_id == transitions[-1][1]
                    )
                )
            ):
                raise ValueError(f"invalid address-space map entry: {map_path}")
            transitions.append((int(ordinal), int(address_space_id)))
    return map_path, transitions


def read_warmup_record_count(
    path: Path, core_id: int, record_count: int
) -> int | None:
    boundary_path = path.parent / f"functional-boundary-core{core_id}.json"
    boundary = None
    if boundary_path.is_file():
        boundary = json.loads(boundary_path.read_text())
    else:
        metadata_path = path.parent / "trace.json"
        if metadata_path.is_file():
            metadata = json.loads(metadata_path.read_text())
            boundary = metadata.get("functional_boundaries", {}).get(
                str(core_id)
            )
    if boundary is None:
        return None
    warmup_records = int(boundary.get("warmup_records", -1))
    measurement_records = int(boundary.get("measurement_records", -1))
    if (
        boundary.get("schema") != "tcsim-functional-boundary-v1"
        or warmup_records < 0
        or measurement_records < 0
        or warmup_records + measurement_records != record_count
    ):
        raise ValueError(f"invalid functional boundary for {path}")
    return warmup_records


def summarize_path_record_coverage(
    records: np.ndarray,
    start: int,
    end: int,
    known_tokens: set[int],
    prefix: str,
) -> dict[str, int]:
    segment = records[start:end]
    tokenized = (segment["flags"] & VIRTUAL_PAGE_TOKEN_FLAG) != 0
    tokens = (
        segment["reserved"][tokenized].astype(np.uint64) & TOKEN_MASK
    )
    kernel = segment["op_class"][tokenized] < -1
    known_values = np.fromiter(known_tokens, dtype=np.uint64)
    known = np.isin(tokens, known_values)
    result: dict[str, int] = {}
    for domain, selected in (
        ("all", np.ones(tokens.shape, dtype=bool)),
        ("user", ~kernel),
        ("kernel", kernel),
    ):
        domain_tokens = tokens[selected]
        domain_known = known[selected]
        unique_tokens = {int(value) for value in np.unique(domain_tokens)}
        result[f"{prefix}_{domain}_token_records"] = int(selected.sum())
        result[f"{prefix}_{domain}_path_known_records"] = int(
            domain_known.sum()
        )
        result[f"{prefix}_{domain}_path_unknown_records"] = int(
            selected.sum() - domain_known.sum()
        )
        result[f"{prefix}_{domain}_unique_pages"] = len(unique_tokens)
        result[f"{prefix}_{domain}_path_known_unique_pages"] = len(
            unique_tokens & known_tokens
        )
    return result


def audit_file(path: Path, allow_missing: bool) -> dict:
    path = path.resolve()
    core_id, record_count, features = read_fst_header(path)
    tokenized = bool(features & FEATURE_VIRTUAL_PAGE_TOKENS)
    address_space_map = read_address_space_map(path, core_id, record_count)
    if address_space_map is None:
        asmap_path = None
        transitions = [(0, 0)]
    else:
        asmap_path, transitions = address_space_map
    address_space_summary = {
        "address_space_map_present": asmap_path is not None,
        "address_space_map_path": (
            str(asmap_path) if asmap_path is not None else None
        ),
        "address_space_map_bytes": (
            asmap_path.stat().st_size if asmap_path is not None else 0
        ),
        "address_space_transitions": (
            len(transitions) if asmap_path is not None else 0
        ),
        "address_space_switches": (
            len(transitions) - 1 if asmap_path is not None else 0
        ),
        "address_spaces": (
            len({row[1] for row in transitions})
            if asmap_path is not None else 0
        ),
    }
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
            **address_space_summary,
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
        map_v1 = magic == VMAP_MAGIC_V1 and version == 1
        map_v2 = magic == VMAP_MAGIC_V2 and version == 2
        entry_struct = VMAP_ENTRY_V2 if map_v2 else VMAP_ENTRY_V1
        if (
            not (map_v1 or map_v2)
            or header_size != VMAP_HEADER.size
            or entry_size != entry_struct.size
            or map_core != core_id
            or source_records != record_count
            or entry_count == 0
            or page_bits != 12
            or reserved != 0
            or map_path.stat().st_size
            != VMAP_HEADER.size + entry_count * entry_struct.size
        ):
            raise ValueError(f"invalid virtual-page map header: {map_path}")
        mappings: dict[
            int,
            tuple[
                int,
                int,
                int,
                int,
                int,
                int,
                tuple[int, ...],
                int,
                int,
                tuple[int, ...],
            ],
        ] = {}
        for _ in range(entry_count):
            raw = source.read(entry_struct.size)
            if len(raw) != entry_struct.size:
                raise ValueError(f"truncated virtual-page map: {map_path}")
            values = entry_struct.unpack(raw)
            token, flags, first_record, vpage, ppage = values[:5]
            if map_v2:
                (
                    initial_levels,
                    initial_page_size_bits,
                    roi_entry_levels,
                    roi_entry_page_size_bits,
                ) = values[5:9]
                reserved_bytes = values[9:13]
                initial_addresses = tuple(values[13:21])
                roi_entry_addresses = tuple(values[21:29])
            else:
                initial_levels = 0
                initial_page_size_bits = 0
                roi_entry_levels = 0
                roi_entry_page_size_bits = 0
                reserved_bytes = (0, 0, 0, 0)
                initial_addresses = (0,) * 8
                roi_entry_addresses = (0,) * 8
            known_flags = (
                VMAP_KNOWN_FLAGS_V2 if map_v2 else VMAP_KNOWN_FLAGS_V1
            )
            if (
                token == 0
                or token > TOKEN_MASK
                or flags & ~known_flags
                or (
                    flags & VMAP_INITIAL_PTE_PRESENT
                    and not flags & VMAP_INITIAL_PTE_STATE_VALID
                )
                or (
                    flags & VMAP_ROI_ENTRY_PAGE_PRESENT
                    and not flags & VMAP_ROI_ENTRY_PAGE_STATE_VALID
                )
                or any(reserved_bytes)
                or not valid_pte_path(
                    bool(flags & VMAP_INITIAL_PTE_PATH_VALID),
                    initial_levels,
                    initial_page_size_bits,
                    initial_addresses,
                )
                or not valid_pte_path(
                    bool(flags & VMAP_ROI_ENTRY_PTE_PATH_VALID),
                    roi_entry_levels,
                    roi_entry_page_size_bits,
                    roi_entry_addresses,
                )
                or first_record >= record_count
                or token in mappings
            ):
                raise ValueError(f"invalid virtual-page map entry: {map_path}")
            mappings[token] = (
                flags,
                first_record,
                vpage,
                ppage,
                initial_levels,
                initial_page_size_bits,
                initial_addresses,
                roi_entry_levels,
                roi_entry_page_size_bits,
                roi_entry_addresses,
            )

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
    token_address_spaces: dict[int, int] = {}
    for index, (start, address_space_id) in enumerate(transitions):
        end = (
            transitions[index + 1][0]
            if index + 1 < len(transitions) else record_count
        )
        segment = records[start:end]
        segment_valid = (
            segment["flags"] & VIRTUAL_PAGE_TOKEN_FLAG
        ) != 0
        for token in np.unique(
            segment["reserved"][segment_valid] & TOKEN_MASK
        ):
            token = int(token)
            prior = token_address_spaces.get(token)
            if prior is not None and prior != address_space_id:
                raise ValueError(
                    f"virtual-page token {token} crosses address spaces "
                    f"{prior} and {address_space_id}: {path}"
                )
            token_address_spaces[token] = address_space_id

    mapping_rows: dict[int, tuple] = {}
    transition_ordinals = [row[0] for row in transitions]
    for token, mapping in mappings.items():
        flags, first_record, vpage, ppage = mapping[:4]
        transition_index = bisect_right(
            transition_ordinals, first_record
        ) - 1
        address_space_id = transitions[transition_index][1]
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
        if token_address_spaces.get(token) != address_space_id:
            raise ValueError(
                f"token/address-space identity mismatch token={token}: {path}"
            )
        mapping_rows[token] = (
            flags,
            first_record,
            vpage,
            ppage,
            address_space_id,
            *mapping[4:],
        )
    mappings = mapping_rows
    warmup_records = read_warmup_record_count(path, core_id, record_count)
    path_record_coverage: dict[str, int | bool] = {
        "functional_boundary_present": warmup_records is not None,
    }
    if warmup_records is not None:
        initial_path_tokens = {
            token
            for token, row in mappings.items()
            if row[0] & VMAP_INITIAL_PTE_PATH_VALID
        }
        roi_entry_path_tokens = {
            token
            for token, row in mappings.items()
            if row[0] & VMAP_ROI_ENTRY_PTE_PATH_VALID
        }
        path_record_coverage.update(
            summarize_path_record_coverage(
                records,
                0,
                warmup_records,
                initial_path_tokens,
                "warmup_initial",
            )
        )
        path_record_coverage.update(
            summarize_path_record_coverage(
                records,
                warmup_records,
                record_count,
                roi_entry_path_tokens,
                "measurement_roi_entry",
            )
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
    roi_entry_page_known = sum(
        bool(row[0] & VMAP_ROI_ENTRY_PAGE_STATE_VALID)
        for row in mappings.values()
    )
    roi_entry_page_present = sum(
        bool(row[0] & VMAP_ROI_ENTRY_PAGE_PRESENT)
        for row in mappings.values()
    )
    roi_entry_inflight_page_fault = sum(
        bool(row[0] & VMAP_ROI_ENTRY_INFLIGHT_PAGE_FAULT)
        for row in mappings.values()
    )
    initial_pte_path_known = sum(
        bool(row[0] & VMAP_INITIAL_PTE_PATH_VALID)
        for row in mappings.values()
    )
    roi_entry_pte_path_known = sum(
        bool(row[0] & VMAP_ROI_ENTRY_PTE_PATH_VALID)
        for row in mappings.values()
    )
    return {
        "path": str(path),
        "core_id": core_id,
        "records": record_count,
        "tokenized": True,
        "map_present": True,
        "map_path": str(map_path),
        "map_version": int(version),
        "map_bytes": map_path.stat().st_size,
        "entries": len(mappings),
        "virtual_pages": len({row[2] for row in mappings.values()}),
        "physical_pages": len({row[3] for row in mappings.values()}),
        **address_space_summary,
        "initial_pte_known": initial_pte_known,
        "initial_pte_present": initial_pte_present,
        "initial_pte_nonpresent": initial_pte_known - initial_pte_present,
        "initial_pte_unknown": len(mappings) - initial_pte_known,
        "roi_entry_page_known": roi_entry_page_known,
        "roi_entry_page_present": roi_entry_page_present,
        "roi_entry_page_nonpresent": (
            roi_entry_page_known - roi_entry_page_present
        ),
        "roi_entry_page_unknown": len(mappings) - roi_entry_page_known,
        "roi_entry_inflight_page_fault": (
            roi_entry_inflight_page_fault
        ),
        "initial_pte_path_known": initial_pte_path_known,
        "initial_pte_path_unknown": len(mappings) - initial_pte_path_known,
        "initial_pte_path_unknown_examples": [
            {
                "token": token,
                "first_record": row[1],
                "virtual_page": row[2],
                "address_space_id": row[4],
            }
            for token, row in mappings.items()
            if not row[0] & VMAP_INITIAL_PTE_PATH_VALID
        ][:16],
        "initial_pte_path_four_level": sum(
            row[5] == 4 for row in mappings.values()
        ),
        "roi_entry_pte_path_known": roi_entry_pte_path_known,
        "roi_entry_pte_path_unknown": (
            len(mappings) - roi_entry_pte_path_known
        ),
        "roi_entry_pte_path_unknown_examples": [
            {
                "token": token,
                "first_record": row[1],
                "virtual_page": row[2],
                "address_space_id": row[4],
            }
            for token, row in mappings.items()
            if not row[0] & VMAP_ROI_ENTRY_PTE_PATH_VALID
        ][:16],
        "roi_entry_pte_path_four_level": sum(
            row[8] == 4 for row in mappings.values()
        ),
        "pte_physical_lines": len(
            {
                address >> 6
                for row in mappings.values()
                for levels, addresses in ((row[5], row[7]), (row[8], row[10]))
                for address in addresses[:levels]
            }
        ),
        **path_record_coverage,
        "_initial_pte_states": {
            (row[4], row[2]): (
                bool(row[0] & VMAP_INITIAL_PTE_PRESENT)
                if row[0] & VMAP_INITIAL_PTE_STATE_VALID
                else None
            )
            for row in mappings.values()
        },
        "_roi_entry_page_states": {
            (row[4], row[2]): (
                bool(row[0] & VMAP_ROI_ENTRY_PAGE_PRESENT)
                if row[0] & VMAP_ROI_ENTRY_PAGE_STATE_VALID
                else None
            )
            for row in mappings.values()
        },
        "_initial_pte_paths": {
            (row[4], row[2]): (row[5], row[6], row[7])
            for row in mappings.values()
            if row[0] & VMAP_INITIAL_PTE_PATH_VALID
        },
        "_roi_entry_pte_paths": {
            (row[4], row[2]): (row[8], row[9], row[10])
            for row in mappings.values()
            if row[0] & VMAP_ROI_ENTRY_PTE_PATH_VALID
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
    process_pages: set[tuple[int, int]] = set()
    process_known: dict[tuple[int, int], bool] = {}
    process_roi_entry_known: dict[tuple[int, int], bool] = {}
    process_initial_pte_paths: dict[tuple[int, int], tuple] = {}
    process_roi_entry_pte_paths: dict[tuple[int, int], tuple] = {}
    for row in files:
        states = row.pop("_initial_pte_states", {})
        roi_entry_states = row.pop("_roi_entry_page_states", {})
        initial_paths = row.pop("_initial_pte_paths", {})
        roi_entry_paths = row.pop("_roi_entry_pte_paths", {})
        process_pages.update(states)
        process_pages.update(initial_paths)
        process_pages.update(roi_entry_paths)
        for page_identity, state in states.items():
            if state is None:
                continue
            prior = process_known.get(page_identity)
            if prior is not None and prior != state:
                raise ValueError(
                    "conflicting initial PTE state across streams for "
                    f"address-space/page {page_identity}"
                )
            process_known[page_identity] = state
        for page_identity, state in roi_entry_states.items():
            if state is None:
                continue
            prior = process_roi_entry_known.get(page_identity)
            if prior is not None and prior != state:
                raise ValueError(
                    "conflicting ROI-entry page state across streams for "
                    f"address-space/page {page_identity}"
                )
            process_roi_entry_known[page_identity] = state
        for page_identity, path in initial_paths.items():
            prior = process_initial_pte_paths.get(page_identity)
            if prior is not None and prior != path:
                raise ValueError(
                    "conflicting initial PTE path across streams for "
                    f"address-space/page {page_identity}"
                )
            process_initial_pte_paths[page_identity] = path
        for page_identity, path in roi_entry_paths.items():
            prior = process_roi_entry_pte_paths.get(page_identity)
            if prior is not None and prior != path:
                raise ValueError(
                    "conflicting ROI-entry PTE path across streams for "
                    f"address-space/page {page_identity}"
                )
            process_roi_entry_pte_paths[page_identity] = path
    coverage_prefixes = ("warmup_initial_", "measurement_roi_entry_")
    coverage_fields = sorted(
        {
            key
            for row in files
            for key in row
            if key.startswith(coverage_prefixes)
        }
    )
    coverage_totals = {
        key: sum(int(row.get(key, 0)) for row in files)
        for key in coverage_fields
    }
    payload = {
        "schema": "fastsim-fst-v7-virtual-page-map-audit-v8",
        "valid": True,
        "totals": {
            "files": len(files),
            "mapped_files": sum(row["map_present"] for row in files),
            "records": sum(row["records"] for row in files),
            "entries": sum(row["entries"] for row in files),
            "map_bytes": sum(row.get("map_bytes", 0) for row in files),
            "address_space_mapped_files": sum(
                row.get("address_space_map_present", False)
                for row in files
            ),
            "address_space_map_bytes": sum(
                row.get("address_space_map_bytes", 0) for row in files
            ),
            "address_space_switches": sum(
                row.get("address_space_switches", 0) for row in files
            ),
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
            "roi_entry_page_known": sum(
                row.get("roi_entry_page_known", 0) for row in files
            ),
            "roi_entry_page_present": sum(
                row.get("roi_entry_page_present", 0) for row in files
            ),
            "roi_entry_page_nonpresent": sum(
                row.get("roi_entry_page_nonpresent", 0) for row in files
            ),
            "roi_entry_page_unknown": sum(
                row.get("roi_entry_page_unknown", 0) for row in files
            ),
            "roi_entry_inflight_page_fault": sum(
                row.get("roi_entry_inflight_page_fault", 0)
                for row in files
            ),
            "initial_pte_path_known": sum(
                row.get("initial_pte_path_known", 0) for row in files
            ),
            "initial_pte_path_unknown": sum(
                row.get("initial_pte_path_unknown", 0) for row in files
            ),
            "initial_pte_path_four_level": sum(
                row.get("initial_pte_path_four_level", 0)
                for row in files
            ),
            "roi_entry_pte_path_known": sum(
                row.get("roi_entry_pte_path_known", 0) for row in files
            ),
            "roi_entry_pte_path_unknown": sum(
                row.get("roi_entry_pte_path_unknown", 0)
                for row in files
            ),
            "roi_entry_pte_path_four_level": sum(
                row.get("roi_entry_pte_path_four_level", 0)
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
            "process_roi_entry_page_known": len(
                process_roi_entry_known
            ),
            "process_roi_entry_page_present": sum(
                process_roi_entry_known.values()
            ),
            "process_roi_entry_page_nonpresent": sum(
                not state for state in process_roi_entry_known.values()
            ),
            "process_roi_entry_page_unknown": (
                len(process_pages) - len(process_roi_entry_known)
            ),
            "process_initial_pte_path_known": len(
                process_initial_pte_paths
            ),
            "process_initial_pte_path_unknown": (
                len(process_pages) - len(process_initial_pte_paths)
            ),
            "process_roi_entry_pte_path_known": len(
                process_roi_entry_pte_paths
            ),
            "process_roi_entry_pte_path_unknown": (
                len(process_pages) - len(process_roi_entry_pte_paths)
            ),
            **coverage_totals,
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
