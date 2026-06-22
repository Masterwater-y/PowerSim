#!/usr/bin/env python3
import argparse
import gzip
import json
import struct
from pathlib import Path


TRACE_TYPE_INSTR = 10
TRACE_TYPE_INSTR_DIRECT_JUMP = 11
TRACE_TYPE_INSTR_INDIRECT_JUMP = 12
TRACE_TYPE_INSTR_CONDITIONAL_JUMP = 13
TRACE_TYPE_INSTR_DIRECT_CALL = 14
TRACE_TYPE_INSTR_INDIRECT_CALL = 15
TRACE_TYPE_INSTR_RETURN = 16
TRACE_TYPE_ENCODING = 47
TRACE_TYPE_INSTR_TAKEN_JUMP = 48
TRACE_TYPE_INSTR_UNTAKEN_JUMP = 49

ENTRY = struct.Struct("<HHQ")


def is_instruction(entry_type: int) -> bool:
    return (
        TRACE_TYPE_INSTR <= entry_type <= TRACE_TYPE_INSTR_RETURN
        or entry_type in (TRACE_TYPE_INSTR_TAKEN_JUMP, TRACE_TYPE_INSTR_UNTAKEN_JUMP)
    )


def parse_hex_bytes(text: str) -> bytes:
    compact = text.replace(" ", "").replace(":", "").lower()
    if len(compact) % 2 != 0:
        raise ValueError(f"hex marker has odd length: {text}")
    return bytes.fromhex(compact)


def slice_trace(input_path: Path, output_path: Path, begin_magic: bytes, end_magic: bytes) -> dict:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    in_roi = False
    found_begin = False
    found_end = False
    current_encoding = bytearray()
    pending_encoding_entries = []

    total_entries = 0
    total_instructions = 0
    roi_entries = 0
    roi_instructions = 0
    begin_pc = None
    end_pc = None

    with gzip.open(input_path, "rb") as fin, gzip.open(output_path, "wb", compresslevel=6) as fout:
        while True:
            raw = fin.read(ENTRY.size)
            if not raw:
                break
            if len(raw) != ENTRY.size:
                raise RuntimeError(f"short trace entry after {total_entries} entries")

            total_entries += 1
            entry_type, size, value = ENTRY.unpack(raw)

            if entry_type == TRACE_TYPE_ENCODING:
                enc = value.to_bytes(8, "little")[:size]
                current_encoding.extend(enc)
                pending_encoding_entries.append(raw)
                continue

            if is_instruction(entry_type):
                total_instructions += 1
                inst_encoding = bytes(current_encoding)
                pc = value

                if not in_roi and inst_encoding == begin_magic:
                    found_begin = True
                    begin_pc = pc
                    in_roi = True
                    current_encoding.clear()
                    pending_encoding_entries.clear()
                    continue

                if in_roi and inst_encoding == end_magic:
                    found_end = True
                    end_pc = pc
                    break

                if in_roi:
                    for enc_raw in pending_encoding_entries:
                        fout.write(enc_raw)
                        roi_entries += 1
                    fout.write(raw)
                    roi_entries += 1
                    roi_instructions += 1

                current_encoding.clear()
                pending_encoding_entries.clear()
                continue

            if in_roi:
                fout.write(raw)
                roi_entries += 1

    if not found_begin:
        raise RuntimeError(f"begin marker {begin_magic.hex()} was not found in {input_path}")
    if not found_end:
        raise RuntimeError(f"end marker {end_magic.hex()} was not found after begin marker")
    if roi_instructions == 0:
        raise RuntimeError("ROI slice contains zero instructions")

    return {
        "input_trace": str(input_path),
        "output_trace": str(output_path),
        "begin_marker_hex": begin_magic.hex(),
        "end_marker_hex": end_magic.hex(),
        "begin_pc": f"0x{begin_pc:x}" if begin_pc is not None else None,
        "end_pc": f"0x{end_pc:x}" if end_pc is not None else None,
        "total_entries_scanned": total_entries,
        "total_instructions_scanned": total_instructions,
        "roi_entries": roi_entries,
        "roi_instructions": roi_instructions,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Slice a DynamoRIO raw trace by ROI marker instruction bytes.")
    ap.add_argument("--input", required=True, type=Path, help="Input .trace.gz file")
    ap.add_argument("--output", required=True, type=Path, help="Output ROI .trace.gz file")
    ap.add_argument("--begin", default="0f1f840042424242", help="Begin marker instruction encoding")
    ap.add_argument("--end", default="0f1f840043434343", help="End marker instruction encoding")
    ap.add_argument("--summary", type=Path, help="Optional JSON summary path")
    args = ap.parse_args()

    summary = slice_trace(args.input, args.output, parse_hex_bytes(args.begin), parse_hex_bytes(args.end))
    print(json.dumps(summary, indent=2, sort_keys=True))

    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
