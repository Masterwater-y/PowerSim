#!/usr/bin/env python3
"""Build a canonical ``.fst.imap`` from producer-decoded instruction facts.

The input is JSONL with one row per static instruction.  It intentionally
contains no dynamic branch outcome, timing, cache result, or physical address,
so both a drmemtrace module decoder and gem5 TaoTrace can emit the same schema.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
from typing import Any, NamedTuple


FST_HEADER = struct.Struct("<8sIIIIQQQQQQ")
IMAP_HEADER = struct.Struct("<8sIIIIQQII")
IMAP_ENTRY_V1 = struct.Struct("<QQQHB5x")
IMAP_ENTRY_V2 = struct.Struct("<QQQHBB4xQQQQ")

FST_MAGIC = b"FSTRC01\0"
IMAP_MAGIC_V1 = b"FSTIMP1\0"
IMAP_MAGIC_V2 = b"FSTIMP2\0"
IMAP_VERSION_V1 = 1
IMAP_VERSION_V2 = 2
IMAP_COMPLETE = 1 << 0
IMAP_OPERANDS_COMPLETE = 1 << 1
IMAP_OPERANDS_VALID = 1 << 0

ISA_UNKNOWN = 0
ISA_X86_64 = 1
REGISTER_COUNT = 128

STATIC_BRANCH = 1 << 0
STATIC_CONDITIONAL = 1 << 1
STATIC_INDIRECT = 1 << 2
STATIC_CALL = 1 << 3
STATIC_RETURN = 1 << 4
STATIC_DIRECT_TARGET_VALID = 1 << 5
STATIC_MEMORY = 1 << 6


class DecodedInstruction(NamedTuple):
    pc: int
    fallthrough: int
    direct_target: int
    flags: int
    size: int
    operand_semantics_valid: bool
    read_register_mask: tuple[int, int]
    write_register_mask: tuple[int, int]


def integer(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} cannot be boolean")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str):
        result = int(value, 0)
    else:
        raise ValueError(f"{field} must be an integer")
    if result < 0 or result > (1 << 64) - 1:
        raise ValueError(f"{field} is outside uint64")
    return result


def boolean(row: dict[str, Any], field: str) -> bool:
    value = row.get(field, False)
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be boolean when present")
    return value


def decode_register_mask(
    value: Any, field: str
) -> tuple[int, int]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array of register IDs")
    words = [0, 0]
    seen: set[int] = set()
    for index, item in enumerate(value):
        register_id = integer(item, f"{field}[{index}]")
        if register_id >= REGISTER_COUNT:
            raise ValueError(
                f"{field}[{index}] is outside the 0..127 ISA namespace"
            )
        if register_id in seen:
            raise ValueError(f"{field} contains duplicate register ID {register_id}")
        seen.add(register_id)
        words[register_id // 64] |= 1 << (register_id % 64)
    return words[0], words[1]


def decode_row(row: dict[str, Any], source: str) -> DecodedInstruction:
    pc = integer(row.get("pc"), f"{source}.pc")
    size = integer(row.get("size"), f"{source}.size")
    if size < 1 or size > 15 or pc + size > (1 << 64) - 1:
        raise ValueError(f"{source}: invalid x86 instruction geometry")
    fallthrough = integer(
        row.get("fallthrough_pc", pc + size), f"{source}.fallthrough_pc"
    )
    if fallthrough != pc + size:
        raise ValueError(f"{source}: fallthrough_pc must equal pc + size")

    branch = boolean(row, "is_branch")
    conditional = boolean(row, "is_conditional")
    indirect = boolean(row, "is_indirect")
    call = boolean(row, "is_call")
    is_return = boolean(row, "is_return")
    memory = boolean(row, "is_memory")
    if conditional or indirect or call or is_return:
        branch = True
    if is_return and not indirect:
        raise ValueError(f"{source}: return must also be indirect")

    target_value = row.get("direct_target")
    target_valid_value = row.get("direct_target_valid", target_value is not None)
    if not isinstance(target_valid_value, bool):
        raise ValueError(f"{source}.direct_target_valid must be boolean")
    if target_valid_value:
        if not branch or indirect or target_value is None:
            raise ValueError(
                f"{source}: valid direct target requires a direct branch"
            )
        direct_target = integer(target_value, f"{source}.direct_target")
    else:
        if target_value not in (None, 0, "0", "0x0"):
            raise ValueError(f"{source}: target value lacks validity")
        direct_target = 0

    flags = 0
    if branch:
        flags |= STATIC_BRANCH
    if conditional:
        flags |= STATIC_CONDITIONAL
    if indirect:
        flags |= STATIC_INDIRECT
    if call:
        flags |= STATIC_CALL
    if is_return:
        flags |= STATIC_RETURN
    if target_valid_value:
        flags |= STATIC_DIRECT_TARGET_VALID
    if memory:
        flags |= STATIC_MEMORY
    read_present = "read_register_ids" in row
    write_present = "write_register_ids" in row
    if read_present != write_present:
        raise ValueError(
            f"{source}: read_register_ids and write_register_ids must "
            "appear together"
        )
    read_mask = (0, 0)
    write_mask = (0, 0)
    if read_present:
        read_mask = decode_register_mask(
            row["read_register_ids"], f"{source}.read_register_ids"
        )
        write_mask = decode_register_mask(
            row["write_register_ids"], f"{source}.write_register_ids"
        )
    return DecodedInstruction(
        pc,
        fallthrough,
        direct_target,
        flags,
        size,
        read_present,
        read_mask,
        write_mask,
    )


def read_fst_identity(path: Path) -> tuple[int, int]:
    with path.open("rb") as source:
        raw = source.read(FST_HEADER.size)
    if len(raw) != FST_HEADER.size:
        raise ValueError(f"short FST header: {path}")
    fields = FST_HEADER.unpack(raw)
    magic, version, header_size, record_size, core_id, record_count = fields[:6]
    if magic != FST_MAGIC or version != 7 or header_size != 72 or record_size != 64:
        raise ValueError(f"instruction maps require canonical FST v7: {path}")
    return core_id, record_count


def load_rows(
    path: Path, require_operands: bool = False
) -> list[DecodedInstruction]:
    instructions: dict[int, DecodedInstruction] = {}
    with path.open("r", encoding="utf-8") as source:
        for line_number, text in enumerate(source, 1):
            if not text.strip():
                continue
            try:
                value = json.loads(text)
            except json.JSONDecodeError as error:
                raise ValueError(f"{path}:{line_number}: {error}") from error
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: row must be an object")
            decoded = decode_row(value, f"{path}:{line_number}")
            prior = instructions.get(decoded[0])
            if prior is not None and prior != decoded:
                raise ValueError(
                    f"{path}:{line_number}: PC has conflicting static decodings"
                )
            instructions[decoded[0]] = decoded
    if not instructions:
        raise ValueError(f"static instruction input is empty: {path}")
    rows = [instructions[pc] for pc in sorted(instructions)]
    if require_operands:
        missing = sum(not row.operand_semantics_valid for row in rows)
        if missing:
            raise ValueError(
                f"{path}: {missing} static rows lack register operand semantics"
            )
    return rows


def write_map(
    output: Path,
    core_id: int,
    record_count: int,
    rows: list[DecodedInstruction],
    complete: bool,
    isa: int = ISA_UNKNOWN,
) -> int:
    any_operands = any(row.operand_semantics_valid for row in rows)
    all_operands = bool(rows) and all(
        row.operand_semantics_valid for row in rows
    )
    if any_operands and isa != ISA_X86_64:
        raise ValueError(
            "register operand semantics require --isa x86-64"
        )
    if not any_operands and isa != ISA_UNKNOWN:
        raise ValueError(
            "--isa requires at least one row with register operands"
        )
    version = IMAP_VERSION_V2 if any_operands else IMAP_VERSION_V1
    magic = IMAP_MAGIC_V2 if any_operands else IMAP_MAGIC_V1
    entry = IMAP_ENTRY_V2 if any_operands else IMAP_ENTRY_V1
    flags = IMAP_COMPLETE if complete else 0
    if all_operands:
        flags |= IMAP_OPERANDS_COMPLETE
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb", prefix=f".{output.name}.", dir=output.parent, delete=False
    ) as temporary:
        temporary_path = Path(temporary.name)
        try:
            temporary.write(
                IMAP_HEADER.pack(
                    magic,
                    version,
                    IMAP_HEADER.size,
                    entry.size,
                    core_id,
                    record_count,
                    len(rows),
                    flags,
                    isa,
                )
            )
            for row in rows:
                if any_operands:
                    temporary.write(
                        IMAP_ENTRY_V2.pack(
                            row.pc,
                            row.fallthrough,
                            row.direct_target,
                            row.flags,
                            row.size,
                            IMAP_OPERANDS_VALID
                            if row.operand_semantics_valid
                            else 0,
                            *row.read_register_mask,
                            *row.write_register_mask,
                        )
                    )
                else:
                    temporary.write(
                        IMAP_ENTRY_V1.pack(
                            row.pc,
                            row.fallthrough,
                            row.direct_target,
                            row.flags,
                            row.size,
                        )
                    )
            temporary.flush()
            os.fsync(temporary.fileno())
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
    os.replace(temporary_path, output)
    return version


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a canonical static-instruction companion for FST v7."
    )
    parser.add_argument("--fst", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--complete",
        action="store_true",
        help="assert that input covers every executable instruction in scope",
    )
    parser.add_argument(
        "--isa",
        choices=("x86-64",),
        help="ISA register namespace; required when operands are present",
    )
    parser.add_argument(
        "--require-operands",
        action="store_true",
        help="reject any decoded row without read/write register IDs",
    )
    args = parser.parse_args()

    core_id, record_count = read_fst_identity(args.fst)
    rows = load_rows(args.input, args.require_operands)
    output = args.output or Path(str(args.fst) + ".imap")
    isa = ISA_X86_64 if args.isa == "x86-64" else ISA_UNKNOWN
    version = write_map(
        output, core_id, record_count, rows, args.complete, isa
    )
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    print(
        json.dumps(
            {
                "schema": "fastsim-fst-static-instruction-map-build-v2",
                "fst": str(args.fst),
                "output": str(output),
                "core_id": core_id,
                "source_record_count": record_count,
                "instruction_count": len(rows),
                "complete": args.complete,
                "imap_version": version,
                "isa": args.isa,
                "operand_semantics_rows": sum(
                    row.operand_semantics_valid for row in rows
                ),
                "operands_complete": all(
                    row.operand_semantics_valid for row in rows
                ),
                "sha256": digest,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
