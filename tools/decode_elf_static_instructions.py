#!/usr/bin/env python3
"""Decode ELF executable sections into producer-neutral instruction JSONL.

This adapter uses GNU objdump only as an ISA decoder.  Its output contains
static facts accepted by ``build_fst_instruction_map.py`` and deliberately
omits dynamic prediction, timing, cache, and PMU information.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any


SECTION_ROW = re.compile(
    r"^\s*\d+\s+(\S+)\s+([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+"
    r"[0-9a-fA-F]+\s+[0-9a-fA-F]+\s+"
)
INSTRUCTION_ROW = re.compile(
    r"^\s*([0-9a-fA-F]+):\s+((?:[0-9a-fA-F]{2}\s+)+)"
    r"\s*([^\s]+)(?:\s+(.*))?$"
)
DIRECT_TARGET = re.compile(r"^\s*(?:0x)?([0-9a-fA-F]+)(?:\s|<|$)")


def run(command: list[str]) -> str:
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"{result.stdout.strip()}"
        )
    return result.stdout


def code_sections(objdump: str, binary: Path) -> list[dict[str, Any]]:
    lines = run([objdump, "-h", str(binary)]).splitlines()
    sections: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        matched = SECTION_ROW.match(line)
        if matched is None or index + 1 >= len(lines):
            continue
        flags = {item.strip() for item in lines[index + 1].split(",")}
        if "CODE" not in flags or "CONTENTS" not in flags:
            continue
        name, size_text, vma_text = matched.groups()
        size = int(size_text, 16)
        if size == 0:
            continue
        sections.append(
            {"name": name, "size": size, "vma": int(vma_text, 16)}
        )
    if not sections:
        raise ValueError(f"ELF has no executable CONTENTS sections: {binary}")
    return sections


def normalized_mnemonic(mnemonic: str, operands: str) -> tuple[str, str]:
    prefixes = {"bnd", "notrack", "rep", "repe", "repz", "repne", "repnz"}
    current = mnemonic.lower()
    remaining = operands.strip()
    while current in prefixes and remaining:
        fields = remaining.split(None, 1)
        current = fields[0].lower()
        remaining = fields[1] if len(fields) == 2 else ""
    return current, remaining


def may_access_memory(mnemonic: str, operands: str) -> bool:
    """Conservatively classify static x86 data-memory instructions."""
    mnemonic, operands = normalized_mnemonic(mnemonic, operands)
    if mnemonic.startswith("lea"):
        return False
    explicit = "(" in operands or re.search(
        r"%(?:cs|ds|es|fs|gs|ss):", operands, re.IGNORECASE
    ) is not None
    implicit_prefixes = (
        "push", "pop", "call", "ret", "enter", "leave",
        "movs", "stos", "lods", "scas", "cmps", "xlat",
        "maskmov", "gather", "scatter",
    )
    return explicit or mnemonic.startswith(implicit_prefixes)


def control_flow(mnemonic: str, operands: str) -> dict[str, Any]:
    mnemonic, operands = normalized_mnemonic(mnemonic, operands)
    result: dict[str, Any] = {
        "is_branch": False,
        "is_conditional": False,
        "is_indirect": False,
        "is_call": False,
        "is_return": False,
    }
    unconditional_jump = mnemonic in {"jmp", "jmpq", "ljmp"}
    call = mnemonic in {"call", "callq", "lcall"}
    is_return = mnemonic.startswith("ret") or mnemonic in {
        "lret",
        "lretq",
        "iret",
        "iretd",
        "iretq",
    }
    conditional = (
        (mnemonic.startswith("j") and not unconditional_jump)
        or mnemonic.startswith("loop")
    )
    if not (unconditional_jump or call or is_return or conditional):
        return result

    result["is_branch"] = True
    result["is_conditional"] = conditional
    result["is_call"] = call
    result["is_return"] = is_return
    stripped = operands.lstrip()
    indirect = is_return or stripped.startswith("*")
    result["is_indirect"] = indirect
    if not indirect:
        matched = DIRECT_TARGET.match(stripped)
        if matched is not None:
            result["direct_target"] = int(matched.group(1), 16)
            result["direct_target_valid"] = True
    return result


def decode_section(
    objdump: str, binary: Path, section: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[tuple[int, int]]]:
    text = run(
        [objdump, "-d", "-w", f"--section={section['name']}", str(binary)]
    )
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        matched = INSTRUCTION_ROW.match(line)
        if matched is None:
            continue
        pc_text, bytes_text, mnemonic, operands = matched.groups()
        encoded = bytes_text.split()
        if not encoded or any(len(byte) != 2 for byte in encoded):
            continue
        pc = int(pc_text, 16)
        size = len(encoded)
        if size < 1 or size > 15:
            raise ValueError(
                f"{binary}:{section['name']}: invalid decoded length {size} at {pc:#x}"
            )
        row: dict[str, Any] = {
            "pc": pc,
            "size": size,
            "fallthrough_pc": pc + size,
            "is_memory": may_access_memory(mnemonic, operands or ""),
        }
        row.update(control_flow(mnemonic, operands or ""))
        rows.append(row)

    rows.sort(key=lambda row: row["pc"])
    gaps: list[tuple[int, int]] = []
    cursor = section["vma"]
    limit = cursor + section["size"]
    for row in rows:
        pc = row["pc"]
        end = pc + row["size"]
        if pc < cursor:
            raise ValueError(
                f"{binary}:{section['name']}: overlapping decoding at {pc:#x}"
            )
        if pc > cursor:
            gaps.append((cursor, pc))
        cursor = end
    if cursor < limit:
        gaps.append((cursor, limit))
    if cursor > limit:
        raise ValueError(
            f"{binary}:{section['name']}: decoding exceeds section boundary"
        )
    return rows, gaps


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Decode ELF CODE sections into static instruction JSONL."
    )
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--objdump", default="objdump")
    parser.add_argument(
        "--require-complete",
        action="store_true",
        help="fail if decoded instructions do not cover every CODE-section byte",
    )
    args = parser.parse_args()

    sections = code_sections(args.objdump, args.binary)
    instructions: dict[int, dict[str, Any]] = {}
    section_reports = []
    for section in sections:
        rows, gaps = decode_section(args.objdump, args.binary, section)
        for row in rows:
            prior = instructions.get(row["pc"])
            if prior is not None and prior != row:
                raise ValueError(
                    f"conflicting ELF decodings for PC {row['pc']:#x}"
                )
            instructions[row["pc"]] = row
        section_reports.append(
            {
                **section,
                "instructions": len(rows),
                "decoded_bytes": sum(row["size"] for row in rows),
                "gaps": [[begin, end] for begin, end in gaps],
            }
        )
    complete = all(not section["gaps"] for section in section_reports)
    if args.require_complete and not complete:
        gap_count = sum(len(section["gaps"]) for section in section_reports)
        raise ValueError(f"ELF static decoding has {gap_count} uncovered ranges")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=f".{args.output.name}.",
        dir=args.output.parent,
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
        try:
            for pc in sorted(instructions):
                temporary.write(json.dumps(instructions[pc], sort_keys=True) + "\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
    os.replace(temporary_path, args.output)

    print(
        json.dumps(
            {
                "schema": "fastsim-elf-static-instruction-decode-v1",
                "binary": str(args.binary),
                "binary_sha256": sha256(args.binary),
                "output": str(args.output),
                "output_sha256": sha256(args.output),
                "instruction_count": len(instructions),
                "complete": complete,
                "sections": section_reports,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
