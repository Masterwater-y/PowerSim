#!/usr/bin/env python3
"""Strictly audit functional ``.fst.ifmap`` instruction-page companions."""

from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path


FST_HEADER = struct.Struct("<8sIIIIQQQQQQ")
ASMAP_HEADER = struct.Struct("<8sIIIIQQQ")
ASMAP_ENTRY = struct.Struct("<QQ")
IFMAP_HEADER = struct.Struct("<8sIIIIQQII")
IFMAP_ENTRY = struct.Struct("<QQQQ")
PC = struct.Struct("<Q")


def read_header(source, layout: struct.Struct, path: Path):
    payload = source.read(layout.size)
    if len(payload) != layout.size:
        raise ValueError(f"truncated header: {path}")
    return layout.unpack(payload)


def read_fst_header(path: Path) -> tuple[int, int]:
    with path.open("rb") as source:
        header = read_header(source, FST_HEADER, path)
    magic, version, header_size, record_size, core_id, records, *_ = header
    if magic != b"FSTRC01\0" or version != 7 or header_size != 72:
        raise ValueError(f"not a canonical FST v7 trace: {path}")
    if record_size != 64:
        raise ValueError(f"unexpected FST record size: {path}")
    if path.stat().st_size < header_size + records * record_size:
        raise ValueError(f"truncated FST record stream: {path}")
    return core_id, records


def read_asmap(path: Path, core_id: int, records: int):
    map_path = Path(str(path) + ".asmap")
    if not map_path.is_file():
        raise ValueError(f"ifmap requires an asmap: {path}")
    with map_path.open("rb") as source:
        header = read_header(source, ASMAP_HEADER, map_path)
        magic, version, header_size, entry_size, source_core, count, rows, reserved = header
        if (
            magic != b"FSTASM1\0"
            or version != 1
            or header_size != ASMAP_HEADER.size
            or entry_size != ASMAP_ENTRY.size
            or source_core != core_id
            or count != records
            or rows == 0
            or reserved != 0
            or map_path.stat().st_size != header_size + rows * entry_size
        ):
            raise ValueError(f"invalid asmap header: {map_path}")
        transitions = [ASMAP_ENTRY.unpack(source.read(entry_size)) for _ in range(rows)]
    if transitions[0][0] != 0:
        raise ValueError(f"asmap does not start at ordinal zero: {map_path}")
    for index, (ordinal, address_space) in enumerate(transitions):
        if address_space == 0 or ordinal >= records:
            raise ValueError(f"invalid asmap row {index}: {map_path}")
        if index and (
            ordinal <= transitions[index - 1][0]
            or address_space == transitions[index - 1][1]
        ):
            raise ValueError(f"unordered/redundant asmap row {index}: {map_path}")
    return transitions


def read_ifmap(path: Path, core_id: int, records: int, transitions):
    map_path = Path(str(path) + ".ifmap")
    with map_path.open("rb") as source:
        header = read_header(source, IFMAP_HEADER, map_path)
        magic, version, header_size, entry_size, source_core, count, rows, page_bits, reserved = header
        if (
            magic != b"FSTIFM1\0"
            or version != 1
            or header_size != IFMAP_HEADER.size
            or entry_size != IFMAP_ENTRY.size
            or source_core != core_id
            or count != records
            or rows == 0
            or page_bits != 12
            or reserved != 0
            or map_path.stat().st_size != header_size + rows * entry_size
        ):
            raise ValueError(f"invalid ifmap header: {map_path}")
        mappings = [IFMAP_ENTRY.unpack(source.read(entry_size)) for _ in range(rows)]

    transition_index = 0
    previous = None
    active_pages: dict[tuple[int, int], int] = {}
    remaps = 0
    for index, row in enumerate(mappings):
        ordinal, address_space, virtual_page, physical_page = row
        while (
            transition_index + 1 < len(transitions)
            and transitions[transition_index + 1][0] <= ordinal
        ):
            transition_index += 1
        key = (ordinal, address_space, virtual_page)
        active_key = (address_space, virtual_page)
        if (
            ordinal >= records
            or address_space == 0
            or physical_page > ((1 << 64) - 1) >> page_bits
            or (previous is not None and key <= previous)
            or transitions[transition_index][1] != address_space
            or active_pages.get(active_key) == physical_page
        ):
            raise ValueError(f"invalid/redundant ifmap row {index}: {map_path}")
        if active_key in active_pages:
            remaps += 1
        active_pages[active_key] = physical_page
        previous = key
    return map_path, mappings, remaps


def audit(path: Path, require: bool) -> dict:
    core_id, records = read_fst_header(path)
    map_path = Path(str(path) + ".ifmap")
    result = {
        "fst": str(path),
        "core_id": core_id,
        "records": records,
        "ifmap": str(map_path),
        "present": map_path.is_file(),
    }
    if not map_path.is_file():
        if require:
            raise ValueError(f"instruction-page map is required: {path}")
        return result

    transitions = read_asmap(path, core_id, records)
    map_path, mappings, remaps = read_ifmap(path, core_id, records, transitions)
    transition_index = 0
    mapping_index = 0
    active: dict[tuple[int, int], int] = {}
    mapped_records = 0
    mapped_virtual_pages: set[tuple[int, int]] = set()
    mapped_physical_pages: set[int] = set()
    with path.open("rb") as source:
        source.seek(FST_HEADER.size)
        for ordinal in range(records):
            while (
                transition_index + 1 < len(transitions)
                and transitions[transition_index + 1][0] <= ordinal
            ):
                transition_index += 1
            address_space = transitions[transition_index][1]
            while mapping_index < len(mappings) and mappings[mapping_index][0] == ordinal:
                _, row_as, virtual_page, physical_page = mappings[mapping_index]
                active[(row_as, virtual_page)] = physical_page
                mapping_index += 1
            record = source.read(64)
            if len(record) != 64:
                raise ValueError(f"truncated FST record {ordinal}: {path}")
            pc = PC.unpack_from(record)[0]
            key = (address_space, pc >> 12)
            physical_page = active.get(key)
            if physical_page is not None:
                mapped_records += 1
                mapped_virtual_pages.add(key)
                mapped_physical_pages.add(physical_page)

    result.update(
        {
            "bytes": map_path.stat().st_size,
            "entries": len(mappings),
            "remaps": remaps,
            "address_spaces": len({row[1] for row in mappings}),
            "virtual_pages": len({(row[1], row[2]) for row in mappings}),
            "physical_pages": len({row[3] for row in mappings}),
            "mapped_records": mapped_records,
            "unmapped_records": records - mapped_records,
            "record_coverage": mapped_records / records if records else 0.0,
            "active_virtual_pages": len(mapped_virtual_pages),
            "active_physical_pages": len(mapped_physical_pages),
        }
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("fst", nargs="+", type=Path)
    parser.add_argument("--require", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    traces = [audit(path, args.require) for path in args.fst]
    present = [trace for trace in traces if trace["present"]]
    report = {
        "schema": "fastsim-fst-ifmap-audit-v1",
        "traces": traces,
        "aggregate": {
            "trace_count": len(traces),
            "present_count": len(present),
            "entries": sum(trace.get("entries", 0) for trace in traces),
            "records": sum(trace["records"] for trace in traces),
            "mapped_records": sum(trace.get("mapped_records", 0) for trace in traces),
            "unmapped_records": sum(trace.get("unmapped_records", trace["records"]) for trace in traces),
        },
    }
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload)
    else:
        print(payload, end="")


if __name__ == "__main__":
    main()
