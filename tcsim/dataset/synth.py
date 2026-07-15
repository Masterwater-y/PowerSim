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
        "is_int": flags.get("is_int", 1),
        "is_fp": flags.get("is_fp", 0),
        "is_simd": flags.get("is_simd", 0),
        "is_serialize": flags.get("is_serialize", 0),
        "is_microop": 1,
        "is_last_microop": 1,
        "n_src": 1,
        "n_dst": 1,
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
