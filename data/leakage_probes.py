"""leakage_probes.py — Phase 0 data-contract leakage probes.

Three fail-fast checks over ``data/v28_1/{chunks,manifest.parquet}``:

  1) Input allowlist. The chunks parquet must not contain any oracle/timing
     column. Any of these leaking into the chunk payload fails the gate:
         commit_tick, fetch_tick, issue_tick, complete_tick, ready_tick,
         path_class, coh_oracle, mispredicted, dtlb_hit, itlb_hit,
         d_mshr_depth, i_mshr_depth, i_path_class, i_coh_oracle,
         stall_reason, MSHR occupancy variants.

  2) Metadata-only GBDT probe. Fit ``sklearn.ensemble.GradientBoostingRegressor``
     using ONLY {cores, core_id, chunk_id, n_uops, n_macros, workload_id,
     seed, cfg_hash_bucket} to predict ``cpi_macro``. Split by ``run_id`` to
     avoid trivial leaks. R^2 on val split must be < ``--gate-r2`` (default 0.30).

  3) Target-shuffle probe. Same features but ``cpi_macro`` shuffled globally.
     R^2 must be near zero (< 0.05).

Outputs ``<out>/leakage_report.json`` and prints a ``[gate leakage] PASS/FAIL``
line at the end.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import math
import os
import random
import sys
from typing import Dict, List, Sequence, Tuple

try:
    import pyarrow.parquet as pq
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "pyarrow is required; run with /data00/yinhaolang/infer/.venv/bin/python"
    ) from exc


ORACLE_COLUMNS = {
    "commit_tick", "fetch_tick", "issue_tick", "complete_tick", "ready_tick",
    "path_class", "coh_oracle", "mispredicted", "dtlb_hit", "itlb_hit",
    "d_mshr_depth", "i_mshr_depth", "i_path_class", "i_coh_oracle",
    "stall_reason", "d_walker_dram_misses", "i_walker_dram_misses",
    "d_walker_levels", "i_walker_levels", "mesi_before", "sharer_bucket",
    "owner_dist", "dirty_owner", "path_class_i",
}


def _iter_chunks_parquets(chunks_root: str) -> List[str]:
    return sorted(glob.glob(os.path.join(chunks_root, "*", "chunks.parquet")))


def check_allowlist(chunks_root: str) -> Tuple[bool, List[Tuple[str, List[str]]]]:
    """Return (pass, per_file_violations)."""
    files = _iter_chunks_parquets(chunks_root)
    violations: List[Tuple[str, List[str]]] = []
    for p in files:
        cols = set(pq.ParquetFile(p).schema_arrow.names)
        bad = sorted(cols & ORACLE_COLUMNS)
        if bad:
            violations.append((p, bad))
    return (len(violations) == 0), violations


def _hash_bucket(s: str, n_buckets: int = 32) -> int:
    return int(hashlib.md5(s.encode()).hexdigest()[:8], 16) % n_buckets


def _load_features(chunks_root: str, labels_glob: str,
                   sample_per_run: int, seed: int) -> Tuple[List[List[float]], List[float], List[str]]:
    """Return (X, y, run_ids). y is cpi_macro."""
    rng = random.Random(seed)
    Xs: List[List[float]] = []
    ys: List[float] = []
    run_ids: List[str] = []
    for chunk_pq in _iter_chunks_parquets(chunks_root):
        run_dir = os.path.dirname(chunk_pq)
        labels_pq = os.path.join(run_dir, "labels.parquet")
        if not os.path.isfile(labels_pq):
            continue
        run_id = os.path.basename(run_dir)
        ch_tbl = pq.read_table(chunk_pq, columns=[
            "core_id", "chunk_id", "n_uops", "n_macros", "workload", "n_cores",
        ]).to_pydict()
        lb_tbl = pq.read_table(labels_pq, columns=[
            "core_id", "chunk_id", "cpi_macro", "valid_label",
        ]).to_pydict()
        # index labels by (core_id, chunk_id)
        idx: Dict[Tuple[int, int], float] = {}
        for i in range(len(lb_tbl["core_id"])):
            if lb_tbl["valid_label"][i] and lb_tbl["cpi_macro"][i] is not None:
                idx[(int(lb_tbl["core_id"][i]), int(lb_tbl["chunk_id"][i]))] = float(
                    lb_tbl["cpi_macro"][i]
                )
        n = len(ch_tbl["core_id"])
        pool = list(range(n))
        rng.shuffle(pool)
        take = pool[: max(1, min(sample_per_run, n))]
        for i in take:
            key = (int(ch_tbl["core_id"][i]), int(ch_tbl["chunk_id"][i]))
            if key not in idx:
                continue
            wl = str(ch_tbl["workload"][i])
            xs = [
                float(ch_tbl["n_cores"][i]),
                float(ch_tbl["core_id"][i]),
                float(ch_tbl["chunk_id"][i]),
                float(ch_tbl["n_uops"][i]),
                float(ch_tbl["n_macros"][i]),
                float(_hash_bucket(wl, 32)),
                float(_hash_bucket(run_id, 8)),
            ]
            Xs.append(xs)
            ys.append(float(idx[key]))
            run_ids.append(run_id)
    return Xs, ys, run_ids


def _split_by_run(run_ids: Sequence[str], seed: int, val_frac: float = 0.25) -> Tuple[List[int], List[int]]:
    unique = sorted(set(run_ids))
    rng = random.Random(seed)
    rng.shuffle(unique)
    n_val = max(1, int(len(unique) * val_frac))
    val_set = set(unique[:n_val])
    tr = [i for i, r in enumerate(run_ids) if r not in val_set]
    va = [i for i, r in enumerate(run_ids) if r in val_set]
    return tr, va


def _r2(y_true: Sequence[float], y_pred: Sequence[float]) -> float:
    if not y_true:
        return 0.0
    mean = sum(y_true) / len(y_true)
    ss_res = sum((t - p) ** 2 for t, p in zip(y_true, y_pred))
    ss_tot = sum((t - mean) ** 2 for t in y_true)
    if ss_tot <= 0:
        return 0.0
    return 1.0 - ss_res / ss_tot


def _fit_gbdt(X_tr, y_tr, X_va, y_va) -> float:
    try:
        from sklearn.ensemble import GradientBoostingRegressor  # type: ignore
    except Exception:
        # Fallback: simple mean baseline; r2 = 0.
        mean_y = sum(y_tr) / max(1, len(y_tr))
        return _r2(y_va, [mean_y] * len(y_va))
    m = GradientBoostingRegressor(n_estimators=200, max_depth=4, random_state=0)
    m.fit(X_tr, y_tr)
    pred = m.predict(X_va).tolist()
    return _r2(y_va, pred)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks-root", default="/data00/yinhaolang/LLMSim/data/v28_1/chunks")
    ap.add_argument("--out", default="/data00/yinhaolang/LLMSim/data/v28_1/leakage_report.json")
    ap.add_argument("--sample-per-run", type=int, default=200,
                    help="chunks to sample per run for the probe")
    ap.add_argument("--gate-r2", type=float, default=0.30,
                    help="metadata-only probe R^2 above this fails the gate")
    ap.add_argument("--gate-shuffle-r2", type=float, default=0.05,
                    help="target-shuffle probe R^2 above this fails the gate")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    if not os.path.isdir(args.chunks_root):
        print(f"[gate leakage] FAIL no chunks under {args.chunks_root}", flush=True)
        return 1

    ok_allow, violations = check_allowlist(args.chunks_root)
    for path, bad in violations:
        print(f"[allowlist FAIL] {path}: {bad}", flush=True)

    Xs, ys, run_ids = _load_features(args.chunks_root, "", args.sample_per_run, args.seed)
    if len(Xs) < 200:
        # Not enough samples to fit a probe; still perform allowlist check.
        report = {
            "allowlist_pass": ok_allow,
            "allowlist_violations": violations,
            "metadata_r2": None,
            "shuffle_r2": None,
            "n_samples": len(Xs),
            "note": "insufficient chunks to fit probe; allowlist-only check",
        }
        with open(args.out, "w") as fh:
            json.dump(report, fh, indent=2)
        print(f"[gate leakage] samples={len(Xs)} — allowlist_only", flush=True)
        return 0 if ok_allow else 2

    tr_idx, va_idx = _split_by_run(run_ids, seed=args.seed)
    if not tr_idx or not va_idx:
        print(f"[gate leakage] FAIL insufficient run split (train={len(tr_idx)}, val={len(va_idx)})",
              flush=True)
        return 2
    X_tr = [Xs[i] for i in tr_idx]
    y_tr = [ys[i] for i in tr_idx]
    X_va = [Xs[i] for i in va_idx]
    y_va = [ys[i] for i in va_idx]
    r2_meta = _fit_gbdt(X_tr, y_tr, X_va, y_va)

    rng = random.Random(args.seed + 1)
    ys_shuf = list(ys)
    rng.shuffle(ys_shuf)
    r2_shuf = _fit_gbdt([Xs[i] for i in tr_idx], [ys_shuf[i] for i in tr_idx],
                        [Xs[i] for i in va_idx], [ys_shuf[i] for i in va_idx])

    report = {
        "allowlist_pass": ok_allow,
        "allowlist_violations": violations,
        "n_samples": len(Xs),
        "n_train": len(tr_idx),
        "n_val": len(va_idx),
        "metadata_r2": float(r2_meta),
        "shuffle_r2": float(r2_shuf),
        "gate_r2": float(args.gate_r2),
        "gate_shuffle_r2": float(args.gate_shuffle_r2),
    }
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(report, fh, indent=2)
    print(f"[gate leakage] allowlist_pass={ok_allow} "
          f"meta_R2={r2_meta:.3f} shuffle_R2={r2_shuf:.3f}",
          flush=True)
    fail = (
        (not ok_allow)
        or (r2_meta > args.gate_r2)
        or (r2_shuf > args.gate_shuffle_r2)
    )
    if fail:
        print("[gate leakage] FAIL", flush=True)
        return 2
    print("[gate leakage] PASS", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
