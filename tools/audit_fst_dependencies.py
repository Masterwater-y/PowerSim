#!/usr/bin/env python3
"""Stream-audit complete FST RAW dependencies and their exact storage cost.

Consumes functional records only, never reference issue/response timestamps.
"""
from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path
import struct

HEADER = struct.Struct("<8sIIIIQQ4Q")
RECORD = struct.Struct("<QQQQ4IHHhBB4BI")
DEPENDENCIES = struct.Struct("<8sIIIIQQQ")
ROW = struct.Struct("<QII")
COMPLETE = 1 << 5


def record_hash(raw: bytes) -> int:
    result = 2166136261
    for byte in raw:
        result = ((result ^ byte) * 16777619) & 0xffffffff
    return result


def audit(path: Path, require_complete: bool = False) -> dict:
    with path.open("rb") as source:
        header = HEADER.unpack(source.read(HEADER.size))
        magic, version, header_size, record_size, core, count, features = header[:7]
        if (magic, version, header_size, record_size) != (b"FSTRC01\0", 7, 72, 64):
            raise ValueError(f"not a canonical FST v7 stream: {path}")
        complete = bool(features & COMPLETE)
        if require_complete and not complete:
            raise ValueError(f"complete dependencies are not declared: {path}")
        companion = Path(str(path) + ".deps")
        deps = companion.open("rb") if complete else None
        rows = extra_count = extra_read = rows_read = 0
        next_row = None
        if deps:
            dm, dv, ds, dc, flags, dn, rows, extra_count = DEPENDENCIES.unpack(deps.read(48))
            if (dm, dv, ds, dc, flags, dn) != (b"FSTDEP1\0", 1, 48, core, 0, count):
                raise ValueError(f"dependency header identity mismatch: {path}")
            if rows > count or extra_count < rows or companion.stat().st_size != 48 + 16 * rows + 4 * extra_count:
                raise ValueError(f"dependency table size/count mismatch: {path}")

        def next_extension(previous: int = -1):
            nonlocal extra_read, rows_read
            if rows_read == rows:
                if extra_read != extra_count:
                    raise ValueError("extra edge count mismatch")
                return None
            ordinal, size, fingerprint = ROW.unpack(deps.read(16))
            if not previous < ordinal < count or not 1 <= size <= 251:
                raise ValueError("extension ordinal/count is invalid")
            distances = struct.unpack(f"<{size}I", deps.read(4 * size))
            extra_read += size
            rows_read += 1
            return ordinal, fingerprint, distances

        try:
            next_row = next_extension()
            fanin = collections.Counter()
            operands = collections.Counter()
            kernel = 0
            for ordinal in range(count):
                raw = source.read(64)
                record = RECORD.unpack(raw)
                inline = list(record[4:8])
                extras = ()
                if next_row and next_row[0] == ordinal:
                    if next_row[1] != record_hash(raw):
                        raise ValueError(f"hot record/extension mismatch at {ordinal}")
                    extras = next_row[2]
                    next_row = next_extension(ordinal)
                distances = [d for d in inline if d] + list(extras)
                if any(d > ordinal for d in distances):
                    raise ValueError(f"dependency precedes trace at {ordinal}")
                if complete:
                    if (extras and not all(inline)) or inline != sorted(inline, key=lambda d: (d == 0, d)):
                        raise ValueError(f"inline dependency ordering at {ordinal}")
                    if len(distances) > record[11] or any(a >= b for a, b in zip(distances, distances[1:])):
                        raise ValueError(f"dependency completeness/order at {ordinal}")
                fanin[len(set(distances))] += 1
                operands[record[11]] += 1
                kernel += record[10] < -1
            if next_row is not None or (deps and deps.read(1)):
                raise ValueError("unconsumed dependency data")
        finally:
            if deps:
                deps.close()
    added = companion.stat().st_size if complete else 0
    return {
        "fst": str(path.resolve()), "core": core, "records": count,
        "complete": complete, "kernel_records": kernel,
        "n_src_over_four": sum(v for k, v in operands.items() if k > 4),
        "max_n_src": max(operands, default=0),
        "max_distinct_producers": max(fanin, default=0),
        "producer_fanin_histogram": dict(sorted(fanin.items())),
        "extension_records": rows, "extra_dependencies": extra_count,
        "total_dependencies": sum(k * v for k, v in fanin.items()),
        "fst_bytes": path.stat().st_size, "dependency_companion_bytes": added,
        "increase_percent_vs_fst": 100 * added / path.stat().st_size,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fst", type=Path, action="append", required=True)
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    reports = [audit(path, args.require_complete) for path in args.fst]
    base = sum(row["fst_bytes"] for row in reports)
    extra = sum(row["dependency_companion_bytes"] for row in reports)
    result = {"files": reports, "fst_bytes": base, "dependency_companion_bytes": extra,
              "increase_percent": 100 * extra / base}
    encoded = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded)
    print(encoded, end="")


if __name__ == "__main__":
    main()
