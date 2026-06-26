"""P0 per-op oracle vs ground-truth diagnostic.

Goal: explain *where* the shared_system oracle disagrees with the gem5
tao_trace ground-truth ``path_class`` on a per-op basis, split by load/store.

The aggregate harness (:mod:`scripts.oracle_warmup_ab`) only compares miss
*rates*; it cannot tell whether the ~93% ``mr_l1d_ld`` error is a few badly
misclassified hot lines or a systematic shift. This script:

1. Reuses the harness event collection / jsonl emission to build the exact
   same committed-only, commit_tick-ordered input the oracle consumes.
2. Runs ``llmsim_shared_system`` with ``--emit-per-op=<sink>`` so the oracle
   echoes its per-op decision keyed by ``(core_id, micro_seq)``.
3. Joins those decisions against the parquet ground-truth ``path_class``
   (same key) and prints, separately for loads and stores:
     - a level confusion matrix (truth level x oracle level),
     - the L1-hit/L1-miss confusion (the quantity that drives ``mr_l1d_ld``),
     - among L1-hit-in-truth-but-miss-in-oracle ops, how warm the line was in
       truth's LLC set (proxy for "speculatively warmed" lines).

Levels use the project path_class encoding: 0=L1 hit, 1=L2, 2=LLC, 3=NoC
(remote), 4=DRAM. "L1 miss" == path_class >= 1.

Usage::

    /root/miniconda3/envs/yinhaolang/bin/python scripts/oracle_per_op_diag.py \
        --workload W_ads_ctr --warmup-dt 0
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

import pyarrow.parquet as pq

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.roi_stats import load_workload_rows  # noqa: E402
from scripts.oracle_warmup_ab import (  # noqa: E402
    SHARED_BIN,
    SHARED_PROFILE,
    UARCH_CFG,
    collect_oracle_events,
    emit_oracle_jsonl,
    load_tpc,
)

ROOT = Path("/data00/yinhaolang/LLMSim")
PC_NAME = {0: "L1hit", 1: "L2", 2: "LLC", 3: "NoC", 4: "DRAM"}

# Parquet columns we need for the join + truth labels.
TRUTH_COLS = [
    "core_id", "micro_seq", "is_load", "is_store", "is_atomic",
    "path_class", "coh_oracle", "paddr", "cacheline_paddr", "commit_tick",
]
# Optional LLC-set warmth columns (present in v10.3+ traces); used to test the
# "speculatively warmed line" hypothesis. Missing columns are skipped.
OPT_COLS = ["d_llc_set_residency", "d_llc_set_lru_pos"]


def run_oracle_per_op(events_path: Path, per_op_path: Path,
                      snap_path: Path) -> None:
    """Invoke the oracle binary with the per-op sink enabled."""
    env = os.environ.copy()
    try:
        lib = subprocess.check_output(
            ["g++", "-print-file-name=libstdc++.so.6"], text=True).strip()
    except Exception:
        lib = ""
    if lib and lib != "libstdc++.so.6":
        libdir = str(Path(lib).resolve().parent)
        env["LD_LIBRARY_PATH"] = (
            f"{libdir}:{env['LD_LIBRARY_PATH']}"
            if env.get("LD_LIBRARY_PATH") else libdir
        )
    cmd = [
        str(SHARED_BIN), str(SHARED_PROFILE), str(events_path), str(snap_path),
        f"--emit-per-op={per_op_path}",
    ]
    subprocess.run(cmd, check=True, env=env)


def load_truth(trace_dir: Path) -> Dict[Tuple[int, int], dict]:
    """Read parquet truth keyed by (core_id, micro_seq) for mem ops only."""
    truth: Dict[Tuple[int, int], dict] = {}
    files = sorted(trace_dir.glob("*.aligned.parquet"))
    if not files:
        raise RuntimeError(f"{trace_dir}: no aligned parquet")
    for fp in files:
        pf = pq.ParquetFile(str(fp))
        avail = set(pf.schema_arrow.names)
        cols = [c for c in TRUTH_COLS if c in avail]
        cols += [c for c in OPT_COLS if c in avail]
        for batch in pf.iter_batches(columns=cols, batch_size=65536):
            d = {name: batch.column(name).to_pylist()
                 for name in batch.schema.names}
            n = batch.num_rows
            for i in range(n):
                is_ld = int(d["is_load"][i] or 0)
                is_st = int(d["is_store"][i] or 0)
                is_at = int(d["is_atomic"][i] or 0)
                if not (is_ld or is_st or is_at):
                    continue
                cid = int(d["core_id"][i] or 0)
                ms = int(d["micro_seq"][i] or 0)
                row = {
                    "is_load": is_ld,
                    "is_store": int(is_st or is_at),
                    "path_class": int(d["path_class"][i] or 0),
                    "coh": int(d.get("coh_oracle", [0] * n)[i] or 0),
                    "paddr": int(d.get("paddr", [0] * n)[i] or 0),
                }
                for oc in OPT_COLS:
                    if oc in d:
                        row[oc] = int(d[oc][i] or 0)
                truth[(cid, ms)] = row
    return truth


def load_oracle(per_op_path: Path) -> Dict[Tuple[int, int], dict]:
    """Read oracle per-op decisions keyed by (core_id, micro_seq)."""
    oracle: Dict[Tuple[int, int], dict] = {}
    with open(per_op_path) as f:
        for line in f:
            if not line or line[0] != "{":
                continue
            o = json.loads(line)
            oracle[(int(o["core_id"]), int(o["micro_seq"]))] = o
    return oracle


def fmt_matrix(title: str, mat: Dict[Tuple[int, int], int],
               total: int) -> str:
    levels = [0, 1, 2, 3, 4]
    out = [f"\n{title}  (rows=truth, cols=oracle; N={total})"]
    header = "truth\\oracle  " + "".join(
        f"{PC_NAME[c]:>8}" for c in levels) + f"{'rowsum':>9}"
    out.append(header)
    for r in levels:
        rowsum = sum(mat.get((r, c), 0) for c in levels)
        cells = "".join(f"{mat.get((r, c), 0):>8}" for c in levels)
        out.append(f"{PC_NAME[r]:>12}  {cells}{rowsum:>9}")
    colsum = "".join(
        f"{sum(mat.get((r, c), 0) for r in levels):>8}" for c in levels)
    out.append(f"{'colsum':>12}  {colsum}{total:>9}")
    return "\n".join(out)


def analyze(workload: str, warmup_dt: int, raw_root: Path,
            out_dir: Path) -> dict:
    tpc = load_tpc("arch_A")
    trace_dir = raw_root / workload / "tao_trace"
    print(f"[{workload}] loading rows from {trace_dir} ...")
    merged = load_workload_rows(str(trace_dir))

    events = collect_oracle_events(merged)
    # split tick mirrors oracle_warmup_ab.main
    first_ticks = []
    for seq in merged.values():
        for r in seq:
            ct = int(r.get("_commit_tick", r.get("commit_tick", 0)) or 0)
            if ct > 0:
                first_ticks.append(ct)
                break
    split_tick = 0
    if warmup_dt > 0 and first_ticks:
        split_tick = max(first_ticks) + warmup_dt * tpc

    out_dir.mkdir(parents=True, exist_ok=True)
    events_path = out_dir / f"{workload}.warmup{warmup_dt}.mem_events.jsonl"
    snap_path = out_dir / f"{workload}.warmup{warmup_dt}.shared_pmu.jsonl"
    per_op_path = out_dir / f"{workload}.warmup{warmup_dt}.per_op.jsonl"

    warm, roi = emit_oracle_jsonl(events, workload, split_tick, tpc, events_path)
    print(f"[{workload}] events: warmup={warm} roi={roi} split_tick={split_tick}")

    run_oracle_per_op(events_path, per_op_path, snap_path)

    truth = load_truth(trace_dir)
    oracle = load_oracle(per_op_path)
    print(f"[{workload}] truth mem ops={len(truth)} oracle ops={len(oracle)}")

    # Restrict comparison to ROI ops (commit_tick >= split_tick), matching the
    # rate harness. We get commit_tick from the merged rows via the join key.
    roi_keys = set()
    for c, seq in merged.items():
        for r in seq:
            is_ld = int(r.get("is_load", 0) or 0)
            is_st = int(r.get("is_store", 0) or 0)
            is_at = int(r.get("is_atomic", 0) or 0)
            if not (is_ld or is_st or is_at):
                continue
            ct = int(r.get("_commit_tick", r.get("commit_tick", 0)) or 0)
            if ct <= 0:
                continue
            if split_tick > 0 and ct < split_tick:
                continue
            roi_keys.add((int(c), int(r.get("micro_seq", 0) or 0)))

    keys = roi_keys & set(truth.keys()) & set(oracle.keys())
    missing_oracle = len(roi_keys & set(truth.keys())) - len(keys)
    print(f"[{workload}] joined ROI ops={len(keys)} "
          f"(unmatched in oracle={missing_oracle})")

    # Confusion matrices split by op kind.
    mat = {"load": defaultdict(int), "store": defaultdict(int)}
    # L1 hit/miss confusion (drives mr_l1d_ld / mr_l1d_st).
    l1 = {"load": Counter(), "store": Counter()}  # keys: (truth_miss, orc_miss)
    # Among truth-L1hit but oracle-L1miss: warmth of truth LLC set.
    warm_hist = Counter()
    warm_examples = []
    counts = {"load": 0, "store": 0}
    for k in keys:
        t = truth[k]
        o = oracle[k]
        kind = "load" if t["is_load"] else "store"
        counts[kind] += 1
        tpc_l = t["path_class"]
        opc = int(o["path_class"])
        mat[kind][(tpc_l, opc)] += 1
        tmiss = 1 if tpc_l >= 1 else 0
        omiss = 1 if opc >= 1 else 0
        l1[kind][(tmiss, omiss)] += 1
        if kind == "load" and tmiss == 0 and omiss == 1:
            res = t.get("d_llc_set_residency")
            if res is not None:
                warm_hist[res] += 1
            if len(warm_examples) < 5:
                warm_examples.append({"key": k, "truth": t, "oracle": o})

    lines: List[str] = []
    lines.append(f"==== {workload}  warmup_dt={warmup_dt}  "
                 f"joined_roi_ops={len(keys)} ====")
    for kind in ("load", "store"):
        n = counts[kind]
        if n == 0:
            continue
        c = l1[kind]
        tp_hit_o_hit = c[(0, 0)]
        tp_hit_o_miss = c[(0, 1)]
        tp_miss_o_hit = c[(1, 0)]
        tp_miss_o_miss = c[(1, 1)]
        truth_miss = tp_miss_o_hit + tp_miss_o_miss
        orc_miss = tp_hit_o_miss + tp_miss_o_miss
        truth_mr = truth_miss / n if n else 0.0
        orc_mr = orc_miss / n if n else 0.0
        relerr = abs(orc_mr - truth_mr) / truth_mr if truth_mr else float("nan")
        lines.append(
            f"\n-- {kind.upper()}  N={n}  "
            f"truth_mr={truth_mr:.4%}  oracle_mr={orc_mr:.4%}  "
            f"relerr={relerr:.2%}")
        lines.append(
            f"   L1 confusion: "
            f"truthHIT&orcHIT={tp_hit_o_hit}  "
            f"truthHIT&orcMISS={tp_hit_o_miss}  "
            f"truthMISS&orcHIT={tp_miss_o_hit}  "
            f"truthMISS&orcMISS={tp_miss_o_miss}")
        net = tp_hit_o_miss - tp_miss_o_hit
        lines.append(
            f"   => oracle over-counts L1 miss by net {net} "
            f"({net / n:+.4%} of ops); "
            f"false-miss={tp_hit_o_miss} false-hit={tp_miss_o_hit}")
        lines.append(fmt_matrix(f"   {kind} level confusion", mat[kind], n))

    if warm_hist:
        total_fm = sum(warm_hist.values())
        lines.append(
            f"\n-- LOAD false-miss (truthL1hit, oracleL1miss) LLC-set "
            f"residency histogram  (N={total_fm}):")
        for res in sorted(warm_hist):
            lines.append(f"   residency={res:>2}: {warm_hist[res]} "
                         f"({warm_hist[res] / total_fm:.1%})")

    report = "\n".join(lines)
    print(report)

    rpt_path = out_dir / f"{workload}.warmup{warmup_dt}.per_op_diag.txt"
    rpt_path.write_text(report + "\n")
    print(f"\n[{workload}] report -> {rpt_path}")

    def l1_to_str(counter: Counter) -> dict:
        names = {(0, 0): "truthHIT_orcHIT", (0, 1): "truthHIT_orcMISS",
                 (1, 0): "truthMISS_orcHIT", (1, 1): "truthMISS_orcMISS"}
        return {names[k]: v for k, v in counter.items()}

    return {
        "workload": workload,
        "warmup_dt": warmup_dt,
        "joined_roi_ops": len(keys),
        "load": l1_to_str(l1["load"]),
        "store": l1_to_str(l1["store"]),
        "report_path": str(rpt_path),
    }


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workload", nargs="*", default=["W_ads_ctr"])
    ap.add_argument("--warmup-dt", type=int, nargs="*", default=[0])
    ap.add_argument("--raw-root", default=str(ROOT / "data/raw_eval11_8c"))
    ap.add_argument("--out-dir",
                    default=str(ROOT / "logs/oracle_per_op_diag"))
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    raw_root = Path(args.raw_root)
    out_dir = Path(args.out_dir)
    summary = []
    for w in args.workload:
        for dt in args.warmup_dt:
            summary.append(analyze(w, dt, raw_root, out_dir))
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print(f"\nsummary -> {out_dir / 'summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
