#!/usr/bin/env python3
"""Static pre-collection checks for the v28 ROI workload implementation."""
from __future__ import annotations

import argparse
import os
import re
import subprocess
from pathlib import Path


FORBIDDEN_SOURCE = re.compile(
    r"\b(?:pthread_|atomic_|__atomic|__sync_|futex|sched_|sleep|usleep|nanosleep)"
)
FORBIDDEN_ASM = re.compile(
    r"\b(?:lock|xchg|cmpxchg|xadd|monitor|mwait|fdivr)\b", re.IGNORECASE
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--source",
        default="/data00/yinhaolang/TSim/workloads/v28/v28_business_proxy.c",
    )
    ap.add_argument(
        "--bin-dir", default="/data00/yinhaolang/TSim/workloads/bin",
    )
    args = ap.parse_args()

    source = Path(args.source).read_text(encoding="utf-8")
    kernel_begin = source.index("static uint64_t kernel_int_alu")
    kernel_end = source.index("static uint64_t dispatch_kernel")
    kernel_source = source[kernel_begin:kernel_end]
    source_hits = sorted(set(FORBIDDEN_SOURCE.findall(kernel_source)))

    business_begin = source.index("static uint64_t kernel_marine")
    business_source = source[business_begin:kernel_end]
    shared_write_hits = re.findall(
        r"(?:st->shared_ro|shared)\s*\[[^\]]+\]\s*=", business_source,
    )
    zipf_kernel_count = len(set(re.findall(
        r"static uint64_t (kernel_(?:marine|gofeed|flink|mysql|redis|pytorch|bvc))",
        business_source,
    )))
    zipf_call_count = business_source.count("shared_zipf_line(")
    private_slots_isolated = (
        "_Static_assert(sizeof(private_slot_t) == LINE_BYTES" in source
        and "private_slot_t per_thread_slot[V28_MAX_THREADS]" in source
    )

    worker = source[source.index("static void *worker_main"):source.index("int main(")]
    order_ok = (
        worker.index("pthread_barrier_wait")
        < worker.index("m5_work_begin_inline")
        < worker.index("dispatch_kernel")
        < worker.index("m5_work_end_inline")
    )
    m5_asm_begin = source.index("static inline void m5_work_begin_inline")
    m5_asm_end = source.index("static uint64_t splitmix64")
    m5_asm_source = source[m5_asm_begin:m5_asm_end]
    # gem5 x86 pseudo-ops return through RAX.  Omitting the clobber lets GCC
    # keep a live pointer in RAX across WORKBEGIN/WORKEND and produces a
    # workload-dependent null dereference inside gem5.
    m5_rax_clobbers = len(re.findall(r'\:\s*"rax",\s*"memory"\)', m5_asm_source))
    m5_rax_clobber_ok = m5_rax_clobbers == 3

    bins = sorted(Path(args.bin_dir).glob("v28_*"))
    asm_hits = []
    for binary in bins:
        nm = subprocess.run(
            ["nm", "-an", os.fspath(binary)], check=True,
            text=True, stdout=subprocess.PIPE,
        ).stdout
        symbols = sorted(set(re.findall(
            r"\b(kernel_[A-Za-z0-9_]+)(?:\.constprop\.\d+)?$",
            nm, re.M,
        )))
        for symbol in symbols:
            asm = subprocess.run(
                ["objdump", "-d", f"--disassemble={symbol}", os.fspath(binary)],
                check=True, text=True, stdout=subprocess.PIPE,
            ).stdout
            match = FORBIDDEN_ASM.search(asm)
            if match:
                asm_hits.append({
                    "binary": binary.name, "symbol": symbol,
                    "instruction": match.group(0),
                })

    print(f"source={args.source}")
    print(f"binaries={len(bins)}")
    print(f"roi_order_ok={order_ok}")
    print(f"m5_rax_clobber_ok={m5_rax_clobber_ok}")
    print(f"forbidden_source_hits={source_hits}")
    print(f"forbidden_kernel_asm_hits={asm_hits}")
    print(f"business_kernel_count={zipf_kernel_count}")
    print(f"business_zipf_call_sites={zipf_call_count}")
    print(f"business_shared_write_hits={shared_write_hits}")
    print(f"private_slots_cacheline_isolated={private_slots_isolated}")
    if (
        len(bins) != 23 or not order_ok or not m5_rax_clobber_ok
        or source_hits or asm_hits
        or zipf_kernel_count != 7 or zipf_call_count < 7
        or shared_write_hits or not private_slots_isolated
    ):
        return 2
    print("status=pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
