#!/usr/bin/env python3
"""Compare the Python v29 decoder with a gem5-C++ AddrRange oracle."""
from __future__ import annotations

import argparse
import hashlib
import os
import random
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from tcsim.utils.io import dump_json
from tcsim.v29.features import load_trace_profile


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace-dir", required=True)
    parser.add_argument("--gem5-root", default="/data00/yinhaolang/gem5")
    parser.add_argument("--gem5-build", default="X86_MESI_Three_Level")
    parser.add_argument("--samples", type=int, default=100000)
    parser.add_argument("--seed", type=int, default=20260716)
    parser.add_argument("--out", required=True)
    parser.add_argument("--build-dir", default="/tmp/tcsim_v29_decoder_audit")
    args = parser.parse_args()

    profile, decoder = load_trace_profile(args.trace_dir)
    source = os.path.join(ROOT, "tests", "cpp", "v29_gem5_decoder_oracle.cc")
    os.makedirs(args.build_dir, exist_ok=True)
    binary = os.path.join(args.build_dir, "v29_gem5_decoder_oracle")
    compile_command = [
        "g++", "-std=c++17", "-O2",
        "-I", os.path.join(args.gem5_root, "build", args.gem5_build),
        "-I", os.path.join(args.gem5_root, "src"),
        "-I", os.path.join(args.gem5_root, "ext"),
        source,
        os.path.join(args.gem5_root, "src", "base", "cprintf.cc"),
        "-o", binary,
    ]
    subprocess.run(compile_command, check=True)
    generator = random.Random(int(args.seed))
    addresses = {
        0, 64, 127 * 8 * 64, 128 * 8 * 64,
        128 * 16 * 8 * 64, 128 * 16 * 2 * 8 * 64,
        4294967296 - 64,
    }
    for _ in range(max(0, int(args.samples) - len(addresses))):
        addresses.add(generator.randrange(0, 4294967296 // 64) * 64)
    ordered = sorted(addresses)
    completed = subprocess.run(
        [binary],
        input="".join(f"{address}\n" for address in ordered),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    rows = [line.split() for line in completed.stdout.splitlines() if line.strip()]
    if len(rows) != len(ordered):
        raise RuntimeError("C++ decoder oracle returned the wrong row count")
    mismatches = []
    for values in rows:
        parsed = tuple(int(value) for value in values)
        address = parsed[0]
        expected = parsed[1:]
        decoded = decoder.decode(address)
        actual = (
            decoded.physical_line,
            decoded.l1_set,
            decoded.l2_set,
            decoded.llc_set,
            decoded.llc_bank,
            decoded.dram_channel,
            decoded.dram_rank,
            decoded.dram_bank,
            decoded.dram_row,
            decoded.dram_column,
        )
        if actual != expected:
            mismatches.append({
                "address": address,
                "python": actual,
                "cpp": expected,
            })
            if len(mismatches) >= 100:
                break
    source_files = [
        os.path.join(args.gem5_root, "src", "base", "addr_range.hh"),
        os.path.join(args.gem5_root, "src", "mem", "dram_interface.cc"),
    ]
    report = {
        "schema_version": "tcsim-v29-decoder-differential-1",
        "trace_dir": os.path.abspath(args.trace_dir),
        "samples": len(ordered),
        "matches": len(ordered) - len(mismatches),
        "mismatches": mismatches,
        "pass": not mismatches,
        "python_decoder_hash": decoder.provenance_hash(),
        "cpp_oracle": {
            "source": source,
            "binary": binary,
            "uses_gem5_addr_range_header": True,
            "dram_arithmetic_mirrors_decode_packet": True,
            "compile_command": compile_command,
        },
        "gem5_source_hashes": {
            os.path.relpath(path, args.gem5_root): _sha256(path)
            for path in source_files
        },
        "uarch_profile_hash_input": profile.get("resource_decoder_hash"),
    }
    dump_json(args.out, report)
    print(
        f"[v29 decoder] samples={len(ordered)} mismatches={len(mismatches)} "
        f"pass={report['pass']} report={os.path.abspath(args.out)}",
        flush=True,
    )
    return 0 if report["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
