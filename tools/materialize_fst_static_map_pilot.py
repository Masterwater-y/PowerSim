#!/usr/bin/env python3
"""Clone an FST case by hard link and attach one decoded static map per core."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

from build_fst_instruction_map import (
    ISA_UNKNOWN,
    ISA_X86_64,
    load_rows,
    read_fst_identity,
    write_map,
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def clone(source: Path, target: Path) -> str:
    try:
        os.link(source, target)
        return "hardlink"
    except OSError:
        shutil.copy2(source, target)
        return "copy"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a storage-efficient FST static-map pilot case."
    )
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--decoded", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--complete",
        action="store_true",
        help="assert decoded input covers the executable module scope",
    )
    parser.add_argument("--isa", choices=("x86-64",))
    parser.add_argument(
        "--require-operands",
        action="store_true",
        help="reject any decoded row without read/write register IDs",
    )
    args = parser.parse_args()

    source_trace = args.case / "tao_trace"
    source_manifest = source_trace / "manifest.txt"
    if not source_manifest.is_file():
        raise ValueError(f"case has no TaoTrace manifest: {args.case}")
    if args.output.exists():
        raise ValueError(f"pilot output already exists: {args.output}")
    rows = load_rows(args.decoded, args.require_operands)
    isa = ISA_X86_64 if args.isa == "x86-64" else ISA_UNKNOWN

    staging = args.output.with_name(
        f".{args.output.name}.staging-{uuid.uuid4().hex[:10]}"
    )
    trace = staging / "tao_trace"
    trace.mkdir(parents=True)
    files: dict[str, dict[str, object]] = {}
    try:
        manifest_lines = []
        for line_number, text in enumerate(
            source_manifest.read_text(encoding="utf-8").splitlines(), 1
        ):
            if not text.strip() or text.lstrip().startswith("#"):
                manifest_lines.append(text + "\n")
                continue
            fields = text.split()
            if len(fields) < 3:
                raise ValueError(
                    f"invalid trace manifest row {source_manifest}:{line_number}"
                )
            core = int(fields[0])
            source_fst = Path(fields[2])
            if not source_fst.is_absolute():
                source_fst = source_manifest.parent / source_fst
            source_fst = source_fst.resolve()
            target_fst = trace / f"core{core}.fst"
            method = clone(source_fst, target_fst)
            source_vmap = Path(str(source_fst) + ".vmap")
            vmap_method = None
            if source_vmap.is_file():
                vmap_method = clone(
                    source_vmap, Path(str(target_fst) + ".vmap")
                )
            source_asmap = Path(str(source_fst) + ".asmap")
            asmap_method = None
            if source_asmap.is_file():
                asmap_method = clone(
                    source_asmap, Path(str(target_fst) + ".asmap")
                )
            core_id, record_count = read_fst_identity(target_fst)
            if core_id != core:
                raise ValueError(
                    f"manifest/core mismatch for {source_fst}: {core} != {core_id}"
                )
            target_map = Path(str(target_fst) + ".imap")
            imap_version = write_map(
                target_map,
                core_id,
                record_count,
                rows,
                args.complete,
                isa,
            )
            fields[2] = target_fst.name
            manifest_lines.append(" ".join(fields) + "\n")
            files[str(core)] = {
                "fst": str(args.output / "tao_trace" / target_fst.name),
                "fst_clone_method": method,
                "source_record_count": record_count,
                "virtual_page_map_clone_method": vmap_method,
                "address_space_map_clone_method": asmap_method,
                "static_instruction_map": str(
                    args.output / "tao_trace" / target_map.name
                ),
                "static_instruction_map_sha256": sha256(target_map),
                "static_instruction_map_version": imap_version,
            }
        (trace / "manifest.txt").write_text(
            "".join(manifest_lines), encoding="utf-8"
        )
        metadata = {
            "schema": "fastsim-fst-static-map-pilot-v2",
            "source_case": str(args.case.resolve()),
            "decoded": str(args.decoded.resolve()),
            "decoded_sha256": sha256(args.decoded),
            "static_instruction_count": len(rows),
            "complete": args.complete,
            "isa": args.isa,
            "operand_semantics_rows": sum(
                row.operand_semantics_valid for row in rows
            ),
            "operands_complete": all(
                row.operand_semantics_valid for row in rows
            ),
            "files": files,
        }
        (staging / "pilot.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        staging.rename(args.output)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    print(json.dumps(metadata, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2)
