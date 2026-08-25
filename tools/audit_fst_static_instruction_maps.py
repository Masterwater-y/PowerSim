#!/usr/bin/env python3
"""Strictly audit portable ``.fst.imap`` v1/v2 companions.

This is an input-observability gate only.  It validates executable geometry
and register operand coverage; it never estimates or changes CPI.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import struct
import sys
from typing import Any


FST_HEADER = struct.Struct("<8sIIIIQQQQQQ")
IMAP_HEADER = struct.Struct("<8sIIIIQQII")
IMAP_ENTRY_V1 = struct.Struct("<QQQHB5x")
IMAP_ENTRY_V2 = struct.Struct("<QQQHBB4xQQQQ")

FST_MAGIC = b"FSTRC01\0"
IMAP_MAGIC_V1 = b"FSTIMP1\0"
IMAP_MAGIC_V2 = b"FSTIMP2\0"
IMAP_COMPLETE = 1 << 0
IMAP_OPERANDS_COMPLETE = 1 << 1
IMAP_OPERANDS_VALID = 1 << 0
ISA_UNKNOWN = 0
ISA_X86_64 = 1
KNOWN_STATIC_FLAGS = (1 << 7) - 1


def fst_identity(path: Path) -> tuple[int, int]:
    with path.open("rb") as source:
        raw = source.read(FST_HEADER.size)
    if len(raw) != FST_HEADER.size:
        raise ValueError("short FST header")
    fields = FST_HEADER.unpack(raw)
    magic, version, header_size, record_size, core_id, records = fields[:6]
    if magic != FST_MAGIC or version != 7 or header_size != 72 or record_size != 64:
        raise ValueError("static maps require canonical FST v7")
    return core_id, records


def validate_geometry(
    pc: int, fallthrough: int, target: int, flags: int, size: int
) -> None:
    if size < 1 or size > 15 or pc + size > (1 << 64) - 1:
        raise ValueError("invalid instruction geometry")
    if fallthrough != pc + size or flags & ~KNOWN_STATIC_FLAGS:
        raise ValueError("invalid fallthrough or static flags")
    branch = bool(flags & (1 << 0))
    conditional = bool(flags & (1 << 1))
    indirect = bool(flags & (1 << 2))
    call = bool(flags & (1 << 3))
    is_return = bool(flags & (1 << 4))
    target_valid = bool(flags & (1 << 5))
    if (conditional or indirect or call or is_return) and not branch:
        raise ValueError("control-flow subtype lacks branch flag")
    if is_return and not indirect:
        raise ValueError("return lacks indirect flag")
    if target_valid and (not branch or indirect):
        raise ValueError("invalid direct-target flag")
    if not target_valid and target != 0:
        raise ValueError("direct-target value lacks validity flag")


def audit_map(fst: Path) -> dict[str, Any]:
    core_id, record_count = fst_identity(fst)
    imap = Path(str(fst) + ".imap")
    result: dict[str, Any] = {
        "fst": str(fst),
        "imap": str(imap),
        "core_id": core_id,
        "source_record_count": record_count,
        "present": imap.is_file(),
    }
    if not imap.is_file():
        return result
    with imap.open("rb") as source:
        raw = source.read(IMAP_HEADER.size)
        if len(raw) != IMAP_HEADER.size:
            raise ValueError("short instruction-map header")
        (
            magic,
            version,
            header_size,
            entry_size,
            map_core,
            map_records,
            entry_count,
            flags,
            isa,
        ) = IMAP_HEADER.unpack(raw)
        map_v1 = magic == IMAP_MAGIC_V1 and version == 1
        map_v2 = magic == IMAP_MAGIC_V2 and version == 2
        entry = IMAP_ENTRY_V2 if map_v2 else IMAP_ENTRY_V1
        known_flags = IMAP_COMPLETE | (IMAP_OPERANDS_COMPLETE if map_v2 else 0)
        valid_isa = isa == (ISA_X86_64 if map_v2 else ISA_UNKNOWN)
        if (
            not (map_v1 or map_v2)
            or header_size != IMAP_HEADER.size
            or entry_size != entry.size
            or map_core != core_id
            or map_records != record_count
            or entry_count == 0
            or flags & ~known_flags
            or not valid_isa
            or imap.stat().st_size != header_size + entry_count * entry_size
        ):
            raise ValueError("invalid instruction-map header or FST identity")

        previous_pc: int | None = None
        operand_rows = 0
        read_rows = 0
        write_rows = 0
        read_operands = 0
        write_operands = 0
        max_read_operands = 0
        max_write_operands = 0
        branch_rows = 0
        memory_rows = 0
        for index in range(entry_count):
            encoded = source.read(entry_size)
            if len(encoded) != entry_size:
                raise ValueError(f"short instruction-map row {index}")
            fields = entry.unpack(encoded)
            pc, fallthrough, target, static_flags, size = fields[:5]
            validate_geometry(pc, fallthrough, target, static_flags, size)
            if previous_pc is not None and pc <= previous_pc:
                raise ValueError(f"row {index} is not strictly PC ordered")
            previous_pc = pc
            if static_flags & (1 << 0):
                branch_rows += 1
            if static_flags & (1 << 6):
                memory_rows += 1
            if map_v2:
                semantic_flags = fields[5]
                read_mask = fields[6:8]
                write_mask = fields[8:10]
                if semantic_flags & ~IMAP_OPERANDS_VALID:
                    raise ValueError(f"row {index} has unknown semantic flags")
                valid = bool(semantic_flags & IMAP_OPERANDS_VALID)
                if not valid and (any(read_mask) or any(write_mask)):
                    raise ValueError(f"row {index} has masks without validity")
                if valid:
                    row_reads = sum(bin(mask).count("1") for mask in read_mask)
                    row_writes = sum(bin(mask).count("1") for mask in write_mask)
                    operand_rows += 1
                    read_rows += int(any(read_mask))
                    write_rows += int(any(write_mask))
                    read_operands += row_reads
                    write_operands += row_writes
                    max_read_operands = max(max_read_operands, row_reads)
                    max_write_operands = max(max_write_operands, row_writes)

    operands_complete = bool(flags & IMAP_OPERANDS_COMPLETE)
    if map_v2 and operand_rows == 0:
        raise ValueError("v2 map has no operand-semantic rows")
    if operands_complete and operand_rows != entry_count:
        raise ValueError("operand-complete flag disagrees with rows")
    result.update(
        {
            "version": version,
            "isa": "x86-64" if isa == ISA_X86_64 else None,
            "instruction_rows": entry_count,
            "map_complete": bool(flags & IMAP_COMPLETE),
            "operand_semantics_rows": operand_rows,
            "operands_complete": operands_complete,
            "rows_with_reads": read_rows,
            "rows_with_writes": write_rows,
            "read_operands": read_operands,
            "write_operands": write_operands,
            "max_read_operands_per_row": max_read_operands,
            "max_write_operands_per_row": max_write_operands,
            "branch_rows": branch_rows,
            "memory_rows": memory_rows,
        }
    )
    return result


def manifest_traces(path: Path) -> list[Path]:
    traces: list[Path] = []
    for line_number, text in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not text.strip() or text.lstrip().startswith("#"):
            continue
        fields = text.split()
        if len(fields) < 3:
            raise ValueError(f"{path}:{line_number}: invalid manifest row")
        trace = Path(fields[2])
        traces.append(trace if trace.is_absolute() else path.parent / trace)
    return traces


def discover(inputs: list[Path]) -> list[Path]:
    traces: dict[Path, None] = {}
    for item in inputs:
        if item.is_dir():
            candidates = item.rglob("*.fst")
        elif item.name == "manifest.txt":
            candidates = manifest_traces(item)
        else:
            candidates = [item]
        for candidate in candidates:
            # Keep a manifest/symlink-local companion association. Resolving
            # the FST symlink here would incorrectly search for `.imap` next
            # to the storage target instead of next to the declared stream.
            traces[candidate.absolute()] = None
    if not traces:
        raise ValueError("no FST files discovered")
    return sorted(traces)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit FST static-map geometry and operand coverage."
    )
    parser.add_argument("inputs", type=Path, nargs="+")
    parser.add_argument("--require-map", action="store_true")
    parser.add_argument("--require-map-complete", action="store_true")
    parser.add_argument("--require-operands", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for fst in discover(args.inputs):
        try:
            row = audit_map(fst)
            if args.require_map and not row["present"]:
                raise ValueError("instruction map is required")
            if args.require_map_complete and not row.get("map_complete", False):
                raise ValueError("complete executable map is required")
            if args.require_operands and not row.get("operands_complete", False):
                raise ValueError("complete register operand semantics are required")
            row["valid"] = True
            rows.append(row)
        except (OSError, ValueError, struct.error) as error:
            errors.append(f"{fst}: {error}")
            rows.append({"fst": str(fst), "valid": False, "error": str(error)})

    report = {
        "schema": "fastsim-fst-static-instruction-map-audit-v2",
        "valid": not errors,
        "trace_count": len(rows),
        "map_count": sum(bool(row.get("present")) for row in rows),
        "v2_map_count": sum(row.get("version") == 2 for row in rows),
        "instruction_rows": sum(int(row.get("instruction_rows", 0)) for row in rows),
        "operand_semantics_rows": sum(
            int(row.get("operand_semantics_rows", 0)) for row in rows
        ),
        "read_operands": sum(int(row.get("read_operands", 0)) for row in rows),
        "write_operands": sum(
            int(row.get("write_operands", 0)) for row in rows
        ),
        "max_read_operands_per_row": max(
            (int(row.get("max_read_operands_per_row", 0)) for row in rows),
            default=0,
        ),
        "max_write_operands_per_row": max(
            (int(row.get("max_write_operands_per_row", 0)) for row in rows),
            default=0,
        ),
        "errors": errors,
        "traces": rows,
    }
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if not errors else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2)
