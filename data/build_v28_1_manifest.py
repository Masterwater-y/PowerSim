"""build_v28_1_manifest.py — v28_1 run/window manifest with strict split labels.

Produces two parquet files under ``<out>/``:

  manifest.parquet (one row per run_id)
    run_id, workload, seed, cores, seed_dir, workload_dir,
    binary_name, binary_hash, uarch_hash, tick_per_cycle,
    sim_commit, warm_policy, records_per_core_target, split, split_reason,
    heldout, chunks_dir, n_chunks

  window_manifest.parquet (one row per (run_id, core_id, chunk_id))
    run_id, workload, seed, cores, core_id, chunk_id,
    n_uops, n_macros, split, split_reason

Split policy (matches docs/LLM语义建模方案.md §5.1):
  seed==0 && workload ∈ Train16               -> train         (includes c32)
  seed==0 && workload ∈ Heldout7              -> family_ood
  seed==1 && workload ∈ Train16               -> seed_ood
  seed==1 && workload ∈ Heldout7              -> sealed_joint_ood

The manifest is written independently of the chunks (does not require chunks
to exist yet), so it can be built early. If chunks are already built, n_chunks
is populated from chunks/labels parquet.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "pyarrow is required; run with /data00/yinhaolang/infer/.venv/bin/python"
    ) from exc


TRAIN_16 = [
    "W_v28_int_alu_dense", "W_v28_int_div_serial",
    "W_v28_fp_alu_dense", "W_v28_simd_sse_dense",
    "W_v28_cache_L1_mixed", "W_v28_cache_L2_mixed",
    "W_v28_memory_seq_moderate", "W_v28_memory_random_mlp",
    "W_v28_coh_readmostly_sparse",
    "W_v28_marine_base", "W_v28_gofeed_base", "W_v28_flink_base",
    "W_v28_mysql_base", "W_v28_redis_base", "W_v28_pytorch_base",
    "W_v28_bvc_encoder_base",
]
HELDOUT_7 = [
    "W_v28_marine_heldout", "W_v28_gofeed_heldout", "W_v28_flink_heldout",
    "W_v28_mysql_heldout", "W_v28_redis_heldout", "W_v28_pytorch_heldout",
    "W_v28_bvc_encoder_heldout",
]


def _split_for(seed: int, workload: str) -> Tuple[str, str, bool]:
    heldout = workload in HELDOUT_7
    if seed == 0 and workload in TRAIN_16:
        return "train", "seed0_train16", False
    if seed == 0 and heldout:
        return "family_ood", "seed0_heldout7", True
    if seed == 1 and workload in TRAIN_16:
        return "seed_ood", "seed1_train16", False
    if seed == 1 and heldout:
        return "sealed_joint_ood", "seed1_heldout7", True
    return "unknown", "unknown", heldout


def _sha1_str(*parts: str) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(str(p).encode())
        h.update(b"|")
    return h.hexdigest()


def _load_uarch_hash(trace_dir: str) -> Tuple[str, float]:
    prof_path = os.path.join(trace_dir, "..", "uarch_profile.json")
    prof_path = os.path.normpath(prof_path)
    if not os.path.isfile(prof_path):
        return "", 0.0
    with open(prof_path, "r") as fh:
        prof = json.load(fh)
    tpc = 0.0
    try:
        freq_ghz = float(prof.get("core", {}).get("freq_ghz") or 0.0)
        # gem5's default clock ratio: tick 1ps == 1e-12s; cycle = 1/(freq_ghz*1e9)s
        if freq_ghz > 0:
            tpc = 1e12 / (freq_ghz * 1e9)  # ticks per cycle
    except Exception:
        pass
    h = hashlib.sha1(json.dumps(prof, sort_keys=True).encode()).hexdigest()
    return h, tpc


def _load_collect_meta(raw_workload_dir: str) -> Dict[str, Any]:
    p = os.path.join(raw_workload_dir, "collect.meta")
    out: Dict[str, Any] = {}
    if not os.path.isfile(p):
        return out
    with open(p, "r") as fh:
        for line in fh:
            line = line.strip()
            if not line or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _binary_hash_lookup(static_dict_manifest: Optional[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not static_dict_manifest or not os.path.isfile(static_dict_manifest):
        return out
    with open(static_dict_manifest, "r") as fh:
        for line in fh:
            r = json.loads(line)
            name = str(r.get("binary_name") or "")
            h = str(r.get("binary_hash") or "")
            if name and h:
                out[name] = h
    return out


def _chunks_stats(chunks_root: str, run_id: str) -> Tuple[Optional[str], int, List[Dict[str, int]]]:
    chunks_dir = os.path.join(chunks_root, run_id)
    chunks_path = os.path.join(chunks_dir, "chunks.parquet")
    if not os.path.isfile(chunks_path):
        return None, 0, []
    tbl = pq.read_table(
        chunks_path,
        columns=["core_id", "chunk_id", "n_uops", "n_macros"],
    )
    core_ids = tbl["core_id"].to_pylist()
    chunk_ids = tbl["chunk_id"].to_pylist()
    n_uops = tbl["n_uops"].to_pylist()
    n_macros = tbl["n_macros"].to_pylist()
    rows = [
        {
            "core_id": int(core_ids[i]),
            "chunk_id": int(chunk_ids[i]),
            "n_uops": int(n_uops[i]),
            "n_macros": int(n_macros[i]),
        }
        for i in range(len(core_ids))
    ]
    return chunks_dir, len(rows), rows


def _iter_runs(raw_root: str, prefix: str, seeds: Sequence[int],
               cores: Sequence[str]) -> Iterator[Tuple[int, str, str, str]]:
    for seed in seeds:
        for core_str in cores:
            root = os.path.join(raw_root, f"{prefix}_seed{seed}_c{core_str}")
            if not os.path.isdir(root):
                continue
            for name in sorted(os.listdir(root)):
                if not name.startswith("W_v28_"):
                    continue
                p = os.path.join(root, name)
                if not os.path.isdir(os.path.join(p, "tao_trace")):
                    continue
                yield seed, core_str, name, p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-root", default="/data00/yinhaolang/TSim/data")
    ap.add_argument("--raw-prefix", default="raw_v28_1_business_a2_sharedzipf")
    ap.add_argument("--seeds", default="0,1")
    ap.add_argument("--cores", default="01,04,08,16,32")
    ap.add_argument("--chunks-root", default="/data00/yinhaolang/LLMSim/data/v28_1/chunks")
    ap.add_argument("--static-dict-manifest",
                    default="/data00/yinhaolang/LLMSim/data/v28_1/static_dict/manifest.jsonl")
    ap.add_argument("--out", default="/data00/yinhaolang/LLMSim/data/v28_1")
    args = ap.parse_args()

    seeds = [int(s) for s in args.seeds.split(",") if s]
    cores = [c.strip() for c in args.cores.split(",") if c.strip()]
    bhash_map = _binary_hash_lookup(args.static_dict_manifest)

    manifest_rows: List[dict] = []
    window_rows: List[dict] = []
    split_counts: Dict[str, int] = {}
    for seed, core_str, workload, raw_dir in _iter_runs(args.raw_root, args.raw_prefix,
                                                        seeds, cores):
        run_id = f"v28_1_a2_sharedzipf_seed{seed}_c{core_str}_{workload}"
        split, reason, heldout = _split_for(seed, workload)
        split_counts[split] = split_counts.get(split, 0) + 1
        meta = _load_collect_meta(raw_dir)
        trace_dir = os.path.join(raw_dir, "tao_trace")
        uarch_h, tpc = _load_uarch_hash(trace_dir)
        binary_name = workload.removeprefix("W_")
        binary_hash = bhash_map.get(binary_name, "")
        chunks_dir, n_chunks, chunk_rows = _chunks_stats(args.chunks_root, run_id)
        manifest_rows.append({
            "run_id": run_id,
            "workload": workload,
            "seed": int(seed),
            "cores": int(core_str),
            "seed_dir": f"{args.raw_prefix}_seed{seed}_c{core_str}",
            "workload_dir": raw_dir,
            "binary_name": binary_name,
            "binary_hash": binary_hash,
            "uarch_hash": uarch_h,
            "tick_per_cycle": float(tpc),
            "sim_commit": str(meta.get("sim_commit") or ""),
            "warm_policy": str(meta.get("ff_atomic") or ""),
            "records_per_core_target": int(meta.get("target_per_core", "0") or 0),
            "split": split,
            "split_reason": reason,
            "heldout": bool(heldout),
            "chunks_dir": chunks_dir or "",
            "n_chunks": int(n_chunks),
        })
        for r in chunk_rows:
            window_rows.append({
                "run_id": run_id,
                "workload": workload,
                "seed": int(seed),
                "cores": int(core_str),
                "core_id": int(r["core_id"]),
                "chunk_id": int(r["chunk_id"]),
                "n_uops": int(r["n_uops"]),
                "n_macros": int(r["n_macros"]),
                "split": split,
                "split_reason": reason,
            })

    os.makedirs(args.out, exist_ok=True)
    manifest_path = os.path.join(args.out, "manifest.parquet")
    windows_path = os.path.join(args.out, "window_manifest.parquet")

    if not manifest_rows:
        print("[build_v28_1_manifest] no runs found", flush=True)
        return 1

    manifest_cols = {k: [r.get(k) for r in manifest_rows] for k in manifest_rows[0].keys()}
    tbl_m = pa.table(manifest_cols)
    tmp_m = manifest_path + ".tmp"
    pq.write_table(tbl_m, tmp_m, compression="zstd")
    os.replace(tmp_m, manifest_path)

    if window_rows:
        window_cols = {k: [r.get(k) for r in window_rows] for k in window_rows[0].keys()}
        tbl_w = pa.table(window_cols)
        tmp_w = windows_path + ".tmp"
        pq.write_table(tbl_w, tmp_w, compression="zstd")
        os.replace(tmp_w, windows_path)
    print(f"[build_v28_1_manifest] {len(manifest_rows)} runs -> {manifest_path}",
          flush=True)
    print(f"[build_v28_1_manifest] {len(window_rows)} windows -> {windows_path}",
          flush=True)
    for split, n in sorted(split_counts.items()):
        print(f"  split[{split}] runs={n}", flush=True)
    # Split contract sanity: no run may be labelled 'unknown'.
    unknown = [r for r in manifest_rows if r["split"] == "unknown"]
    if unknown:
        for r in unknown[:5]:
            print(f"[gate manifest] FAIL unknown split: {r['run_id']}", flush=True)
        return 2
    print("[gate manifest] PASS (all runs assigned a valid split)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
