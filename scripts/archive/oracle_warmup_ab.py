"""Oracle warmup A/B: bypass the model and feed shared_system with the ground-
truth global memory queue derived from trace ``commit_tick``.

For each workload we:

1. Load aligned parquet rows from every core (see :mod:`data.roi_stats`).
2. Collect all mem ops (``is_load | is_store | is_atomic``) and sort them by
   ``commit_tick`` to obtain the gem5-true global access order.
3. Optionally split the queue into a pre-ROI warmup prefix and an ROI tail at
   the global tick ``T = max_c(first_valid_tick[c]) + warmup_dt * tpc``.
4. Emit a single ``<workload>.warmup<dt>.mem_events.jsonl`` per (workload, dt).
   When ``dt > 0`` we insert a ``{"event_type":"roi_begin", ...}`` marker at
   the split point so shared_system drops the warmup-induced counter delta.
5. Invoke ``llmsim_shared_system`` once per file and parse the final PMU
   snapshot. The cold (dt=0) and warm (dt>0) snapshots are compared against
   the trace ground truth from :func:`data.build_windows.aggregate_pmu`.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.build_windows import aggregate_pmu  # noqa: E402
from data.roi_stats import load_workload_rows  # noqa: E402
from scripts.stats_truth import load_real_stats  # noqa: E402


ROOT = Path("/data00/yinhaolang/LLMSim")
SHARED_BIN = ROOT / "shared_system/mesi_ref_sim/build/llmsim_shared_system"
SHARED_PROFILE = ROOT / "config/uarch_profile_arch_A.json"
UARCH_CFG = ROOT / "config/uarch_configs.yaml"
PMU_RATE_KEYS = ("mr_llc", "mr_l1d_ld", "mr_l1d_st")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-root", default=str(ROOT / "data/raw_eval11_8c"))
    ap.add_argument("--workload", action="append", default=[],
                    help="可多次；默认 3 个 holdout")
    ap.add_argument("--out-dir", required=True,
                    help="输出 mem_events.jsonl 与 shared_pmu.jsonl 的目录")
    ap.add_argument("--uarch-config", default="arch_A")
    ap.add_argument("--warmup-dt", type=int, action="append", default=[],
                    help="warmup-dt 列表（cycle），>=0；默认 0 和 100000")
    ap.add_argument("--shared-binary", default=str(SHARED_BIN))
    ap.add_argument("--shared-profile", default=str(SHARED_PROFILE))
    ap.add_argument("--warm-model", action="append", default=[],
                    choices=["none", "nextline", "mispred-shadow"],
                    help="可多次；默认 none。比较不同投机焐热策略")
    ap.add_argument("--mispred-depth-K", type=int, action="append", default=[],
                    help="可多次；mispred-shadow 每次触发焐热的最近 K 条 load，"
                         "0=不触发，默认 16（满 ring）。仅对 mispred-shadow 生效。")
    ap.add_argument("--shadow-stride-mode", action="append", default=[],
                    choices=["fixed64", "stride", "auto"],
                    help="可多次；影子焐热的地址偏移策略。"
                         "fixed64=cl+64（向后兼容默认），"
                         "stride=用 per-core stride detector 学到的步长（低置信跳过），"
                         "auto=学到时用 stride 否则回退 +64。")
    return ap.parse_args()


def load_tpc(uarch_name: str) -> int:
    with open(UARCH_CFG) as f:
        cfg = yaml.safe_load(f)
    return int(cfg["configs"][uarch_name].get("tick_per_cycle", 333))


def _is_mem(rec: dict) -> bool:
    return bool(int(rec.get("is_load", 0) or 0)
                or int(rec.get("is_store", 0) or 0)
                or int(rec.get("is_atomic", 0) or 0))


def _first_valid_tick(seq: List[dict]) -> int:
    for r in seq:
        ct = int(r.get("_commit_tick", r.get("commit_tick", 0)) or 0)
        if ct > 0:
            return ct
    return 0


def compute_split_tick(merged: Dict[int, List[dict]],
                       warmup_dt: int, tpc: int) -> int:
    if warmup_dt <= 0:
        return 0
    firsts = [_first_valid_tick(seq) for seq in merged.values()]
    firsts = [t for t in firsts if t > 0]
    if not firsts:
        return 0
    return max(firsts) + warmup_dt * tpc


def collect_oracle_events(merged: Dict[int, List[dict]]) -> List[Tuple[int, int, int, dict]]:
    """Return events as ``(commit_tick, core_id, micro_seq, obj)``.

    Includes:
      - all committed mem ops (event_type=mem)
      - mispredicted branches (event_type=mispred), as a gating signal for
        the speculative warm-up model in shared_system; carries no addr/size.

    Sorting is global: ascending by commit_tick, then core_id then micro_seq.
    """
    events: List[Tuple[int, int, int, dict]] = []
    for c, seq in merged.items():
        for idx, rec in enumerate(seq):
            ct = int(rec.get("_commit_tick", rec.get("commit_tick", 0)) or 0)
            if ct <= 0:
                continue
            micro_seq = int(rec.get("micro_seq", idx) or idx)
            if _is_mem(rec):
                paddr = int(rec.get("paddr", 0) or 0)
                cl_paddr = int(rec.get("cacheline_paddr", 0) or 0)
                if paddr == 0:
                    paddr = cl_paddr or int(rec.get("cacheline_addr", 0) or 0)
                if cl_paddr == 0:
                    cl_paddr = paddr & ~63
                size = int(rec.get("size", 0) or 0)
                if size <= 0:
                    size = 8
                obj = {
                    "event_type": "mem",
                    "core_id": int(c),
                    "thread_id": int(rec.get("thread_id", c) or c),
                    "paddr": paddr,
                    "cacheline_paddr": cl_paddr,
                    "cacheline_addr": cl_paddr,
                    "is_load": int(rec.get("is_load", 0) or 0),
                    "is_store": int(rec.get("is_store", 0) or 0),
                    "is_atomic": int(rec.get("is_atomic", 0) or 0),
                    "size": size,
                    "micro_seq": micro_seq,
                }
                events.append((ct, int(c), micro_seq, obj))
                continue
            # Mispredicted branch row: emit a gating event.
            if int(rec.get("is_branch", 0) or 0) and int(rec.get("mispredicted", 0) or 0):
                obj = {
                    "event_type": "mispred",
                    "core_id": int(c),
                    "thread_id": int(rec.get("thread_id", c) or c),
                    "is_branch_cond": int(rec.get("is_branch_cond", 0) or 0),
                    "is_branch_indirect": int(rec.get("is_branch_indirect", 0) or 0),
                    "micro_seq": micro_seq,
                }
                events.append((ct, int(c), micro_seq, obj))
    events.sort(key=lambda x: (x[0], x[1], x[2]))
    return events


def emit_oracle_jsonl(events: List[Tuple[int, int, int, dict]],
                      workload: str,
                      split_tick: int,
                      tpc: int,
                      out_path: Path) -> Tuple[int, int]:
    """Write JSONL; return ``(warmup_count, roi_count)``."""
    warm = roi = 0
    seq_id = 0
    inserted_marker = split_tick <= 0
    with open(out_path, "w") as f:
        for ct, _, _, obj in events:
            if not inserted_marker and ct >= split_tick:
                f.write(json.dumps({
                    "event_type": "roi_begin",
                    "workload": workload,
                    "split_tick": split_tick,
                    "split_cycle": split_tick / float(tpc),
                }, separators=(",", ":")))
                f.write("\n")
                inserted_marker = True
            obj["workload"] = workload
            obj["window"] = -1
            obj["t_pred_cycle"] = float(ct) / float(tpc)
            obj["seq"] = seq_id
            seq_id += 1
            f.write(json.dumps(obj, separators=(",", ":")))
            f.write("\n")
            if split_tick > 0 and ct < split_tick:
                warm += 1
            else:
                roi += 1
        f.write(json.dumps({
            "event_type": "snapshot",
            "workload": workload,
            "reason": "final",
        }, separators=(",", ":")))
        f.write("\n")
    return warm, roi


def run_shared_system(binary: str, profile: str,
                      events_path: Path, snap_path: Path,
                      warm_model: str = "none",
                      mispred_depth_K: int = 16,
                      shadow_stride_mode: str = "fixed64") -> None:
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
    cmd = [binary, profile, str(events_path), str(snap_path),
           f"--warm-model={warm_model}",
           f"--mispred-depth-K={mispred_depth_K}",
           f"--shadow-stride-mode={shadow_stride_mode}"]
    subprocess.run(cmd, check=True, env=env)


def parse_final_rates(snap_path: Path) -> Optional[dict]:
    last = None
    with open(snap_path) as f:
        for ln in f:
            ln = ln.strip()
            if not ln.startswith("{"):
                continue
            try:
                last = json.loads(ln)
            except Exception:
                continue
    if last is None:
        return None
    return last.get("rates")


def aggregate_truth(merged: Dict[int, List[dict]], tpc: int,
                    split_tick: int) -> dict:
    """Compute trace ground-truth rates over ROI rows only.

    Rows with commit_tick < split_tick are treated as warmup and excluded.
    """
    loads = stores = mem_ops = 0
    l1d_ld_miss = l1d_st_miss = llc_miss = 0
    PC_L2 = 1
    PC_DRAM = 4
    for seq in merged.values():
        for r in seq:
            ct = int(r.get("_commit_tick", r.get("commit_tick", 0)) or 0)
            if ct <= 0:
                continue
            if split_tick > 0 and ct < split_tick:
                continue
            is_ld = int(r.get("is_load", 0) or 0)
            is_st = int(r.get("is_store", 0) or 0)
            is_at = int(r.get("is_atomic", 0) or 0)
            if not (is_ld or is_st or is_at):
                continue
            pc = int(r.get("path_class", 0) or 0)
            if is_ld:
                loads += 1
                if pc >= PC_L2:
                    l1d_ld_miss += 1
            if is_st:
                stores += 1
                if pc >= PC_L2:
                    l1d_st_miss += 1
            mem_ops += 1
            if pc >= PC_DRAM:
                llc_miss += 1

    def sd(a, b):
        return float(a) / float(b) if b > 0 else float("nan")

    return {
        "mr_llc": sd(llc_miss, mem_ops),
        "mr_l1d_ld": sd(l1d_ld_miss, loads),
        "mr_l1d_st": sd(l1d_st_miss, stores),
        "_counts": {
            "loads": loads, "stores": stores, "mem_ops": mem_ops,
            "l1d_ld_miss": l1d_ld_miss, "l1d_st_miss": l1d_st_miss,
            "llc_miss": llc_miss,
        },
    }


def relerr(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None:
        return None
    if isinstance(a, float) and math.isnan(a):
        return None
    if isinstance(b, float) and (math.isnan(b) or b == 0):
        return None
    return abs(a - b) / abs(b)


def main() -> int:
    args = parse_args()
    workloads = args.workload or [
        "W_ads_ctr", "W_feed_ranking", "W_interest_graph_recall"]
    warmup_dts = args.warmup_dt or [0, 100000]
    warm_models = args.warm_model or ["none"]
    mispred_Ks = args.mispred_depth_K or [16]
    shadow_modes = args.shadow_stride_mode or ["fixed64"]
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    tpc = load_tpc(args.uarch_config)

    table: List[dict] = []
    for w in workloads:
        trace_dir = Path(args.raw_root) / w / "tao_trace"
        t0 = time.time()
        merged = load_workload_rows(str(trace_dir))
        t_load = time.time() - t0
        events = collect_oracle_events(merged)
        print(
            f"[{w}] cores={len(merged)} mem_events={len(events)} "
            f"load_s={t_load:.1f}",
            flush=True,
        )

        for dt in warmup_dts:
            split_tick = compute_split_tick(merged, dt, tpc)
            tag_dt = f"warmup{dt}"
            events_path = out_dir / f"{w}.{tag_dt}.mem_events.jsonl"

            t1 = time.time()
            warm_n, roi_n = emit_oracle_jsonl(
                events, w, split_tick, tpc, events_path)
            t_emit = time.time() - t1

            truth = aggregate_truth(merged, tpc, split_tick)
            real = load_real_stats(Path(args.raw_root), w)
            real_rates = real.get("rates", {}) if real else {}

            for wm in warm_models:
                # K only varies the behavior of mispred-shadow. For other
                # warm models, collapse to a single (irrelevant) K to avoid
                # duplicate identical runs.
                Ks = mispred_Ks if wm == "mispred-shadow" else [mispred_Ks[0]]
                # shadow_stride_mode is consumed by both nextline and
                # mispred-shadow. Skip it for ``none`` (collapse).
                Sms = shadow_modes if wm != "none" else [shadow_modes[0]]
                for K in Ks:
                    for sm in Sms:
                        tag_k = f".K{K}" if wm == "mispred-shadow" else ""
                        tag_sm = f".sm-{sm}" if wm != "none" else ""
                        snap_path = out_dir / (
                            f"{w}.{tag_dt}.wm-{wm}{tag_k}{tag_sm}."
                            f"shared_pmu.jsonl")
                        t2 = time.time()
                        run_shared_system(
                            args.shared_binary, args.shared_profile,
                            events_path, snap_path, warm_model=wm,
                            mispred_depth_K=K, shadow_stride_mode=sm)
                        t_run = time.time() - t2

                        rates = parse_final_rates(snap_path) or {}
                        row = {
                            "workload": w,
                            "warmup_dt": dt,
                            "warm_model": wm,
                            "mispred_depth_K": (
                                K if wm == "mispred-shadow" else None),
                            "shadow_stride_mode": (
                                sm if wm != "none" else None),
                            "split_tick": split_tick,
                            "warmup_events": warm_n,
                            "roi_events": roi_n,
                            "shared": {k: rates.get(k) for k in PMU_RATE_KEYS},
                            "truth": {k: truth[k] for k in PMU_RATE_KEYS},
                            "real_stats": {k: real_rates.get(k)
                                           for k in PMU_RATE_KEYS},
                            # A-layer: oracle (shared) vs tao_trace label.
                            # Reflects oracle implementation accuracy.
                            "relerr": {k: relerr(rates.get(k), truth[k])
                                       for k in PMU_RATE_KEYS},
                            # Total: oracle vs real gem5 stats.txt.
                            "relerr_vs_real": {
                                k: relerr(rates.get(k), real_rates.get(k))
                                for k in PMU_RATE_KEYS},
                            # B-layer: tao_trace label vs real gem5. This is
                            # the irreducible model-level gap (tao_trace's
                            # software LRU vs gem5 Ruby MESI), **independent
                            # of oracle**. P1/P2 cannot close it.
                            "relerr_truth_vs_real": {
                                k: relerr(truth[k], real_rates.get(k))
                                for k in PMU_RATE_KEYS},
                            "timing": {"emit_s": t_emit, "run_shared_s": t_run},
                        }
                        table.append(row)
                        k_tag = f" K={K}" if wm == "mispred-shadow" else ""
                        sm_tag = f" sm={sm}" if wm != "none" else ""
                        print(
                            f"[{w}] dt={dt:>6} wm={wm:<14}{k_tag}{sm_tag}  "
                            f"warm={warm_n:>9} roi={roi_n:>9}  "
                            f"emit={t_emit:.1f}s shared={t_run:.1f}s",
                            flush=True,
                        )

    print()
    print("=" * 160)
    print(f"ORACLE WARMUP A/B  workloads={len(workloads)}  "
          f"dts={warmup_dts}  warm_models={warm_models}  "
          f"mispred_Ks={mispred_Ks}  shadow_modes={shadow_modes}")
    print("-" * 160)
    print("ERROR LAYERS (decomposition; smaller is better):")
    print("  A = err%vTr  : oracle(shared) vs tao_trace label   "
          "→ oracle implementation accuracy (P1/P2/P3.a 攻这一层)")
    print("  B = err%Mdl  : tao_trace label vs real stats.txt   "
          "→ tao_trace software-LRU model vs gem5 Ruby MESI "
          "(irreducible by oracle changes)")
    print("  T = err%vRe  : oracle(shared) vs real stats.txt    "
          "→ total = A ⊕ B (the end-user metric)")
    print("=" * 160)
    header = (
        f"{'workload':<26} {'dt':>7} {'wm':<14} {'K':>3} {'sm':<8}  "
        f"{'pmu':<10} {'shared':>9} {'truth':>9} {'real':>9} "
        f"{'A:err%vTr':>10} {'B:err%Mdl':>10} {'T:err%vRe':>10}"
    )
    print(header)
    print("-" * 160)
    for r in table:
        for k in PMU_RATE_KEYS:
            sh = r["shared"][k]
            tr = r["truth"][k]
            re_ = r["real_stats"].get(k)
            err = r["relerr"][k]
            err_mdl = r["relerr_truth_vs_real"].get(k)
            err_r = r["relerr_vs_real"].get(k)

            def fmt(v: Optional[float], w: int, p: int) -> str:
                if v is None or (isinstance(v, float) and math.isnan(v)):
                    return " " * (w - 1) + "-"
                return f"{v:>{w}.{p}f}"

            err_pct = None if err is None else err * 100
            err_mdl_pct = None if err_mdl is None else err_mdl * 100
            err_r_pct = None if err_r is None else err_r * 100
            K = r.get("mispred_depth_K")
            k_str = "-" if K is None else str(K)
            sm = r.get("shadow_stride_mode") or "-"
            print(
                f"{r['workload']:<26} {r['warmup_dt']:>7d} "
                f"{r['warm_model']:<14} {k_str:>3} {sm:<8}  "
                f"{k:<10} {fmt(sh, 9, 5)} {fmt(tr, 9, 5)} {fmt(re_, 9, 5)} "
                f"{fmt(err_pct, 10, 2)} {fmt(err_mdl_pct, 10, 2)} "
                f"{fmt(err_r_pct, 10, 2)}"
            )
        print()
    print("-" * 160)

    summary_path = out_dir / "oracle_warmup_ab.json"
    with open(summary_path, "w") as f:
        json.dump(table, f, indent=2)
    print(f"[ok] summary -> {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
