"""Generate a synthetic tao_trace layout so we can smoke-test end to end.

Produces:

    <root>/<workload>/tao_trace/board.processor.switchN.core.tao_trace.tao_trace.records.micro.jsonl
    <root>/<workload>/tao_trace/board.processor.switchN.core.tao_trace.tao_trace.labels.micro.jsonl

Two-core workload with a slow core and a fast core, so the epsilon-resident
scheduler exercises fast/slow splits.
"""
from __future__ import annotations

import argparse
import json
import os
import random
from typing import Iterable, List


def _rec(seq: int, op_class: int, flags: dict) -> dict:
    r = {
        "core_id": 0,
        "thread_id": 0,
        "micro_seq": seq,
        "seq_num": seq,
        "op_class": op_class,
        "is_load": flags.get("is_load", 0),
        "is_store": flags.get("is_store", 0),
        "is_atomic": flags.get("is_atomic", 0),
        "is_branch": flags.get("is_branch", 0),
        "is_branch_cond": flags.get("is_branch_cond", 0),
        "is_branch_indirect": flags.get("is_branch_indirect", 0),
        "is_call": flags.get("is_call", 0),
        "is_return": flags.get("is_return", 0),
        "branch_taken": flags.get("branch_taken", 0),
        "branch_target": flags.get("branch_target", 0),
        "branch_next_pc": flags.get("branch_next_pc", 0),
        "branch_history": flags.get("branch_history", 0),
        "is_int": flags.get("is_int", 1),
        "is_fp": flags.get("is_fp", 0),
        "is_simd": flags.get("is_simd", 0),
        "is_serialize": flags.get("is_serialize", 0),
        "is_microop": 1,
        "is_last_microop": 1,
        "n_src": 1,
        "n_dst": 1,
        "macro_pc": 0x400000 + seq * 4,
        "micro_pc": 0x400000 + seq * 4,
        "vaddr": 0,
        "paddr": 0,
        "cacheline_addr": 0,
        "cacheline_paddr": 0,
        "size": 0,
    }
    return r


def synth_workload(
    root: str,
    workload: str,
    core_profiles: List[dict],
    n_uops: int,
    tick_per_cycle: int = 500,
    seed: int = 0,
) -> str:
    rng = random.Random(seed)
    trace_dir = os.path.join(root, workload, "tao_trace")
    os.makedirs(trace_dir, exist_ok=True)
    profile_path = os.path.join(root, workload, "uarch_profile.json")
    with open(profile_path, "w", encoding="utf-8") as pf:
        json.dump({
            "core": {"num_cores": len(core_profiles), "freq_ghz": 2.0},
            "cache": {
                "l1d": {"size_b": 32768, "assoc": 8, "line_b": 64},
                "l2": {"size_b": 1048576, "assoc": 8, "line_b": 64},
                "l3": {"size_b": 16777216, "assoc": 16, "line_b": 64,
                       "num_banks": 4},
            },
            "dram": {"num_channels": 4, "banks_per_channel": 8,
                     "row_size_b": 8192, "burst_b": 64},
            "branch_predictor": {"root": {"type": "SyntheticTournamentBP"}},
        }, pf)
    roi_ends = {}
    for core_id, prof in enumerate(core_profiles):
        base_cpi = float(prof.get("cpi", 1.0))
        jitter = float(prof.get("jitter", 0.05))
        op_bias = int(prof.get("op_bias", 1))
        recs_path = os.path.join(
            trace_dir,
            f"board.processor.switch{core_id}.core.tao_trace.tao_trace.records.micro.jsonl",
        )
        lbls_path = os.path.join(
            trace_dir,
            f"board.processor.switch{core_id}.core.tao_trace.tao_trace.labels.micro.jsonl",
        )
        tick = 100_000
        with open(recs_path, "w", encoding="utf-8") as rf, open(lbls_path, "w", encoding="utf-8") as lf:
            for i in range(1, n_uops + 1):
                is_load = 1 if rng.random() < prof.get("p_load", 0.1) else 0
                is_store = 1 if (not is_load) and rng.random() < prof.get("p_store", 0.05) else 0
                is_atomic = 1 if rng.random() < prof.get("p_atomic", 0.001) else 0
                rec = _rec(i, op_bias, {
                    "is_load": is_load,
                    "is_store": is_store,
                    "is_atomic": is_atomic,
                    "is_int": 1,
                })
                rec["core_id"] = core_id
                if is_load or is_store or is_atomic:
                    # Shared deterministic physical space exercises resource
                    # equality without exposing the raw value to the model.
                    addr = 0x100000 + ((i * 64 + core_id * 4096) % (1 << 20))
                    rec["vaddr"] = addr
                    rec["paddr"] = addr
                    rec["cacheline_addr"] = addr >> 6
                    rec["cacheline_paddr"] = addr >> 6
                    rec["size"] = 8
                rf.write(json.dumps(rec))
                rf.write("\n")
                # advance tick by CPI * TPC with mild jitter
                delta_cycles = max(1.0, base_cpi * (1.0 + rng.uniform(-jitter, jitter)))
                tick += int(delta_cycles * tick_per_cycle)
                lbl = {
                    "core_id": core_id,
                    "thread_id": 0,
                    "micro_seq": i,
                    "seq_num": i,
                    "commit_tick": tick,
                }
                lf.write(json.dumps(lbl))
                lf.write("\n")
        roi_ends[core_id] = tick + tick_per_cycle
    with open(os.path.join(trace_dir, "roi_boundaries.jsonl"), "w", encoding="utf-8") as bf:
        depth = 0
        for core_id in range(len(core_profiles)):
            depth += 1
            bf.write(json.dumps({
                "event": "begin", "core_id": core_id, "workid": 0,
                "threadid": core_id, "tick": 100000, "core_depth": 1,
                "global_depth": depth,
            }) + "\n")
        for offset, core_id in enumerate(range(len(core_profiles))):
            bf.write(json.dumps({
                "event": "end", "core_id": core_id, "workid": 0,
                "threadid": core_id, "tick": roi_ends[core_id], "core_depth": 0,
                "global_depth": len(core_profiles) - offset - 1, "matched": 1,
            }) + "\n")
    return trace_dir


def synth_default(root: str, n_uops: int = 1024, seed: int = 0) -> List[str]:
    profiles_a = [
        {"cpi": 1.2, "jitter": 0.05, "op_bias": 1, "p_load": 0.15, "p_store": 0.05},
        {"cpi": 4.0, "jitter": 0.10, "op_bias": 56, "p_load": 0.40, "p_store": 0.10},
    ]
    profiles_b = [
        {"cpi": 2.0, "jitter": 0.08, "op_bias": 1, "p_load": 0.25, "p_store": 0.05},
        {"cpi": 1.3, "jitter": 0.03, "op_bias": 56, "p_load": 0.20, "p_store": 0.05},
    ]
    dirs = [
        synth_workload(root, "W_smoke_fast_slow", profiles_a, n_uops, seed=seed),
        synth_workload(root, "W_smoke_mixed", profiles_b, n_uops, seed=seed + 1),
    ]
    return dirs


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    p.add_argument("--n_uops", type=int, default=1024)
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args()
    dirs = synth_default(args.out, n_uops=args.n_uops, seed=args.seed)
    for d in dirs:
        print(d)
