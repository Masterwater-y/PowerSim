"""eval_quota_cycles.py — v9 min-uop tail-aligned 部署侧验证。

核心流程：
  1) 从 raw trace 的程序序序列出发，窗口0每核至少 seed_n。
  2) 后续窗口以 nmin uop/core 为目标证据量；若上下文预算不足，动态降低
     nmin_eff，并在预算内尽量让各核预测尾时间对齐。
  3) 各核按程序序连续推进，无重叠、无遗漏。
  4) 用真值 labels 聚合当前窗口 PMU，报告：
       - pred vs label：模型预测能力
       - pred vs ROI：端到端部署效果（trace ROI baseline）
       - label vs ROI：方案C 切窗本身是否近似无偏

注意：
  - 这是“部署侧切窗模拟”，窗口边界只依赖上一窗预测 CPI，不依赖真值 tick。
  - 真值 tick 仅用于窗口标签聚合与最终评估，不参与下一窗构造。
  - stats.txt 全程 gem5 CPI 可能包含 trace ROI 外 setup/drain，仅作为参考。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import torch
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.build_windows import (  # noqa: E402
    COH_REMOTE,
    PC_L2,
    PC_DRAM,
    annotate_columnar_cross_core,
    annotate_columnar_trace,
    annotate_cross_core_functional_proxies,
    annotate_functional_proxies,
    annotate_rd_stride,
    aggregate_pmu,
    build_cross_core_features,
    build_core_summary_tokens,
    columnar_pmu_from_span,
    columnar_trace_cache,
    is_macro_head,
    load_core_files,
    read_aligned_parquet_columnar,
)
from data.roi_stats import (  # noqa: E402
    compute_trace_roi_stats,
    count_macros,
    load_workload_rows,
    parse_gem5_stats,
)
from model.regression_head import PMU_KEYS  # noqa: E402
from model import tokenizer as tk  # noqa: E402
from model.shared_state import (  # noqa: E402
    SS_CORE_FEATURE_KEYS,
    SS_GLOBAL_FEATURE_KEYS,
    SharedStateFeatureEngine,
    build_trace_cache,
)
from train.dataset import _global_feats_from_side  # noqa: E402


LABEL_VERSION = "v22_split_direct_no_dtlb"
CPI_UOP_IDX = PMU_KEYS.index("cpi_uop")
DENOM_KEYS = {}
COUNT_KEYS = {
    "branch_miss",
    "l1d_ld_miss",
    "l1d_st_miss",
    "l2_ld_miss",
    "l2_st_miss",
    "llc_miss",
}


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-root", required=True,
                    help="raw workload root, contains W*/tao_trace and stats.txt")
    ap.add_argument("--ckpt", required=True)
    ap.add_argument("--max-len", type=int, default=16384)
    ap.add_argument("--uarch-config", default="arch_A")
    ap.add_argument("--workload", action="append", default=[],
                    help="格式 NAME:stats_path，可多次；省略时自动扫 raw-root/W*/stats.txt")
    ap.add_argument("--max-windows", type=int, default=0,
                    help="每个 workload 最多评估多少窗口（0=全部）")
    ap.add_argument("--load-max-rows-per-core", type=int, default=0,
                    help="诊断用：每核最多加载多少 trace rows（0=全量）。"
                         "只影响加载量，不改变默认评估语义。")
    ap.add_argument("--infer-dtype", choices=("fp32", "bf16", "fp16"),
                    default="bf16",
                    help="CUDA 推理权重 dtype；默认 bf16。CPU 自动使用 fp32。")
    ap.add_argument("--sdpa-backend",
                    choices=("auto", "no_flash", "math", "efficient", "flash"),
                    default="no_flash",
                    help="覆盖 checkpoint 内 SDPA 后端；默认 no_flash，即 bf16 但禁用 flash attention。")
    ap.add_argument("--eval-cache-mode", choices=("auto", "off", "rebuild"),
                    default="auto",
                    help="部署 eval 专用 columnar cache。auto=有 aligned parquet 时使用/创建；off=关闭。")
    ap.add_argument("--eval-cache-dir", default="data/eval_columnar_cache",
                    help="eval columnar cache 存放目录。")
    ap.add_argument("--seed-n", type=int, default=256,
                    help="窗口0每核种子指令数")
    ap.add_argument("--dt-target", type=float, default=1000.0,
                    help="兼容旧参数；v9 min-uop planner 不再按装载率扩大窗口")
    ap.add_argument("--dt-min", type=float, default=200.0,
                    help="dt_target 下限（cycle）")
    ap.add_argument("--dt-max", type=float, default=8000.0,
                    help="dt_target 上限（cycle）")
    ap.add_argument("--dt-alpha", type=float, default=0.3,
                    help="兼容旧参数；v9 min-uop planner 不再使用")
    ap.add_argument("--dt-target-load", type=float, default=0.95,
                    help="兼容旧参数；v9 min-uop planner 不再使用")
    ap.add_argument("--rd-window", type=int, default=8192,
                    help="bounded sliding RD 窗口，单位是每核 memory reference 数")
    ap.add_argument("--dt-step-clip", type=float, default=0.3,
                    help="兼容旧参数；v9 min-uop planner 不再使用")
    ap.add_argument("--dt-warmup", type=int, default=2,
                    help="兼容旧参数；v9 min-uop planner 不再使用")
    ap.add_argument("--nmin", type=int, default=256,
                    help="部署侧每核目标最小 uop 数；预算不足时会动态降低")
    ap.add_argument("--nmin-floor-min", type=int, default=128,
                    help="部署侧证据量软下限；只有上下文连该下限都放不下时才继续降低")
    ap.add_argument("--fit-retry-max", type=int, default=8,
                    help="encode 超过 max_len 时最多自动缩窗重试次数")
    ap.add_argument("--train-max-len", type=int, default=32768,
                    help="训练使用的 max_len；仅用于提示训练/推理上下文不一致")
    ap.add_argument("--align-macro-boundary", action="store_true",
                    help="uop 切窗后向后补齐到 macro 边界；默认关闭，避免超长 macro 撑爆上下文")
    ap.add_argument("--device", default=None,
                    help="默认自动选 cuda/cpu；可显式指定 cpu/cuda:0")
    ap.add_argument("--emit-mem-events-dir", default="",
                    help="若非空，按 workload 输出 serial 全局访存序列 JSONL 到该目录")
    ap.add_argument("--shared-system", action="store_true",
                    help="启动 LLMSim shared_system，定期消费尚未仿真的访存事件")
    ap.add_argument("--shared-system-out-dir", default="",
                    help="shared_system PMU snapshot 输出目录；默认复用 emit-mem-events-dir 或 logs/shared_system")
    ap.add_argument("--shared-system-profile",
                    default="/data00/yinhaolang/LLMSim/config/uarch_profile_arch_A.json",
                    help="MTAO uarch_profile.json 兼容配置")
    ap.add_argument("--shared-system-binary",
                    default="/data00/yinhaolang/LLMSim/shared_system/mesi_ref_sim/build/llmsim_shared_system",
                    help="llmsim_shared_system 可执行文件路径")
    ap.add_argument("--shared-system-build", action="store_true",
                    help="运行前强制构建 shared_system C++ 可执行文件")
    ap.add_argument("--shared-system-flush-windows", type=int, default=20,
                    help="每多少个 LLMSim 窗口把未仿真的访存事件 flush 给 shared_system")
    ap.add_argument("--shared-system-snapshot-interval", type=int, default=0,
                    help="shared_system 内部按事件数额外输出 snapshot；0 表示关闭")
    ap.add_argument("--warmup-dt", type=int, default=0,
                    help="Pre-ROI warmup 长度（cycle）。>0 时按全局 tick "
                         "T = max_c(first_valid_tick[c]) + warmup_dt*tick_per_cycle "
                         "切 warmup/ROI，仅 ROI 段进入推理与 PMU 累加；"
                         "warmup 段的 mem events 仍写入 shared_system 以 warm cache，"
                         "然后插入 roi_begin marker 重置计数器。")
    ap.add_argument("--dump-window-jsonl-dir", default="",
                    help="若非空，按 workload 输出逐窗诊断 JSONL；只用于分析模型 "
                         "residual，不改变推理逻辑。")
    ap.add_argument("--planner-state-source", choices=["pred", "label", "tq_forward"],
                    default="pred",
                    help="pred=部署侧 free-running：下一窗切窗使用模型预测 CPI/累计周期；"
                         "label=oracle 对照：下一窗切窗使用当前窗真实 label CPI/累计周期，"
                         "用于剥离模型误差累积。初始冷启动窗口仍由 --seed-n 决定；"
                         "tq_forward=诊断模式：从当前 cursor 正推，用真实 commit_tick "
                         "tail-align 到每核至少 --nmin uop，不使用模型预测切窗。")
    ap.add_argument("--query-placement", choices=["tail", "segment", "tail_local"],
                    default="tail",
                    help="tail=v9: queries after TRACE_END; "
                         "segment=v15: each QUERY_Ci before Ci_END; "
                         "tail_local=v16: LOCAL_Ci in segment plus tail queries")
    return ap.parse_args()


def resolve_targets(raw_root: str, specs: List[str]) -> List[Tuple[str, str]]:
    if specs:
        out = []
        for spec in specs:
            name, _, stats = spec.partition(":")
            if not stats:
                stats = os.path.join(raw_root, name, "stats.txt")
            if not os.path.isfile(stats):
                raise FileNotFoundError(
                    f"workload {name}: stats file not found at {stats}")
            out.append((name, stats))
        return out
    out = []
    for wd in sorted(os.listdir(raw_root)):
        stats = os.path.join(raw_root, wd, "stats.txt")
        if wd.startswith("W") and os.path.isfile(stats):
            out.append((wd, stats))
    return out


def relerr(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    if math.isnan(a) or math.isnan(b) or b == 0:
        return None
    return abs(a - b) / abs(b)


def _new_pmu_acc() -> dict:
    return {
        "num": {k: 0.0 for k in PMU_KEYS},
        "den": {k: 0.0 for k in PMU_KEYS},
    }


def _accumulate_pmu(acc: dict, pmu: dict) -> None:
    uops = float(pmu.get("uops", 0.0) or 0.0)
    denoms = pmu.get("_denoms", {}) or {}
    for k in PMU_KEYS:
        v = float(pmu.get(k, 0.0) or 0.0)
        if k == "cpi_uop":
            den = uops
            num = v * den
        elif k in COUNT_KEYS:
            den = 1.0
            num = v
        else:
            den = float(denoms.get(DENOM_KEYS.get(k, ""), 0.0) or 0.0)
            num = v * den
        acc["num"][k] += num
        acc["den"][k] += den


def _accumulate_pred_pmu(acc: dict, pred_vals: List[float], label_pmu: dict) -> None:
    uops = float(label_pmu.get("uops", 0.0) or 0.0)
    denoms = label_pmu.get("_denoms", {}) or {}
    for i, k in enumerate(PMU_KEYS):
        v = float(pred_vals[i])
        if k == "cpi_uop":
            den = uops
            num = v * den
        elif k in COUNT_KEYS:
            den = 1.0
            num = v
        else:
            den = float(denoms.get(DENOM_KEYS.get(k, ""), 0.0) or 0.0)
            num = v * den
        acc["num"][k] += num
        acc["den"][k] += den


def _finalize_pmu_acc(acc: dict) -> dict:
    out = {}
    for k in PMU_KEYS:
        den = acc["den"][k]
        if k in COUNT_KEYS:
            out[k] = acc["num"][k]
        else:
            out[k] = acc["num"][k] / den if den > 0 else float("nan")
    return out


def aggregate_trace_pmu(merged: Dict[int, List[dict]], tick_per_cycle: int) -> dict:
    acc = _new_pmu_acc()
    valid_cores = 0
    for seq in merged.values():
        pmu = aggregate_pmu(seq, tick_per_cycle)
        if pmu is None:
            continue
        _accumulate_pmu(acc, pmu)
        valid_cores += 1
    out = _finalize_pmu_acc(acc)
    out["_valid_cores"] = valid_cores
    return out


def _aggregate_core_pmu_eval(seq: List[dict],
                             tick_per_cycle: int,
                             t_start_global_tick: int = 0) -> dict:
    """Eval-only PMU aggregation for one core without dropping the whole core.

    Training label path (`aggregate_pmu`) drops a whole window if any row has
    commit_tick<=0. For ROI baseline in eval, this is too aggressive.
    Here we do row-level filtering:
      - commit_tick<=0: treat as missing label row and skip from counters
      - 0<commit_tick<t_start_global_tick: warmup row, skip from ROI counters
      - others: count into ROI PMU/cycles.
    """
    branch_miss = l1d_ld_miss = l1d_st_miss = l1i_miss = llc_miss = 0
    l2_ld_miss = l2_st_miss = 0
    dtlb_miss = itlb_miss = inv_recv = 0
    branch_count = cond_branch_count = indirect_branch_count = 0
    loads = stores = atomics = mem_ops = fetch_groups = 0
    mshr_sum = mshr_n = 0
    instr_retired = 0
    valid_uops = 0
    total_rows = 0
    missing_label_uops = 0
    warmup_filtered_uops = 0
    valid_ticks: List[int] = []
    prev = None
    t_floor = int(t_start_global_tick)

    for w in seq:
        total_rows += 1
        ct = int(w.get("_commit_tick", w.get("commit_tick", 0)) or 0)
        head = is_macro_head(w, prev)
        prev = w
        if ct <= 0:
            missing_label_uops += 1
            continue
        if ct < t_floor:
            warmup_filtered_uops += 1
            continue

        valid_uops += 1
        valid_ticks.append(ct)
        if head:
            instr_retired += 1
            fetch_groups += 1
            if int(w.get("i_path_class", 0) or 0) >= PC_L2:
                l1i_miss += 1
            if int(w.get("itlb_hit", 1)) == 0:
                itlb_miss += 1

        is_ld = int(w.get("is_load", 0) or 0)
        is_st = int(w.get("is_store", 0) or 0)
        is_at = int(w.get("is_atomic", 0) or 0)
        if int(w.get("is_branch", 0) or 0):
            branch_count += 1
            if int(w.get("is_branch_cond", 0) or 0):
                cond_branch_count += 1
            if int(w.get("is_branch_indirect", 0) or 0):
                indirect_branch_count += 1
            if int(w.get("_mispredicted", w.get("mispredicted", 0)) or 0):
                branch_miss += 1

        pc = int(w.get("path_class", 0) or 0)
        if is_ld:
            loads += 1
            if pc >= PC_L2:
                l1d_ld_miss += 1
            if pc >= 2:
                l2_ld_miss += 1
        if is_st:
            stores += 1
            if pc >= PC_L2:
                l1d_st_miss += 1
            if pc >= 2:
                l2_st_miss += 1
        if is_at:
            atomics += 1
            if pc >= PC_L2:
                l1d_st_miss += 1
            if pc >= 2:
                l2_st_miss += 1
        if is_ld or is_st or is_at:
            mem_ops += 1
            if pc >= PC_DRAM:
                llc_miss += 1
            if int(w.get("dtlb_hit", 1)) == 0:
                dtlb_miss += 1
            mshr_sum += int(w.get("d_mshr_depth", 0) or 0)
            mshr_n += 1
            if int(w.get("coh_oracle", 0) or 0) in COH_REMOTE:
                inv_recv += 1

    if valid_uops <= 0:
        return {
            "valid_uops": 0,
            "total_rows": total_rows,
            "missing_label_uops": missing_label_uops,
            "warmup_filtered_uops": warmup_filtered_uops,
        }

    cycles = 0.0
    if len(valid_ticks) >= 2:
        cycles = (max(valid_ticks) - min(valid_ticks)) / float(tick_per_cycle)

    def safe_div(a: float, b: float) -> float:
        return float(a) / float(b) if b > 0 else 0.0

    return {
        "cycles": cycles,
        "instr_retired": float(instr_retired),
        "uops": float(valid_uops),
        "cpi_uop": safe_div(cycles, valid_uops),
        "branch_miss": float(branch_miss),
        "l1d_ld_miss": float(l1d_ld_miss),
        "l1d_st_miss": float(l1d_st_miss),
        "l2_ld_miss": float(l2_ld_miss),
        "l2_st_miss": float(l2_st_miss),
        "l1i_miss": float(l1i_miss),
        "llc_miss": float(llc_miss),
        "dtlb_miss": float(dtlb_miss),
        "itlb_miss": float(itlb_miss),
        "inv_recv": float(inv_recv),
        "mshr_avg": safe_div(mshr_sum, mshr_n),
        "_denoms": {
            "branch_count": branch_count,
            "cond_branch_count": cond_branch_count,
            "indirect_branch_count": indirect_branch_count,
            "loads": loads,
            "stores": stores,
            "atomics": atomics,
            "fetch_groups": fetch_groups,
            "mem_ops": mem_ops,
        },
        "valid_uops": valid_uops,
        "total_rows": total_rows,
        "missing_label_uops": missing_label_uops,
        "warmup_filtered_uops": warmup_filtered_uops,
    }


def aggregate_trace_pmu_eval(merged: Dict[int, List[dict]],
                             tick_per_cycle: int,
                             t_start_global_tick: int = 0) -> dict:
    """Eval ROI PMU baseline with row-level filtering (no whole-core drop)."""
    acc = _new_pmu_acc()
    valid_cores = 0
    valid_uops = 0
    total_rows = 0
    missing_label_uops = 0
    warmup_filtered_uops = 0
    per_core = {}
    for c, seq in sorted(merged.items()):
        pmu = _aggregate_core_pmu_eval(
            seq, tick_per_cycle, t_start_global_tick=t_start_global_tick)
        per_core[c] = {
            "valid_uops": int(pmu.get("valid_uops", 0)),
            "total_rows": int(pmu.get("total_rows", 0)),
            "missing_label_uops": int(pmu.get("missing_label_uops", 0)),
            "warmup_filtered_uops": int(pmu.get("warmup_filtered_uops", 0)),
        }
        total_rows += int(pmu.get("total_rows", 0))
        missing_label_uops += int(pmu.get("missing_label_uops", 0))
        warmup_filtered_uops += int(pmu.get("warmup_filtered_uops", 0))
        valid_uops += int(pmu.get("valid_uops", 0))
        if int(pmu.get("valid_uops", 0)) <= 0:
            continue
        _accumulate_pmu(acc, pmu)
        valid_cores += 1
    out = _finalize_pmu_acc(acc)
    out["_valid_cores"] = valid_cores
    out["_valid_uops"] = valid_uops
    out["_total_rows"] = total_rows
    out["_missing_label_uops"] = missing_label_uops
    out["_warmup_filtered_uops"] = warmup_filtered_uops
    out["_per_core"] = per_core
    return out


def _empty_core_summary() -> dict:
    return {k: 0.0 for k in tk.SUMMARY_FEATURE_KEYS}


def _avg_core_summaries(summaries: List[dict]) -> dict:
    if not summaries:
        return _empty_core_summary()
    out = _empty_core_summary()
    n = float(len(summaries))
    for k in tk.SUMMARY_FEATURE_KEYS:
        out[k] = sum(float(s.get(k, 0.0) or 0.0) for s in summaries) / n
    return out


def _mean(vals: List[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def summarize_hidden_diagnostics(win: List[dict]) -> dict:
    """Diagnostics-only microarchitectural labels.

    These fields are read from aligned parquet labels and must not become
    deployment inputs. They are useful for explaining CPI residuals.
    """
    mem = [r for r in win if int(r.get("is_load", 0) or 0)
           or int(r.get("is_store", 0) or 0)
           or int(r.get("is_atomic", 0) or 0)]
    loads = [r for r in win if int(r.get("is_load", 0) or 0)]
    branches = [r for r in win if int(r.get("is_branch", 0) or 0)]

    def path_ge(rows: List[dict], level: int) -> float:
        return sum(1 for r in rows if int(r.get("path_class", 0) or 0) >= level) / max(len(rows), 1)

    gaps_commit_issue = []
    gaps_complete_issue = []
    ready_gaps = []
    prod_dists = []
    for r in win:
        issue = int(r.get("issue_tick", 0) or 0)
        complete = int(r.get("complete_tick", 0) or 0)
        commit = int(r.get("_commit_tick", r.get("commit_tick", 0)) or 0)
        ready = int(r.get("ready_tick", 0) or 0)
        if issue > 0 and commit >= issue:
            gaps_commit_issue.append(float(commit - issue))
        if issue > 0 and complete >= issue:
            gaps_complete_issue.append(float(complete - issue))
        if ready > 0 and issue >= ready:
            ready_gaps.append(float(issue - ready))
        for d in (r.get("producer_dists") or []):
            try:
                di = int(d)
            except Exception:
                continue
            if di >= 0:
                prod_dists.append(float(di))

    return {
        "mem_uops": len(mem),
        "load_uops": len(loads),
        "branch_uops": len(branches),
        "path_l1_miss_frac_mem": path_ge(mem, 1),
        "path_llc_miss_frac_mem": path_ge(mem, PC_DRAM),
        "path_l1_miss_frac_load": path_ge(loads, 1),
        "path_llc_miss_frac_load": path_ge(loads, PC_DRAM),
        "d_mshr_depth_avg": _mean([
            float(r.get("d_mshr_depth", 0) or 0) for r in mem
        ]),
        "commit_issue_gap_avg_tick": _mean(gaps_commit_issue),
        "complete_issue_gap_avg_tick": _mean(gaps_complete_issue),
        "ready_issue_gap_avg_tick": _mean(ready_gaps),
        "producer_dist_avg": _mean(prod_dists),
        "producer_dist_max": max(prod_dists) if prod_dists else 0.0,
        "branch_mispred_frac": sum(
            1 for r in branches if int(r.get("_mispredicted", r.get("mispredicted", 0)) or 0)
        ) / max(len(branches), 1),
    }


def _avg_hidden_summaries(summaries: List[dict]) -> dict:
    keys = sorted({k for s in summaries for k in s})
    return {k: _mean([float(s.get(k, 0.0) or 0.0) for s in summaries]) for k in keys}


def load_cfg(name: str) -> dict:
    cfg_path = "/data00/yinhaolang/LLMSim/config/uarch_configs.yaml"
    with open(cfg_path) as f:
        all_cfg = yaml.safe_load(f)
    return all_cfg["configs"][name]


EVAL_COLUMNAR_CACHE_VERSION = 3


class EvalColumnarSeq:
    """List-like lazy view over a columnar core trace for legacy eval helpers."""

    def __init__(self, trace):
        self.trace = trace

    def __len__(self) -> int:
        return len(self.trace)

    def __getitem__(self, idx):
        if isinstance(idx, slice):
            start, stop, step = idx.indices(len(self))
            if step != 1:
                return [self.trace.rec(i) for i in range(start, stop, step)]
            return self.trace.window_records(start, stop)
        return self.trace.rec(int(idx))

    def __iter__(self):
        for i in range(len(self)):
            yield self.trace.rec(i)


def _eval_cache_source_signature(trace_dir: str, rd_window: int) -> tuple[dict, dict]:
    files = load_core_files(trace_dir)
    aligned = {}
    for c, fp in sorted(files.items()):
        path = fp.get("aligned")
        if not path:
            return files, {}
        st = os.stat(path)
        aligned[int(c)] = {
            "path": os.path.realpath(path),
            "size": int(st.st_size),
            "mtime_ns": int(st.st_mtime_ns),
        }
    sig = {
        "version": EVAL_COLUMNAR_CACHE_VERSION,
        "trace_dir": os.path.realpath(trace_dir),
        "rd_window": int(rd_window),
        "aligned": aligned,
    }
    return files, sig


def _eval_cache_path(trace_dir: str, rd_window: int,
                     cache_dir: str) -> tuple[Optional[Path], dict, dict]:
    files, sig = _eval_cache_source_signature(trace_dir, rd_window)
    if not sig:
        return None, files, sig
    raw = json.dumps(sig, sort_keys=True, separators=(",", ":")).encode()
    key = hashlib.sha1(raw).hexdigest()[:20]
    workload = Path(trace_dir).parent.name
    return Path(cache_dir) / f"{workload}.rd{int(rd_window)}.{key}.pt", files, sig


def _build_base_field_cache_from_columnar(traces_by_core: dict,
                                          width: int) -> Dict[int, torch.Tensor]:
    def hash_bucket_np(values: np.ndarray, n: int) -> np.ndarray:
        x = np.asarray(values, dtype=np.uint64)
        x = (x ^ (x >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
        x = (x ^ (x >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
        x = x ^ (x >> np.uint64(31))
        return (x % np.uint64(int(n))).astype(np.int64, copy=False)

    out: Dict[int, torch.Tensor] = {}
    for c, tr in traces_by_core.items():
        n = len(tr)
        mem = tr.mem_mask()
        load = tr.bool_col("is_load")
        store = tr.bool_col("is_store")
        atomic = tr.bool_col("is_atomic")
        branch = tr.bool_col("is_branch")

        op = tr.col("op_class", 0).astype(np.int64, copy=False)
        op = np.where((0 <= op) & (op < tk.N_OPCLASS), op, 0)

        h = ((tr.col("n_src", 0).astype(np.int64, copy=False) & 0x7) << 3) | (
            tr.col("n_dst", 0).astype(np.int64, copy=False) & 0x7)
        h = h.astype(np.uint64, copy=False)
        for i, pcs in enumerate(tr.producer_classes):
            for cls in (pcs or [])[:4]:
                h[i] = (h[i] << np.uint64(8)) | np.uint64(int(cls) & 0xFF)
        reg = hash_bucket_np(h, tk.N_REG_BUCKET)

        memkind = np.zeros((n,), dtype=np.int64)
        memkind[load] = 1
        memkind[store] = 2
        memkind[atomic] = 3

        rd = np.where(
            mem,
            tr.ann.get("rd_bucket", np.full((n,), tk.RD_COLD)),
            tk.RD_NONMEM,
        ).astype(np.int64, copy=False)
        stride = np.where(
            mem,
            tr.ann.get("stride_bucket", np.full((n,), tk.ST_FIRST)),
            tk.ST_NONMEM,
        ).astype(np.int64, copy=False)

        br = np.zeros((n,), dtype=np.int64)
        br_bits = (
            (tr.bool_col("is_branch_cond").astype(np.int64) << 3)
            | (tr.bool_col("is_branch_indirect").astype(np.int64) << 2)
            | (tr.bool_col("is_call").astype(np.int64) << 1)
            | tr.bool_col("is_return").astype(np.int64)
        )
        br[branch] = 1 + br_bits[branch]

        pc = tr.col("macro_pc", 0).astype(np.int64, copy=False)
        pc_b = np.zeros((n,), dtype=np.int64)
        pc_mask = pc != 0
        pc_b[pc_mask] = 1 + hash_bucket_np(pc[pc_mask], tk.N_PC_BUCKET - 1)

        macro_pos = tr.ann.get(
            "macro_pos_bucket",
            np.full((n,), tk.MACRO_POS_UNKNOWN),
        ).astype(np.int64, copy=False)

        line = tr.line_keys()
        line_hash = np.zeros((n,), dtype=np.int64)
        line_mask = mem & (line != 0)
        line_hash[line_mask] = (
            1 + hash_bucket_np(line[line_mask], tk.N_LINE_HASH - 1)
        )

        line_role = tr.ann.get(
            "line_role", np.full((n,), tk.LINE_ROLE_UNKNOWN)
        ).astype(np.int64, copy=False)

        same_hist = np.zeros((n,), dtype=np.int64)
        rd_clip = np.clip(rd, 0, tk.N_RD - 1)
        same_hist[mem] = np.minimum(tk.N_SAME_CORE_HIST - 1, 1 + rd_clip[mem])
        seen8 = tr.ann.get("seen8", np.zeros((n,), dtype=np.int8)).astype(bool)
        seen64 = tr.ann.get("seen64", np.zeros((n,), dtype=np.int8)).astype(bool)
        same_hist[mem & seen64] = 10
        same_hist[mem & seen8] = 9

        xcore = tr.ann.get(
            "xcore", np.full((n,), tk.XCORE_NONMEM)
        ).astype(np.int64, copy=False)
        coh = tr.ann.get(
            "coherence", np.full((n,), tk.COH_NONMEM)
        ).astype(np.int64, copy=False)
        fanout = tr.ann.get(
            "fanout_bucket", np.zeros((n,), dtype=np.int16)
        ).astype(np.int64, copy=False)

        cols = [
            np.clip(op, 0, tk.N_OPCLASS - 1),
            np.clip(reg, 0, tk.N_REG_BUCKET - 1),
            np.clip(memkind, 0, tk.N_MEMKIND - 1),
            np.clip(rd, 0, tk.N_RD - 1),
            np.clip(stride, 0, tk.N_STRIDE - 1),
            np.clip(br, 0, tk.N_BR - 1),
            np.clip(pc_b, 0, tk.N_PC_BUCKET - 1),
            np.clip(macro_pos, 0, tk.N_MACRO_POS - 1),
            np.clip(line_hash, 0, tk.N_LINE_HASH - 1),
            np.clip(line_role, 0, tk.N_LINE_ROLE - 1),
            np.clip(same_hist, 0, tk.N_SAME_CORE_HIST - 1),
            np.clip(xcore, 0, tk.N_XCORE_MEM - 1),
            np.clip(coh, 0, tk.N_COHERENCE - 1),
            np.clip(fanout, 0, tk.N_FANOUT - 1),
        ][:int(width)]
        rows = np.stack(cols, axis=1).astype(np.int16, copy=False)
        out[int(c)] = torch.from_numpy(rows.copy())
    return out


def _prefix_count_np(values: np.ndarray) -> np.ndarray:
    arr = np.asarray(values)
    return np.concatenate([
        np.zeros((1,), dtype=np.int64),
        np.cumsum(arr.astype(np.int64, copy=False)),
    ])


def _prefix_sum_np(values: np.ndarray) -> np.ndarray:
    arr = np.asarray(values)
    return np.concatenate([
        np.zeros((1,), dtype=np.float64),
        np.cumsum(arr.astype(np.float64, copy=False)),
    ])


def build_eval_columnar_prefixes(trace) -> None:
    """Minimal prefixes needed by deployment eval ROI/PMU summaries."""
    n = len(trace)
    mem = trace.mem_mask()
    load = trace.bool_col("is_load")
    store = trace.bool_col("is_store")
    atomic = trace.bool_col("is_atomic")
    store_like = store | atomic
    branch = trace.bool_col("is_branch")
    cond = trace.bool_col("is_branch_cond")
    indirect = trace.bool_col("is_branch_indirect")
    path = trace.col("path_class", 0)
    i_path = trace.col("i_path_class", 0)
    dtlb_hit = trace.col("dtlb_hit", 1)
    itlb_hit = trace.col("itlb_hit", 1)
    mshr = trace.col("d_mshr_depth", 0)
    coh = trace.col("coh_oracle", 0)
    mispred = trace.col("mispredicted", 0)
    head = trace.ann.get("macro_head", np.ones((n,), dtype=np.int8)).astype(bool)

    prefix_defs = {
        "instr_retired": head,
        "fetch_groups": head,
        "branch_count": branch,
        "cond_branch_count": branch & cond,
        "indirect_branch_count": branch & indirect,
        "loads": load,
        "stores": store,
        "atomics": atomic,
        "mem_ops": mem,
        "branch_miss": branch & (mispred.astype(np.int64) != 0),
        "l1d_ld_miss": load & (path >= PC_L2),
        "l1d_st_miss": store_like & (path >= PC_L2),
        "l2_ld_miss": load & (path >= 2),
        "l2_st_miss": store_like & (path >= 2),
        "l1i_miss": head & (i_path >= PC_L2),
        "llc_miss": mem & (path >= PC_DRAM),
        "dtlb_miss": mem & (dtlb_hit.astype(np.int64) == 0),
        "itlb_miss": head & (itlb_hit.astype(np.int64) == 0),
        "inv_recv": mem & np.isin(coh, list(COH_REMOTE)),
    }
    trace.prefix = {key: _prefix_count_np(arr)
                    for key, arr in prefix_defs.items()}
    trace.prefix["mshr_sum"] = _prefix_sum_np(np.where(mem, mshr, 0))


def load_eval_columnar_bundle(trace_dir: str,
                              args: argparse.Namespace) -> Optional[dict]:
    if args.eval_cache_mode == "off" or args.load_max_rows_per_core:
        return None
    cache_path, files, sig = _eval_cache_path(
        trace_dir, args.rd_window, args.eval_cache_dir)
    if cache_path is None or not files:
        return None
    if any("aligned" not in fp for fp in files.values()):
        return None
    t0 = time.perf_counter()
    if cache_path.exists() and args.eval_cache_mode != "rebuild":
        payload = torch.load(cache_path, map_location="cpu", weights_only=False)
        if payload.get("signature") == sig:
            traces_by_core = {
                int(c): read_aligned_parquet_columnar(fp["aligned"])
                for c, fp in sorted(files.items())
            }
            ann_by_core = payload.get("ann_by_core", {})
            prefix_by_core = payload.get("prefix_by_core", {})
            for c, tr in traces_by_core.items():
                tr.ann = dict(ann_by_core.get(int(c), {}))
                tr.prefix = dict(prefix_by_core.get(int(c), {}))
                if not tr.prefix:
                    build_eval_columnar_prefixes(tr)
            payload = {
                "version": EVAL_COLUMNAR_CACHE_VERSION,
                "signature": sig,
                "traces_by_core": traces_by_core,
                "field_cache": payload["field_cache"],
                "trace_cache": payload.get("trace_cache")
                or columnar_trace_cache(traces_by_core),
                "build_s": float(payload.get("build_s", 0.0)),
                "load_s": time.perf_counter() - t0,
                "cache_hit": True,
                "cache_path": str(cache_path),
            }
            return payload

    traces_by_core = {
        int(c): read_aligned_parquet_columnar(fp["aligned"])
        for c, fp in sorted(files.items())
    }
    for tr in traces_by_core.values():
        annotate_columnar_trace(tr, rd_window=args.rd_window)
    annotate_columnar_cross_core(traces_by_core)
    for tr in traces_by_core.values():
        build_eval_columnar_prefixes(tr)
    field_cache = _build_base_field_cache_from_columnar(
        traces_by_core, tk.V26_UOP_FIELD_COUNT)
    trace_cache = columnar_trace_cache(traces_by_core)
    payload = {
        "version": EVAL_COLUMNAR_CACHE_VERSION,
        "signature": sig,
        "ann_by_core": {int(c): tr.ann for c, tr in traces_by_core.items()},
        "field_cache": field_cache,
        "trace_cache": trace_cache,
        "build_s": time.perf_counter() - t0,
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
    torch.save(payload, tmp_path)
    os.replace(tmp_path, cache_path)
    return {
        **payload,
        "traces_by_core": traces_by_core,
        "load_s": 0.0,
        "cache_hit": False,
        "cache_path": str(cache_path),
    }


def columnar_roi_stats(traces_by_core: dict, tick_per_cycle: int,
                       t_start_global_tick: int = 0) -> dict:
    per_core = {}
    sum_cycles = 0.0
    sum_instr = 0.0
    sum_uops = 0.0
    t_floor = int(t_start_global_tick)
    for c, tr in sorted(traces_by_core.items()):
        ticks = tr.ticks
        start = int(np.searchsorted(ticks, t_floor, side="left")) if t_floor > 0 else 0
        end = len(tr)
        if end - start >= 2:
            cycles = (float(ticks[end - 1]) - float(ticks[start])) / float(tick_per_cycle)
        else:
            cycles = 0.0
        instr = float(tr.prefix["instr_retired"][end] - tr.prefix["instr_retired"][start])
        uops = float(max(0, end - start))
        per_core[int(c)] = {
            "cycles": cycles,
            "instr": instr,
            "uops": uops,
            "cpi": cycles / instr if instr > 0 else float("nan"),
            "cpi_macro": cycles / instr if instr > 0 else float("nan"),
            "cpi_uop": cycles / uops if uops > 0 else float("nan"),
            "first_tick": int(ticks[start]) if start < end else 0,
            "last_tick": int(ticks[end - 1]) if end > start else 0,
            "missing_label_uops": 0,
            "rows": len(tr),
        }
        sum_cycles += cycles
        sum_instr += instr
        sum_uops += uops
    return {
        "cycles": sum_cycles,
        "instr": sum_instr,
        "uops": sum_uops,
        "cpi": sum_cycles / sum_instr if sum_instr > 0 else float("nan"),
        "cpi_macro": sum_cycles / sum_instr if sum_instr > 0 else float("nan"),
        "cpi_uop": sum_cycles / sum_uops if sum_uops > 0 else float("nan"),
        "missing_label_uops": 0,
        "per_core": per_core,
    }


def aggregate_columnar_trace_pmu_eval(traces_by_core: dict,
                                      tick_per_cycle: int,
                                      t_start_global_tick: int = 0) -> dict:
    acc = _new_pmu_acc()
    valid_cores = 0
    valid_uops = 0
    warmup_filtered_uops = 0
    per_core = {}
    t_floor = int(t_start_global_tick)
    for c, tr in sorted(traces_by_core.items()):
        ticks = tr.ticks
        start = int(np.searchsorted(ticks, t_floor, side="left")) if t_floor > 0 else 0
        end = len(tr)
        warmup_filtered_uops += start
        pmu = columnar_pmu_from_span(tr, start, end, tick_per_cycle)
        per_core[int(c)] = {
            "valid_uops": max(0, end - start),
            "total_rows": len(tr),
            "missing_label_uops": 0,
            "warmup_filtered_uops": start,
        }
        valid_uops += max(0, end - start)
        if pmu is None:
            continue
        _accumulate_pmu(acc, pmu)
        valid_cores += 1
    out = _finalize_pmu_acc(acc)
    out["_valid_cores"] = valid_cores
    out["_valid_uops"] = valid_uops
    out["_total_rows"] = sum(len(tr) for tr in traces_by_core.values())
    out["_missing_label_uops"] = 0
    out["_warmup_filtered_uops"] = warmup_filtered_uops
    out["_per_core"] = per_core
    return out


def _gcc_runtime_env() -> dict:
    env = os.environ.copy()
    try:
        lib = subprocess.check_output(
            ["g++", "-print-file-name=libstdc++.so.6"],
            text=True,
        ).strip()
    except Exception:
        lib = ""
    if lib and lib != "libstdc++.so.6":
        libdir = str(Path(lib).resolve().parent)
        old = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = libdir if not old else f"{libdir}:{old}"
    return env


def _build_shared_system_binary() -> None:
    src = Path("/data00/yinhaolang/LLMSim/shared_system/mesi_ref_sim")
    build = src / "build"
    build.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cmake", "-S", str(src), "-B", str(build)], check=True)
    subprocess.run(["cmake", "--build", str(build), "-j"], check=True)


class MemEventSink:
    """Write serial global memory events and optionally stream them to shared_system."""

    def __init__(self, args: argparse.Namespace, workload: str):
        self.workload = workload
        self.event_seq = 0
        self.pending_lines: List[str] = []
        self.pending_windows = 0
        self.event_path: Optional[str] = None
        self.shared_snapshot_path: Optional[str] = None
        self._event_fh = None
        self._shared_proc: Optional[subprocess.Popen] = None

        if args.emit_mem_events_dir:
            os.makedirs(args.emit_mem_events_dir, exist_ok=True)
            self.event_path = os.path.join(
                args.emit_mem_events_dir, f"{workload}.mem_events.jsonl")
            self._event_fh = open(self.event_path, "w", buffering=1)

        if args.shared_system:
            binary = Path(args.shared_system_binary)
            if args.shared_system_build or not binary.exists():
                _build_shared_system_binary()
            out_dir = (args.shared_system_out_dir
                       or args.emit_mem_events_dir
                       or "/data00/yinhaolang/LLMSim/logs/shared_system")
            os.makedirs(out_dir, exist_ok=True)
            self.shared_snapshot_path = os.path.join(
                out_dir, f"{workload}.shared_pmu.jsonl")
            cmd = [
                str(binary),
                args.shared_system_profile,
                "-",
                self.shared_snapshot_path,
            ]
            if args.shared_system_snapshot_interval:
                cmd.append(
                    f"--snapshot-interval={args.shared_system_snapshot_interval}")
            self._shared_proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                env=_gcc_runtime_env(),
            )

    def enabled(self) -> bool:
        return self._event_fh is not None or self._shared_proc is not None

    def emit_window(self, lines: List[str], window_id: int,
                    flush_windows: int) -> None:
        if not self.enabled():
            return
        if self._event_fh is not None:
            for line in lines:
                self._event_fh.write(line)
                self._event_fh.write("\n")
            self._event_fh.write(json.dumps({
                "event_type": "window_end",
                "workload": self.workload,
                "window": window_id,
            }, separators=(",", ":")))
            self._event_fh.write("\n")
        if self._shared_proc is not None:
            self.pending_lines.extend(lines)
            self.pending_windows += 1
            if self.pending_windows >= max(flush_windows, 1):
                self.flush_shared("window_end", window_id)

    def emit_warmup_prefix(self, lines: List[str]) -> None:
        """Emit pre-ROI warmup events followed by a roi_begin marker.

        shared_system consumes warmup events to warm cache/TLB/MSHR state,
        then the roi_begin marker tells it to drop accumulated counter deltas
        so that subsequent ROI windows produce a cold-start-free PMU snapshot.
        """
        if not self.enabled():
            return
        roi_marker = json.dumps({
            "event_type": "roi_begin",
            "workload": self.workload,
        }, separators=(",", ":"))
        if self._event_fh is not None:
            for line in lines:
                self._event_fh.write(line)
                self._event_fh.write("\n")
            self._event_fh.write(roi_marker)
            self._event_fh.write("\n")
        if self._shared_proc is not None and self._shared_proc.stdin is not None:
            for line in lines:
                self._shared_proc.stdin.write(line)
                self._shared_proc.stdin.write("\n")
            self._shared_proc.stdin.write(roi_marker)
            self._shared_proc.stdin.write("\n")
            self._shared_proc.stdin.flush()

    def flush_shared(self, reason: str, window_id: int) -> None:
        if self._shared_proc is None or self._shared_proc.stdin is None:
            return
        for line in self.pending_lines:
            self._shared_proc.stdin.write(line)
            self._shared_proc.stdin.write("\n")
        self._shared_proc.stdin.write(json.dumps({
            "event_type": reason,
            "workload": self.workload,
            "window": window_id,
        }, separators=(",", ":")))
        self._shared_proc.stdin.write("\n")
        self._shared_proc.stdin.flush()
        self.pending_lines.clear()
        self.pending_windows = 0

    def close(self, final_window: int) -> None:
        if self._event_fh is not None:
            self._event_fh.close()
            self._event_fh = None
        if self._shared_proc is not None:
            self.flush_shared("snapshot", final_window)
            if self._shared_proc.stdin is not None:
                self._shared_proc.stdin.close()
            rc = self._shared_proc.wait()
            if rc != 0:
                raise RuntimeError(
                    f"shared_system exited with code {rc} for {self.workload}")
            self._shared_proc = None


def build_serial_mem_event_lines(per_core_wins: Dict[int, List[dict]],
                                 pred_pmu,
                                 uops_per_core: List[float],
                                 pred_start_cycle: Dict[int, float],
                                 workload: str,
                                 window_id: int,
                                 seq_start: int) -> Tuple[List[str], int]:
    events = []
    cores = sorted(per_core_wins.keys())
    for ci, c in enumerate(cores):
        pred_cpi_uop = float(pred_pmu[ci, CPI_UOP_IDX].item())
        uops = float(uops_per_core[ci])
        win_start = float(pred_start_cycle[c])
        win_cycles = max(0.0, pred_cpi_uop * uops)
        mems = [
            (idx, rec) for idx, rec in enumerate(per_core_wins[c])
            if int(rec.get("is_load", 0) or 0)
            or int(rec.get("is_store", 0) or 0)
            or int(rec.get("is_atomic", 0) or 0)
        ]
        denom = max(len(mems), 1)
        for j, (idx, rec) in enumerate(mems):
            t_pred = win_start + ((j + 0.5) / denom) * win_cycles
            paddr = int(rec.get("paddr", 0) or 0)
            cl_paddr = int(rec.get("cacheline_paddr", 0) or 0)
            if paddr == 0:
                paddr = cl_paddr or int(rec.get("cacheline_addr", 0) or 0)
            if cl_paddr == 0:
                cl_paddr = paddr & ~63
            events.append((
                t_pred, int(c), int(rec.get("micro_seq", idx) or idx), {
                    "event_type": "mem",
                    "workload": workload,
                    "window": window_id,
                    "t_pred_cycle": t_pred,
                    "core_id": int(c),
                    "thread_id": int(rec.get("thread_id", c) or c),
                    "paddr": paddr,
                    "cacheline_paddr": cl_paddr,
                    "cacheline_addr": cl_paddr,
                    "is_load": int(rec.get("is_load", 0) or 0),
                    "is_store": int(rec.get("is_store", 0) or 0),
                    "is_atomic": int(rec.get("is_atomic", 0) or 0),
                    "size": int(rec.get("size", 0) or 0),
                    "micro_seq": int(rec.get("micro_seq", idx) or idx),
                    "macro_pc": int(rec.get("macro_pc", 0) or 0),
                    "micro_pc": int(rec.get("micro_pc", 0) or 0),
                }
            ))
    events.sort(key=lambda x: (x[0], x[1], x[2]))
    lines = []
    seq = seq_start
    for _, _, _, obj in events:
        obj["seq"] = seq
        seq += 1
        lines.append(json.dumps(obj, separators=(",", ":")))
    return lines, seq


def _finite(xs: List[float]) -> List[float]:
    out = []
    for x in xs:
        try:
            v = float(x)
        except Exception:
            continue
        if math.isfinite(v):
            out.append(v)
    return out


def _mean(xs: List[float]) -> float:
    vals = _finite(xs)
    return sum(vals) / len(vals) if vals else float("nan")


def _quantile(xs: List[float], q: float) -> float:
    vals = sorted(_finite(xs))
    if not vals:
        return float("nan")
    if len(vals) == 1:
        return vals[0]
    pos = (len(vals) - 1) * float(q)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return vals[lo]
    frac = pos - lo
    return vals[lo] * (1.0 - frac) + vals[hi] * frac


def _std(xs: List[float]) -> float:
    vals = _finite(xs)
    if not vals:
        return float("nan")
    m = sum(vals) / len(vals)
    return math.sqrt(sum((x - m) ** 2 for x in vals) / len(vals))


def _cv(xs: List[float]) -> float:
    vals = _finite(xs)
    if not vals:
        return float("nan")
    m = sum(vals) / len(vals)
    return _std(vals) / abs(m) if abs(m) > 1.0e-12 else float("nan")


def _pearson(a: List[float], b: List[float]) -> float:
    pairs = [
        (float(x), float(y)) for x, y in zip(a, b)
        if math.isfinite(float(x)) and math.isfinite(float(y))
    ]
    if len(pairs) < 2:
        return float("nan")
    aa = [x for x, _ in pairs]
    bb = [y for _, y in pairs]
    ma = sum(aa) / len(aa)
    mb = sum(bb) / len(bb)
    da = [x - ma for x in aa]
    db = [y - mb for y in bb]
    va = sum(x * x for x in da)
    vb = sum(y * y for y in db)
    if va <= 1.0e-20 or vb <= 1.0e-20:
        return float("nan")
    return sum(x * y for x, y in zip(da, db)) / math.sqrt(va * vb)


def _rankdata(xs: List[float]) -> List[float]:
    vals = [float(x) for x in xs]
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0 for _ in vals]
    i = 0
    while i < len(order):
        j = i + 1
        while j < len(order) and vals[order[j]] == vals[order[i]]:
            j += 1
        rank = 0.5 * (i + j - 1)
        for k in range(i, j):
            ranks[order[k]] = rank
        i = j
    return ranks


def _spearman(a: List[float], b: List[float]) -> float:
    pairs = [
        (float(x), float(y)) for x, y in zip(a, b)
        if math.isfinite(float(x)) and math.isfinite(float(y))
    ]
    if len(pairs) < 2:
        return float("nan")
    aa = [x for x, _ in pairs]
    bb = [y for _, y in pairs]
    return _pearson(_rankdata(aa), _rankdata(bb))


def _safe_log_cpi(v: float) -> float:
    try:
        x = float(v)
    except Exception:
        return float("nan")
    if not math.isfinite(x) or x <= 0.0:
        return float("nan")
    return math.log(x)


def _v26_rates_to_pmu(raw: torch.Tensor, denoms: torch.Tensor,
                      pmu_keys: Optional[List[str]] = None) -> torch.Tensor:
    """Convert v26 raw output [log_cpi, rates...] to PMU values."""
    keys = list(pmu_keys or PMU_KEYS)
    keys = keys[:int(raw.shape[-1])]
    out = torch.empty_like(raw)
    out[..., 0] = torch.exp(raw[..., 0].clamp(-20.0, 20.0))
    branch = denoms[..., 0]
    loads = denoms[..., 1]
    stores = denoms[..., 2]
    atomics = denoms[..., 3]
    mem_ops = denoms[..., 4]
    store_ops = stores + atomics
    bounds = {
        "branch_miss": branch,
        "l1d_ld_miss": loads,
        "l1d_st_miss": store_ops,
        "l2_ld_miss": loads,
        "l2_st_miss": store_ops,
        "llc_miss": mem_ops,
        "dtlb_miss": mem_ops,
    }
    for i, key in enumerate(keys[1:], start=1):
        bound = bounds.get(key, mem_ops)
        out[..., i] = raw[..., i].clamp(0.0, 1.0) * bound.to(raw.dtype)
    return out


def _columnar_prefix_delta(trace, key: str, start: int, end: int) -> float:
    pref = getattr(trace, "prefix", {}).get(key)
    if pref is None:
        return 0.0
    return float(pref[int(end)] - pref[int(start)])


def _columnar_instr_retired(trace, start: int, end: int) -> float:
    if "instr_retired" in getattr(trace, "prefix", {}):
        return _columnar_prefix_delta(trace, "instr_retired", start, end)
    n = len(trace)
    head = getattr(trace, "ann", {}).get(
        "macro_head", np.ones((n,), dtype=np.int8))
    return float(np.asarray(head[int(start):int(end)]).astype(bool).sum())


def _columnar_is_macro_head(trace, idx: int) -> bool:
    idx = int(idx)
    if idx <= 0:
        return True
    head = getattr(trace, "ann", {}).get("macro_head")
    if head is not None:
        return bool(head[idx])
    macro_pc = trace.col("macro_pc", 0)
    is_micro = trace.col("is_microop", 0)
    is_last = trace.col("is_last_microop", 0)
    prev_ended = int(is_micro[idx - 1]) == 0 or int(is_last[idx - 1]) == 1
    return bool(prev_ended or macro_pc[idx] != macro_pc[idx - 1])


def take_uop_window_columnar(trace, start: int,
                             n_uop: int,
                             align_macro_boundary: bool = False) -> Tuple[int, int]:
    n = len(trace)
    end = min(int(start) + max(1, int(n_uop)), n)
    if not align_macro_boundary:
        return end, 0
    while end < n and not _columnar_is_macro_head(trace, end):
        end += 1
    return end, int(_columnar_instr_retired(trace, start, end))


def _columnar_store_slot_mask(vaddr: int, size: int) -> int:
    v = int(vaddr or 0)
    size = int(size or 0)
    if v == 0 or size <= 0:
        return 0
    start = max(0, min(7, (v & 63) // 8))
    end = max(0, min(7, ((v & 63) + size - 1) // 8))
    mask = 0
    for b in range(start, end + 1):
        mask |= 1 << b
    return mask


def build_cross_core_features_columnar(
    traces_by_core: Mapping[int, object],
    per_core_slices: Mapping[int, Tuple[int, int]],
    per_core_pmu: Mapping[int, dict],
    cores: Sequence[int],
) -> Tuple[List[str], List[List[float]]]:
    """Columnar equivalent of build_cross_core_features()."""
    n_core = max(len(cores), 1)
    span_uops = {
        int(c): max(0, int(per_core_slices[int(c)][1])
                    - int(per_core_slices[int(c)][0]))
        for c in cores
    }
    uops_total = sum(span_uops.values())
    max_uops = max(list(span_uops.values()) or [1])

    events = []
    core_stats = {
        int(c): {
            "mem": 0, "loads": 0, "stores": 0, "random_loads": 0,
            "shared_store": 0, "shared_load": 0, "mw_store": 0,
            "lines": set(), "pages": set(),
        }
        for c in cores
    }
    access_cores: Dict[int, set] = defaultdict(set)
    store_cores: Dict[int, set] = defaultdict(set)
    store_masks: Dict[int, Dict[int, int]] = defaultdict(lambda: defaultdict(int))
    global_lines = set()
    global_pages = set()
    large_stride = 0
    stream_stride = 0
    cold_or_large_loads = 0
    mem_total = 0
    load_total = 0
    store_total = 0

    for c in cores:
        c = int(c)
        tr = traces_by_core[c]
        s0, s1 = per_core_slices[c]
        s0 = int(s0)
        s1 = int(s1)
        if s1 <= s0:
            continue
        mem_mask = tr.mem_mask()[s0:s1]
        rel_idxs = np.nonzero(mem_mask)[0]
        if rel_idxs.size == 0:
            continue
        idxs = rel_idxs + s0
        vaddr = tr.col("vaddr", 0)
        cacheline_addr = tr.col("cacheline_addr", 0)
        size = tr.col("size", 0)
        load = tr.bool_col("is_load")
        store = tr.store_mask()
        micro_seq = tr.col("micro_seq", 0)
        n = len(tr)
        stride = getattr(tr, "ann", {}).get(
            "stride_bucket", np.full((n,), tk.ST_FIRST, dtype=np.int16))
        rd = getattr(tr, "ann", {}).get(
            "rd_bucket", np.full((n,), tk.RD_COLD, dtype=np.int16))

        for idx in idxs.tolist():
            v = int(vaddr[idx] or 0)
            cl = int(cacheline_addr[idx] or 0)
            if v == 0 and cl == 0:
                continue
            line = int((v >> 6) if v != 0 else cl)
            is_ld = bool(load[idx])
            is_st = bool(store[idx])
            st_bucket = int(stride[idx])
            rd_bucket = int(rd[idx])
            mem_total += 1
            load_total += 1 if is_ld else 0
            store_total += 1 if is_st else 0
            large_stride += 1 if st_bucket in (
                tk.ST_P9_64, tk.ST_M9_64, tk.ST_LARGE
            ) else 0
            stream_stride += 1 if st_bucket in (
                tk.ST_P1, tk.ST_M1, tk.ST_P2_8, tk.ST_M2_8
            ) else 0
            is_random_load = bool(is_ld and (
                st_bucket in (tk.ST_P9_64, tk.ST_M9_64, tk.ST_LARGE)
                or rd_bucket in (tk.RD_COLD, tk.RD_FAR)
            ))
            cold_or_large_loads += 1 if is_random_load else 0
            access_cores[line].add(c)
            global_lines.add(line)
            global_pages.add(line >> 6)
            st = core_stats[c]
            st["mem"] += 1
            st["loads"] += 1 if is_ld else 0
            st["stores"] += 1 if is_st else 0
            st["random_loads"] += 1 if is_random_load else 0
            st["lines"].add(line)
            st["pages"].add(line >> 6)
            if is_st:
                store_cores[line].add(c)
                store_masks[line][c] |= _columnar_store_slot_mask(v, int(size[idx]))
            ms = int(micro_seq[idx] or 0)
            if ms == 0:
                ms = int(idx - s0)
            events.append((ms, c, line, is_ld, is_st))

    shared_lines = {line for line, cs in access_cores.items() if len(cs) >= 2}
    multi_writer_lines = {
        line for line, cs in store_cores.items() if len(cs) >= 2
    }
    shared_store_count = 0
    multi_writer_store_count = 0
    shared_load_count = 0
    for _ms, c, line, is_ld, is_st in events:
        if is_ld and line in shared_lines:
            shared_load_count += 1
            core_stats[c]["shared_load"] += 1
        if is_st and line in shared_lines:
            shared_store_count += 1
            core_stats[c]["shared_store"] += 1
        if is_st and line in multi_writer_lines:
            multi_writer_store_count += 1
            core_stats[c]["mw_store"] += 1

    events.sort(key=lambda x: (x[0], x[1]))
    last_store_core: Dict[int, int] = {}
    owner_switch = 0
    recent_cores: Dict[int, set] = defaultdict(set)
    fanout_sum = 0.0
    fanout_max = 0
    for _ms, c, line, is_ld, is_st in events:
        if is_st:
            prev_c = last_store_core.get(line)
            if prev_c is not None and prev_c != c:
                owner_switch += 1
            last_store_core[line] = c
            fanout = len(recent_cores[line] - {c})
            fanout_sum += float(fanout)
            fanout_max = max(fanout_max, fanout)
            recent_cores[line] = {c}
        elif is_ld:
            recent_cores[line].add(c)

    disjoint_pairs = 0
    overlap_pairs = 0
    all_pairs = 0
    for by_core in store_masks.values():
        cs = list(by_core.keys())
        for i in range(len(cs)):
            for j in range(i + 1, len(cs)):
                all_pairs += 1
                if by_core[cs[i]] & by_core[cs[j]]:
                    overlap_pairs += 1
                else:
                    disjoint_pairs += 1

    def ratio(a: float, b: float) -> float:
        return float(a) / float(b) if b > 0 else 0.0

    max_writer = max([len(cs) for cs in store_cores.values()] or [0])
    pair_num = sum((len(cs) * (len(cs) - 1)) / 2.0
                   for cs in store_cores.values())
    pair_den = max((n_core * (n_core - 1)) / 2.0, 1.0)
    shared_store_rate = ratio(shared_store_count, store_total)
    pairwise_pressure = min(1.0, ratio(pair_num, pair_den))
    random_pressure = ratio(cold_or_large_loads, max(load_total, 1))

    global_values = {
        "log1p_active_cores": math.log1p(n_core),
        "log1p_uops_window_total": math.log1p(uops_total),
        "log1p_global_distinct_data_lines": math.log1p(len(global_lines)),
        "log1p_global_distinct_data_pages": math.log1p(len(global_pages)),
        "shared_store_rate": shared_store_rate,
        "multi_writer_line_frac": ratio(len(multi_writer_lines), len(global_lines)),
        "max_writer_cores_per_line_log": math.log1p(max_writer),
        "writer_core_coverage": ratio(max_writer, n_core),
        "pairwise_writer_pressure": pairwise_pressure,
        "store_owner_switch_rate": ratio(owner_switch, store_total),
        "inval_fanout_proxy_mean": ratio(fanout_sum, store_total),
        "disjoint_store_slot_pair_rate": ratio(disjoint_pairs, all_pairs),
        "aggregate_load_density": ratio(load_total, uops_total),
        "aggregate_mem_density": ratio(mem_total, uops_total),
        "global_large_stride_rate": ratio(large_stride, mem_total),
        "random_access_pressure": random_pressure,
        "lines_per_kuop_global": 1000.0 * ratio(len(global_lines), uops_total),
        "pages_per_kuop_global": 1000.0 * ratio(len(global_pages), uops_total),
    }

    side_feats: List[List[float]] = []
    for c in cores:
        c = int(c)
        pmu = per_core_pmu.get(c, {}) or {}
        den = pmu.get("_denoms", {}) or {}
        st = core_stats[c]
        vals = dict(global_values)
        vals.update({
            "log1p_uops_core": math.log1p(float(pmu.get("uops", 0.0) or 0.0)),
            "log1p_instr_retired": math.log1p(
                float(pmu.get("instr_retired", 0.0) or 0.0)
            ),
            "core_fill_ratio": ratio(float(pmu.get("uops", 0.0) or 0.0), max_uops),
            "log1p_branch_count": math.log1p(den.get("branch_count", 0.0) or 0.0),
            "log1p_cond_branch_count": math.log1p(
                den.get("cond_branch_count", 0.0) or 0.0
            ),
            "log1p_indirect_branch_count": math.log1p(
                den.get("indirect_branch_count", 0.0) or 0.0
            ),
            "log1p_load_count": math.log1p(den.get("loads", 0.0) or 0.0),
            "log1p_store_count": math.log1p(den.get("stores", 0.0) or 0.0),
            "log1p_atomic_count": math.log1p(den.get("atomics", 0.0) or 0.0),
            "log1p_mem_ops": math.log1p(den.get("mem_ops", 0.0) or 0.0),
            "log1p_distinct_data_lines_core": math.log1p(len(st["lines"])),
            "log1p_distinct_data_pages_core": math.log1p(len(st["pages"])),
            "core_shared_store_rate": ratio(st["shared_store"], st["stores"]),
            "core_shared_load_rate": ratio(st["shared_load"], st["loads"]),
            "core_multi_writer_store_rate": ratio(st["mw_store"], st["stores"]),
            "core_random_load_density": ratio(st["random_loads"], max(span_uops[c], 1)),
        })
        side_feats.append([float(vals.get(k, 0.0)) for k in tk.SIDE_FEATURE_KEYS])

    global_tokens = [
        f"<G_NCORE_{tk.global_ncore_bucket(n_core)}>",
        f"<G_SHARED_WRITE_{tk.global_level_bucket(shared_store_rate)}>",
        f"<G_PAIRWISE_PRESSURE_{tk.global_level_bucket(pairwise_pressure)}>",
        f"<G_RANDOM_LOAD_{tk.global_level_bucket(random_pressure)}>",
    ]
    return global_tokens, side_feats


def predict_window_v26(model, cfg: dict,
                       per_core_wins: Dict[int, List[dict]],
                       per_core_prev: Dict[int, Optional[dict]],
                       pred_start_cycle: Dict[int, float],
                       device: str, max_len: int,
                       field_cache: Optional[Dict[int, torch.Tensor]] = None,
                       per_core_slices: Optional[Dict[int, Tuple[int, int]]] = None,
                       shared_state: Optional[SharedStateFeatureEngine] = None,
                       shared_trace_cache: Optional[Dict[int, dict]] = None) -> dict:
    """V26 full Q/K/V/R prediction path using structured tensors."""
    cores = sorted(per_core_wins.keys())
    min_start = min(pred_start_cycle[c] for c in cores)
    t_start_rel = [float(pred_start_cycle[c] - min_start) for c in cores]
    t_encode0 = time.perf_counter()
    raw_model = model.module if hasattr(model, "module") else model
    model_pmu_keys = list(getattr(
        getattr(raw_model, "cfg", None),
        "pmu_keys",
        tuple(PMU_KEYS),
    ))
    field_count = int(getattr(
        getattr(raw_model, "cfg", None),
        "uop_field_count",
        tk.V9_UOP_FIELD_COUNT,
    ))
    side_feat_dim = int(getattr(
        getattr(raw_model, "cfg", None),
        "side_feat_dim",
        len(tk.SIDE_FEATURE_KEYS),
    ))
    global_feat_dim = int(getattr(
        getattr(raw_model, "cfg", None),
        "global_feat_dim",
        len(tk.MODEL_GLOBAL_FEATURE_KEYS),
    ))
    n_core = len(cores)
    shared_features = None
    if shared_state is not None:
        if shared_trace_cache is not None and per_core_slices is not None:
            shared_features = shared_state.window_features_cached(
                per_core_slices, shared_trace_cache, cores)
        else:
            shared_features = shared_state.window_features(per_core_wins, cores)
    labels: List[List[float]] = []
    instr_retired: List[float] = []
    uops_per_core: List[float] = []
    denoms_list: List[dict] = []
    per_core_rows: List[torch.Tensor] = []
    max_l = 1
    per_core_pmu = {}
    for c in cores:
        win = per_core_wins[c]
        prev = per_core_prev.get(c)
        pmu = aggregate_pmu(win, int(cfg.get("tick_per_cycle", 333)), prev=prev)
        if pmu is None:
            labels.append([float("nan")] * len(PMU_KEYS))
            pmu_for_feats = {
                "uops": float(len(win)),
                "instr_retired": float(count_macros(win, prev=prev)),
                "_denoms": {},
            }
        else:
            labels.append([pmu[k] for k in PMU_KEYS])
            pmu_for_feats = pmu
        per_core_pmu[c] = pmu_for_feats
        instr_retired.append(float(count_macros(win, prev=prev)))
        uops_per_core.append(float(len(win)))
        denoms_list.append(pmu_for_feats.get("_denoms", {}) or {})

        if (
            field_count <= tk.V26_UOP_FIELD_COUNT
            and
            field_cache is not None
            and per_core_slices is not None
            and c in field_cache
            and c in per_core_slices
        ):
            s0, s1 = per_core_slices[c]
            rows_t = field_cache[c][int(s0):int(s1), :field_count]
        elif field_count > tk.V26_UOP_FIELD_COUNT:
            ss_width = field_count - tk.V26_UOP_FIELD_COUNT
            ss_rows = (
                (shared_features or {}).get("uop", {}).get(c)
                if shared_features is not None else None
            )
            if ss_rows is None:
                ss_rows = [[0] * ss_width for _ in win]
            if (
                field_cache is not None
                and per_core_slices is not None
                and c in field_cache
                and c in per_core_slices
            ):
                s0, s1 = per_core_slices[c]
                base_t = field_cache[c][
                    int(s0):int(s1), :tk.V26_UOP_FIELD_COUNT
                ].to(dtype=torch.int16)
            else:
                base_t = torch.as_tensor(
                    [tk.encode_uop_fields_v26(rec) for rec in win],
                    dtype=torch.int16,
                )
            ss_t = torch.as_tensor(ss_rows, dtype=torch.int16)
            if int(ss_t.shape[0]) != int(base_t.shape[0]):
                fixed = torch.zeros(
                    (int(base_t.shape[0]), ss_width), dtype=torch.int16)
                n_copy = min(int(ss_t.shape[0]), int(base_t.shape[0]))
                if n_copy > 0:
                    fixed[:n_copy, :min(ss_width, int(ss_t.shape[1]))] = (
                        ss_t[:n_copy, :ss_width]
                    )
                ss_t = fixed
            elif int(ss_t.shape[1]) != ss_width:
                fixed = torch.zeros(
                    (int(base_t.shape[0]), ss_width), dtype=torch.int16)
                n_cols = min(ss_width, int(ss_t.shape[1]))
                if n_cols > 0:
                    fixed[:, :n_cols] = ss_t[:, :n_cols]
                ss_t = fixed
            rows_t = torch.cat([base_t, ss_t], dim=1)[:, :field_count]
        elif field_count >= tk.V26_UOP_FIELD_COUNT:
            rows = [tk.encode_uop_fields_v26(rec)[:field_count] for rec in win]
            rows_t = torch.as_tensor(rows, dtype=torch.int16)
        else:
            rows = [tk.encode_uop_fields(rec)[:field_count] for rec in win]
            for row in rows:
                if len(row) < field_count:
                    row.extend([0] * (field_count - len(row)))
            rows_t = torch.as_tensor(rows, dtype=torch.int16)
        if int(rows_t.shape[0]) == 0:
            rows_t = torch.zeros((1, field_count), dtype=torch.int16)
        per_core_rows.append(rows_t)
        max_l = max(max_l, int(rows_t.shape[0]))

    per_core_for_features = {
        c: (per_core_wins[c], per_core_pmu[c]) for c in cores
    }
    _global_tokens, side_feats_list = build_cross_core_features(
        per_core_for_features, cores)
    if shared_features is not None:
        key_to_idx = {k: i for i, k in enumerate(tk.SIDE_FEATURE_KEYS)}
        core_feats = shared_features.get("core", {}) or {}
        global_feats_row = list(shared_features.get("global", []) or [])
        for ci, c in enumerate(cores):
            row = side_feats_list[ci]
            for name, val in zip(
                    SS_CORE_FEATURE_KEYS,
                    core_feats.get(c, [0.0] * len(SS_CORE_FEATURE_KEYS))):
                idx = key_to_idx.get(name)
                if idx is not None:
                    row[idx] = float(val)
            for name, val in zip(SS_GLOBAL_FEATURE_KEYS, global_feats_row):
                idx = key_to_idx.get(name)
                if idx is not None:
                    row[idx] = float(val)

    def _fit_feature_row(row: List[float], width: int) -> List[float]:
        row = list(row)
        if len(row) < width:
            row.extend([0.0] * (width - len(row)))
        return row[:width]

    side_feats_list = [
        _fit_feature_row(row, side_feat_dim) for row in side_feats_list
    ]

    t_tensor0 = time.perf_counter()

    pin = device.startswith("cuda")
    uop_fields_cpu = torch.zeros(
        (1, n_core, max_l, field_count), dtype=torch.int16,
        pin_memory=pin,
    )
    uop_mask_cpu = torch.zeros(
        (1, n_core, max_l), dtype=torch.bool, pin_memory=pin)
    for ci, rows in enumerate(per_core_rows):
        n = int(rows.shape[0])
        uop_fields_cpu[0, ci, :n] = rows.to(dtype=torch.int16)
        uop_mask_cpu[0, ci, :n] = True
    uop_fields = uop_fields_cpu.to(device=device, non_blocking=True)
    uop_mask = uop_mask_cpu.to(device=device, non_blocking=True)
    core_mask = torch.ones((1, n_core), dtype=torch.float32, device=device)
    side_feats = torch.tensor([side_feats_list], dtype=torch.float32, device=device)
    denoms = torch.tensor([
        [
            [
                float((d or {}).get("branch_count", 0.0) or 0.0),
                float((d or {}).get("loads", 0.0) or 0.0),
                float((d or {}).get("stores", 0.0) or 0.0),
                float((d or {}).get("atomics", 0.0) or 0.0),
                float((d or {}).get("mem_ops", 0.0) or 0.0),
                float((d or {}).get("page_touches", 0.0) or 0.0),
            ]
            for d in denoms_list
        ]
    ], dtype=torch.float32, device=device)
    global_feats = _global_feats_from_side(side_feats, core_mask)
    if int(global_feats.shape[-1]) > global_feat_dim:
        global_feats = global_feats[..., :global_feat_dim]
    elif int(global_feats.shape[-1]) < global_feat_dim:
        pad = torch.zeros(
            (*global_feats.shape[:-1],
             global_feat_dim - int(global_feats.shape[-1])),
            dtype=global_feats.dtype,
            device=global_feats.device,
        )
        global_feats = torch.cat([global_feats, pad], dim=-1)

    t_forward0 = time.perf_counter()
    with torch.inference_mode():
        raw = model(uop_fields, uop_mask, core_mask, side_feats, global_feats)
        pmu = _v26_rates_to_pmu(
            raw.float(), denoms, pmu_keys=model_pmu_keys).cpu()[0]
    if device.startswith("cuda"):
        torch.cuda.synchronize()
    t_done = time.perf_counter()
    return {
        "pred_pmu": pmu,
        "label": labels,
        "instr_retired": instr_retired,
        "uops": uops_per_core,
        "t_start_rel": t_start_rel,
        "core_split": [int(x) for x in uops_per_core],
        "timing": {
            "encode_s": t_tensor0 - t_encode0,
            "tensor_s": t_forward0 - t_tensor0,
            "forward_s": t_done - t_forward0,
        },
    }


def predict_window_v26_columnar(
    model,
    cfg: dict,
    traces_by_core: Mapping[int, object],
    per_core_slices: Dict[int, Tuple[int, int]],
    pred_start_cycle: Dict[int, float],
    device: str,
    max_len: int,
    field_cache: Dict[int, torch.Tensor],
    shared_state: Optional[SharedStateFeatureEngine] = None,
    shared_trace_cache: Optional[Dict[int, dict]] = None,
) -> dict:
    """V26/V27 prediction path without materializing per-window dict records."""
    del max_len
    cores = sorted(int(c) for c in per_core_slices.keys())
    min_start = min(pred_start_cycle[c] for c in cores)
    t_start_rel = [float(pred_start_cycle[c] - min_start) for c in cores]
    t_encode0 = time.perf_counter()
    raw_model = model.module if hasattr(model, "module") else model
    model_pmu_keys = list(getattr(
        getattr(raw_model, "cfg", None),
        "pmu_keys",
        tuple(PMU_KEYS),
    ))
    field_count = int(getattr(
        getattr(raw_model, "cfg", None),
        "uop_field_count",
        tk.V9_UOP_FIELD_COUNT,
    ))
    side_feat_dim = int(getattr(
        getattr(raw_model, "cfg", None),
        "side_feat_dim",
        len(tk.SIDE_FEATURE_KEYS),
    ))
    global_feat_dim = int(getattr(
        getattr(raw_model, "cfg", None),
        "global_feat_dim",
        len(tk.MODEL_GLOBAL_FEATURE_KEYS),
    ))
    n_core = len(cores)
    tick_per_cycle = int(cfg.get("tick_per_cycle", 333))

    shared_features = None
    if shared_state is not None:
        if shared_trace_cache is None:
            raise RuntimeError("columnar shared-state eval requires trace_cache")
        shared_features = shared_state.window_features_cached(
            per_core_slices, shared_trace_cache, cores)

    labels: List[List[float]] = []
    instr_retired: List[float] = []
    uops_per_core: List[float] = []
    denoms_list: List[dict] = []
    per_core_rows: List[torch.Tensor] = []
    max_l = 1
    per_core_pmu: Dict[int, dict] = {}
    label_pmu_by_core: Dict[int, Optional[dict]] = {}

    for c in cores:
        tr = traces_by_core[c]
        s0, s1 = per_core_slices[c]
        s0 = int(s0)
        s1 = int(s1)
        uops = float(max(0, s1 - s0))
        instr = float(_columnar_instr_retired(tr, s0, s1))
        pmu = columnar_pmu_from_span(tr, s0, s1, tick_per_cycle)
        label_pmu_by_core[c] = pmu
        if pmu is None:
            labels.append([float("nan")] * len(PMU_KEYS))
            pmu_for_feats = {
                "uops": uops,
                "instr_retired": instr,
                "_denoms": {},
            }
        else:
            labels.append([pmu[k] for k in PMU_KEYS])
            pmu_for_feats = pmu
        per_core_pmu[c] = pmu_for_feats
        instr_retired.append(instr)
        uops_per_core.append(uops)
        denoms_list.append(pmu_for_feats.get("_denoms", {}) or {})

        if field_count > tk.V26_UOP_FIELD_COUNT:
            ss_width = field_count - tk.V26_UOP_FIELD_COUNT
            base_t = field_cache[c][s0:s1, :tk.V26_UOP_FIELD_COUNT].to(
                dtype=torch.int16)
            ss_rows = (
                (shared_features or {}).get("uop", {}).get(c)
                if shared_features is not None else None
            )
            if ss_rows is None:
                ss_rows = [[0] * ss_width for _ in range(s1 - s0)]
            ss_t = torch.as_tensor(ss_rows, dtype=torch.int16)
            if int(ss_t.shape[0]) != int(base_t.shape[0]):
                fixed = torch.zeros(
                    (int(base_t.shape[0]), ss_width), dtype=torch.int16)
                n_copy = min(int(ss_t.shape[0]), int(base_t.shape[0]))
                if n_copy > 0:
                    fixed[:n_copy, :min(ss_width, int(ss_t.shape[1]))] = (
                        ss_t[:n_copy, :ss_width]
                    )
                ss_t = fixed
            elif int(ss_t.shape[1]) != ss_width:
                fixed = torch.zeros(
                    (int(base_t.shape[0]), ss_width), dtype=torch.int16)
                n_cols = min(ss_width, int(ss_t.shape[1]))
                if n_cols > 0:
                    fixed[:, :n_cols] = ss_t[:, :n_cols]
                ss_t = fixed
            rows_t = torch.cat([base_t, ss_t], dim=1)[:, :field_count]
        else:
            rows_t = field_cache[c][s0:s1, :field_count].to(dtype=torch.int16)
        if int(rows_t.shape[0]) == 0:
            rows_t = torch.zeros((1, field_count), dtype=torch.int16)
        per_core_rows.append(rows_t)
        max_l = max(max_l, int(rows_t.shape[0]))

    _global_tokens, side_feats_list = build_cross_core_features_columnar(
        traces_by_core, per_core_slices, per_core_pmu, cores)
    if shared_features is not None:
        key_to_idx = {k: i for i, k in enumerate(tk.SIDE_FEATURE_KEYS)}
        core_feats = shared_features.get("core", {}) or {}
        global_feats_row = list(shared_features.get("global", []) or [])
        for ci, c in enumerate(cores):
            row = side_feats_list[ci]
            for name, val in zip(
                    SS_CORE_FEATURE_KEYS,
                    core_feats.get(c, [0.0] * len(SS_CORE_FEATURE_KEYS))):
                idx = key_to_idx.get(name)
                if idx is not None:
                    row[idx] = float(val)
            for name, val in zip(SS_GLOBAL_FEATURE_KEYS, global_feats_row):
                idx = key_to_idx.get(name)
                if idx is not None:
                    row[idx] = float(val)

    def _fit_feature_row(row: List[float], width: int) -> List[float]:
        row = list(row)
        if len(row) < width:
            row.extend([0.0] * (width - len(row)))
        return row[:width]

    side_feats_list = [
        _fit_feature_row(row, side_feat_dim) for row in side_feats_list
    ]

    t_tensor0 = time.perf_counter()
    pin = device.startswith("cuda")
    uop_fields_cpu = torch.zeros(
        (1, n_core, max_l, field_count), dtype=torch.int16,
        pin_memory=pin,
    )
    uop_mask_cpu = torch.zeros(
        (1, n_core, max_l), dtype=torch.bool, pin_memory=pin)
    for ci, rows in enumerate(per_core_rows):
        n = int(rows.shape[0])
        uop_fields_cpu[0, ci, :n] = rows.to(dtype=torch.int16)
        uop_mask_cpu[0, ci, :n] = True
    uop_fields = uop_fields_cpu.to(device=device, non_blocking=True)
    uop_mask = uop_mask_cpu.to(device=device, non_blocking=True)
    core_mask = torch.ones((1, n_core), dtype=torch.float32, device=device)
    side_feats = torch.tensor([side_feats_list], dtype=torch.float32, device=device)
    denoms = torch.tensor([
        [
            [
                float((d or {}).get("branch_count", 0.0) or 0.0),
                float((d or {}).get("loads", 0.0) or 0.0),
                float((d or {}).get("stores", 0.0) or 0.0),
                float((d or {}).get("atomics", 0.0) or 0.0),
                float((d or {}).get("mem_ops", 0.0) or 0.0),
                float((d or {}).get("page_touches", 0.0) or 0.0),
            ]
            for d in denoms_list
        ]
    ], dtype=torch.float32, device=device)
    global_feats = _global_feats_from_side(side_feats, core_mask)
    if int(global_feats.shape[-1]) > global_feat_dim:
        global_feats = global_feats[..., :global_feat_dim]
    elif int(global_feats.shape[-1]) < global_feat_dim:
        pad = torch.zeros(
            (*global_feats.shape[:-1],
             global_feat_dim - int(global_feats.shape[-1])),
            dtype=global_feats.dtype,
            device=global_feats.device,
        )
        global_feats = torch.cat([global_feats, pad], dim=-1)

    t_forward0 = time.perf_counter()
    with torch.inference_mode():
        raw = model(uop_fields, uop_mask, core_mask, side_feats, global_feats)
        pmu = _v26_rates_to_pmu(
            raw.float(), denoms, pmu_keys=model_pmu_keys).cpu()[0]
    if device.startswith("cuda"):
        torch.cuda.synchronize()
    t_done = time.perf_counter()
    return {
        "pred_pmu": pmu,
        "label": labels,
        "label_pmu_by_core": label_pmu_by_core,
        "instr_retired": instr_retired,
        "uops": uops_per_core,
        "t_start_rel": t_start_rel,
        "core_split": [int(x) for x in uops_per_core],
        "timing": {
            "encode_s": t_tensor0 - t_encode0,
            "tensor_s": t_forward0 - t_tensor0,
            "forward_s": t_done - t_forward0,
        },
    }


class OnlineQuotaPlanner:
    """在线 uop 配额规划器（uop 单路径）。

    每窗 budget 以 uop 计。v9 composite uop 编码后，1 uop 占 1 个
    transformer position；overhead 预留 control/config/global/summary/query。
    plan() 以 n_min 作为目标证据量；当 n_min 或尾时间对齐需求超出预算时，
    动态降低有效 floor，并在预算内最大化落后核的预测尾时间。
    不再按 target_load 尽量填满上下文；预算只用于防止爆上下文和尾部对齐。
    """

    def __init__(self, n_core: int, max_len: int,
                 n_min: int = 8,
                 n_floor_min: int = 1,
                 overhead: int = 64,
                 dt_init: float = 1000.0,
                 dt_min: float = 200.0, dt_max: float = 8000.0,
                 dt_alpha: float = 0.3,
                 dt_target_load: float = 0.95,
                 dt_step_clip: float = 0.3,
                 dt_warmup: int = 2,
                 query_placement: str = "tail"):
        self.n_core = n_core
        self.n_min = n_min
        self.n_floor_min = max(1, min(int(n_floor_min), int(n_min)))
        v9_overhead = (
            1 + 4 + 1 + 4
            + n_core * (2 + len(tk.SUMMARY_TOKEN_FEATURES))
            + (n_core if query_placement == "tail_local" else 0)
            + 1 + n_core
        )
        effective_overhead = max(int(overhead), int(v9_overhead))
        # 留 5% margin 给边界和 tokenizer/config 差异。
        usable = max(0, max_len - effective_overhead)
        self.uop_budget = int(usable * 0.95)
        if self.uop_budget < max(self.n_core, 1):
            raise ValueError(
                f"max_len={max_len} leaves only uop_budget={self.uop_budget}, "
                f"which cannot allocate even 1 uop for n_core={self.n_core}"
            )
        # dt_target is retained only as a stable logging field.
        self.dt_target = float(dt_init)
        self.dt_min = float(dt_min)
        self.dt_max = float(dt_max)
        self.dt_alpha = float(dt_alpha)
        self.dt_target_load = float(dt_target_load)
        self.dt_step_clip = float(dt_step_clip)
        self.dt_warmup = int(dt_warmup)
        self.load_ema = float(dt_target_load)
        self.step_count = 0
        self.last_plan_stats: dict = {}

    def _effective_floor_cap(self, n_active: int) -> int:
        if n_active <= 0:
            return 0
        return max(1, min(self.n_min, self.uop_budget // n_active))

    def _effective_floor_min(self, n_active: int) -> int:
        """Soft evidence floor, reduced only when the context cannot fit it."""
        if n_active <= 0:
            return 0
        return max(1, min(self.n_floor_min, self.uop_budget // n_active))

    def cold_start(self, seed_n: int) -> List[int]:
        """第 0 窗：尽量用 seed_n，但受上下文预算约束。"""
        n_eff = self._effective_floor_cap(self.n_core)
        cap = max(1, self.uop_budget // max(self.n_core, 1))
        n0 = max(n_eff, min(seed_n, cap))
        self.last_plan_stats = {
            "mode": "cold_start",
            "nmin_target": int(self.n_min),
            "nmin_eff": int(n_eff),
            "nmin_floor_min": int(self.n_floor_min),
            "nmin_floor_eff": int(self._effective_floor_min(self.n_core)),
            "uop_budget": int(self.uop_budget),
            "counts_sum": int(n0 * self.n_core),
        }
        return [n0] * self.n_core

    @staticmethod
    def _counts_for_tail(target_tail: float, starts: List[float],
                         cpi: List[float], floor: int) -> List[int]:
        counts = []
        for s, c in zip(starts, cpi):
            need = int(math.ceil((target_tail - s) / c))
            counts.append(max(floor, need))
        return counts

    @staticmethod
    def _tail_times(starts: List[float], cpi: List[float],
                    counts: List[int]) -> List[float]:
        return [s + c * n for s, c, n in zip(starts, cpi, counts)]

    def _greedy_align_leftover(self, counts: List[int], starts: List[float],
                               cpi: List[float],
                               target_tail: float) -> List[int]:
        """Use leftover budget only to reduce tail skew up to target_tail."""
        counts = list(counts)
        while sum(counts) < self.uop_budget:
            tails = self._tail_times(starts, cpi, counts)
            i = min(range(len(tails)), key=lambda j: tails[j])
            if tails[i] >= target_tail:
                break
            counts[i] += 1
        return counts

    def _largest_aligned_floor(self, starts: List[float],
                               cpi: List[float],
                               floor_cap: int) -> Tuple[int, List[int], float] | None:
        """Largest floor whose target tail can be aligned within budget."""
        best: Tuple[int, List[int], float] | None = None
        lo, hi = 1, floor_cap
        while lo <= hi:
            mid = (lo + hi) // 2
            target = max(s + c * mid for s, c in zip(starts, cpi))
            counts = self._counts_for_tail(target, starts, cpi, mid)
            if sum(counts) <= self.uop_budget:
                best = (mid, counts, target)
                lo = mid + 1
            else:
                hi = mid - 1
        return best

    def _best_partial_tail(self, starts: List[float],
                           cpi: List[float],
                           floor: int) -> Tuple[List[int], float]:
        """Strict alignment is impossible at floor; reduce skew without dropping below floor."""
        counts = [floor] * len(starts)
        tails = self._tail_times(starts, cpi, counts)
        lo = min(tails)
        hi = max(tails)
        best_counts = counts
        best_tail = lo
        for _ in range(48):
            mid = (lo + hi) * 0.5
            trial = self._counts_for_tail(mid, starts, cpi, floor)
            if sum(trial) <= self.uop_budget:
                best_counts = trial
                best_tail = mid
                lo = mid
            else:
                hi = mid
        best_counts = self._greedy_align_leftover(
            best_counts, starts, cpi, best_tail)
        return best_counts, best_tail

    def plan(self, pred_cpi_uop: List[float],
             pred_start_cycle: List[float],
             dt_target: float | None = None) -> List[int]:
        N = len(pred_cpi_uop)
        assert len(pred_cpi_uop) == N and len(pred_start_cycle) == N
        if N <= 0:
            self.last_plan_stats = {"mode": "empty"}
            return []
        if self.uop_budget < N:
            raise ValueError(
                f"active cores={N} exceed uop_budget={self.uop_budget}; "
                "increase --max-len"
            )

        # 先尝试最大的可行 nmin_eff，使各核都能追到该 floor 对应的公共尾部。
        # 若严格对齐只能靠低于软下限的 floor 才能做到，则不牺牲证据量：
        # 保持 nmin_floor_eff，把落后核在预算内尽量往公共尾部推进。未处理的
        # uop 留给后续窗口。
        cpi = [max(c, 1e-4) for c in pred_cpi_uop]
        starts = [float(x) for x in pred_start_cycle]
        floor_cap = self._effective_floor_cap(N)
        floor_min = self._effective_floor_min(N)
        aligned = self._largest_aligned_floor(starts, cpi, floor_cap)
        if aligned is not None and aligned[0] >= floor_min:
            n_eff, counts, target_tail = aligned
            counts = self._greedy_align_leftover(
                counts, starts, cpi, target_tail)
            mode = "aligned_floor"
        else:
            n_eff = floor_min
            counts, target_tail = self._best_partial_tail(
                starts, cpi, floor_min)
            mode = "catch_up_floor"
        tails = self._tail_times(starts, cpi, counts)
        self.last_plan_stats = {
            "mode": mode,
            "nmin_target": int(self.n_min),
            "nmin_eff": int(n_eff),
            "nmin_floor_min": int(self.n_floor_min),
            "nmin_floor_eff": int(floor_min),
            "uop_budget": int(self.uop_budget),
            "counts_sum": int(sum(counts)),
            "tail_target": float(target_tail),
            "tail_skew": float(max(tails) - min(tails)) if tails else 0.0,
        }
        return counts

    def update_dt_target(self, uops_used_total: float) -> float:
        """No-op for v9 min-uop planner; load_ema is diagnostic only."""
        self.step_count += 1
        load = uops_used_total / max(self.uop_budget, 1)
        self.load_ema = load
        return self.dt_target


def take_macro_window(seq: List[dict], start: int,
                      n_macro: int) -> Tuple[int, int]:
    """从 start 起取 n_macro 条完整 macro 指令对应的全部 micro-op。

    返回 (end_index, got_macro)：seq[start:end_index] 含 got_macro 条 macro，
    且 end_index 落在下一条 macro 的首条 micro（或序列末尾），保证窗口按
    macro 边界平铺、各窗起点都是 macro head。got_macro < n_macro 表示尾部不足。
    """
    i = start
    prev = seq[start - 1] if start > 0 else None
    got = 0
    while i < len(seq):
        if is_macro_head(seq[i], prev):
            if got == n_macro:
                break
            got += 1
        prev = seq[i]
        i += 1
    return i, got


def take_uop_window(seq: List[dict], start: int,
                    n_uop: int,
                    align_macro_boundary: bool = False) -> Tuple[int, int]:
    """从 start 起取 n_uop 条 µop。

    默认不向后补齐 macro 边界，避免单条超长 macro 分解成数千 µop 时撑爆
    上下文。若 align_macro_boundary=True，则恢复旧行为：向后补到下一条
    macro 的首条 µop（或序列末尾）。
    """
    n = len(seq)
    end_raw = min(start + max(1, n_uop), n)
    end = end_raw
    if not align_macro_boundary:
        return end, 0
    if align_macro_boundary:
        while end < n:
            prev = seq[end - 1] if end > 0 else None
            if is_macro_head(seq[end], prev):
                break
            end += 1
    got_macro = 0
    prev = seq[start - 1] if start > 0 else None
    for i in range(start, end):
        if is_macro_head(seq[i], prev):
            got_macro += 1
        prev = seq[i]
    return end, got_macro


def estimate_v9_token_len(cfg: dict, n_core: int, uop_total: int,
                          query_placement: str = "tail") -> int:
    """Exact v9 sequence length for fixed-size summary/global/control tokens."""
    local_extra = n_core if query_placement == "tail_local" else 0
    overhead = (
        1 + len(tk.cfg_tokens(cfg)) + 1 + len(tk.GLOBAL_TOKEN_FEATURES)
        + n_core * (2 + len(tk.SUMMARY_TOKEN_FEATURES))
        + local_extra + 1 + n_core
    )
    return int(overhead + uop_total)


def _shrink_count_map_to_budget(counts: Dict[int, int],
                                active_cores: List[int],
                                uop_budget: int,
                                floor: int) -> Tuple[Dict[int, int], int]:
    """Shrink planned uop counts without dropping below floor unless impossible."""
    n_active = len(active_cores)
    if n_active <= 0:
        return {}, 0
    if uop_budget < n_active:
        raise ValueError(
            f"uop_budget={uop_budget} cannot fit one uop per active core={n_active}"
        )
    eff_floor = max(1, min(int(floor), int(uop_budget) // n_active))
    out = {
        c: max(1, int(counts.get(c, 1)))
        for c in active_cores
    }
    if sum(out.values()) <= uop_budget:
        return out, eff_floor

    while sum(out.values()) > uop_budget:
        extras = {
            c: max(0, out[c] - min(eff_floor, out[c]))
            for c in active_cores
        }
        c = max(active_cores, key=lambda x: extras[x])
        extra = extras[c]
        if extra <= 0:
            # Should only happen if remaining trace lengths were already below
            # eff_floor for many cores. Lower the largest count as a last resort.
            c = max(active_cores, key=lambda x: out[x])
            if out[c] <= 1:
                break
            out[c] -= 1
            continue
        overflow = sum(out.values()) - uop_budget
        out[c] -= min(extra, overflow)
    return out, eff_floor


def _commit_tick(rec: dict) -> int:
    return int(rec.get("_commit_tick", rec.get("commit_tick", 0)) or 0)


def _last_valid_tick(seq: List[dict], start: int, end: int) -> int:
    for i in range(min(end, len(seq)) - 1, max(0, start) - 1, -1):
        ct = _commit_tick(seq[i])
        if ct > 0:
            return ct
    return 0


def _window_true_start_cycles(per_core_wins: Dict[int, List[dict]],
                              tick_per_cycle: int,
                              true_cycle_origin: float) -> Dict[int, float]:
    out: Dict[int, float] = {}
    for c, win in per_core_wins.items():
        ticks = [_commit_tick(w) for w in win if _commit_tick(w) > 0]
        if ticks:
            out[c] = min(ticks) / float(tick_per_cycle) - true_cycle_origin
        else:
            out[c] = 0.0
    return out


def _columnar_window_tick_span(trace, start: int, end: int) -> Optional[Tuple[int, int]]:
    start = int(start)
    end = int(end)
    if end <= start:
        return None
    ticks = np.asarray(trace.ticks[start:end])
    if ticks.size == 0:
        return None
    ticks = ticks[ticks > 0]
    if ticks.size == 0:
        return None
    return int(ticks.min()), int(ticks.max())


def _columnar_window_true_start_cycles(
    traces_by_core: Mapping[int, object],
    per_core_slices: Mapping[int, Tuple[int, int]],
    tick_per_cycle: int,
    true_cycle_origin: float,
) -> Dict[int, float]:
    out: Dict[int, float] = {}
    for c, (s0, s1) in per_core_slices.items():
        span = _columnar_window_tick_span(traces_by_core[int(c)], s0, s1)
        if span is None:
            out[int(c)] = 0.0
        else:
            out[int(c)] = span[0] / float(tick_per_cycle) - true_cycle_origin
    return out


def plan_tq_forward_counts(merged: Dict[int, List[dict]],
                           cursor: Dict[int, int],
                           active_cores: List[int],
                           min_uops: int,
                           tick_per_cycle: int) -> Tuple[Dict[int, int], dict]:
    """Oracle forward TQ cut.

    Starting from current per-core cursors, find the earliest true tail time at
    which every active core has at least min_uops remaining-window evidence,
    then cut every core to that common true tail time.
    """
    min_uops = max(1, int(min_uops))
    tail_ticks: List[int] = []
    min_end: Dict[int, int] = {}
    for c in active_cores:
        seq = merged[c]
        start = int(cursor[c])
        end = min(start + min_uops, len(seq))
        min_end[c] = end
        if end <= start:
            continue
        tick = _last_valid_tick(seq, start, end)
        if tick > 0:
            tail_ticks.append(tick)

    if tail_ticks:
        target_tail_tick = max(tail_ticks)
    else:
        target_tail_tick = 0

    counts: Dict[int, int] = {}
    true_tail_ticks: List[int] = []
    for c in active_cores:
        seq = merged[c]
        start = int(cursor[c])
        end = start
        if target_tail_tick > 0:
            while end < len(seq):
                ct = _commit_tick(seq[end])
                if ct > 0 and ct > target_tail_tick and end > start:
                    break
                end += 1
        end = max(end, min_end[c])
        end = min(end, len(seq))
        if end <= start and start < len(seq):
            end = start + 1
        counts[c] = max(0, end - start)
        last_tick = _last_valid_tick(seq, start, end)
        if last_tick > 0:
            true_tail_ticks.append(last_tick)

    tail_skew_cycle = (
        (max(true_tail_ticks) - min(true_tail_ticks)) / float(tick_per_cycle)
        if true_tail_ticks else 0.0
    )
    stats = {
        "mode": "tq_forward",
        "nmin_target": int(min_uops),
        "nmin_eff": int(min_uops),
        "nmin_floor_min": int(min_uops),
        "nmin_floor_eff": int(min_uops),
        "target_tail_tick": int(target_tail_tick),
        "counts_sum": int(sum(counts.values())),
        "tail_skew": float(tail_skew_cycle),
    }
    return counts, stats


def _first_valid_commit_tick(seq: List[dict]) -> int:
    for r in seq:
        ct = int(r.get("_commit_tick", r.get("commit_tick", 0)) or 0)
        if ct > 0:
            return ct
    return 0


def compute_warmup_start_tick(merged: Dict[int, List[dict]],
                              warmup_dt_cycles: int,
                              tick_per_cycle: int) -> int:
    """Pre-ROI 切边界：全局 tick T = max_c(first_valid_tick[c]) + warmup_dt·tpc。

    若某核没有任何 commit_tick>0 的样本，则忽略该核（不会拖低 max）。
    返回 0 表示无需 warmup（warmup_dt_cycles<=0 或没有有效核）。
    """
    if warmup_dt_cycles <= 0:
        return 0
    first_ticks: List[int] = []
    for seq in merged.values():
        ft = _first_valid_commit_tick(seq)
        if ft > 0:
            first_ticks.append(ft)
    if not first_ticks:
        return 0
    return max(first_ticks) + int(warmup_dt_cycles) * int(tick_per_cycle)


def compute_columnar_warmup_start_tick(traces_by_core: Mapping[int, object],
                                       warmup_dt_cycles: int,
                                       tick_per_cycle: int) -> int:
    if warmup_dt_cycles <= 0:
        return 0
    first_ticks: List[int] = []
    for tr in traces_by_core.values():
        ticks = np.asarray(tr.ticks)
        valid = ticks[ticks > 0]
        if valid.size:
            first_ticks.append(int(valid[0]))
    if not first_ticks:
        return 0
    return max(first_ticks) + int(warmup_dt_cycles) * int(tick_per_cycle)


def advance_cursor_past_warmup(seq: List[dict],
                               t_start_global_tick: int) -> int:
    """把单核 cursor 推进到第一条 commit_tick >= t_start_global_tick 的位置。

    需落在 macro 边界上：即返回的 idx 必须满足 is_macro_head(seq[idx], seq[idx-1])。
    若整个 trace 都早于 warmup 边界，返回 len(seq)（该核没有 ROI 内容可推理）。
    """
    if t_start_global_tick <= 0:
        return 0
    n = len(seq)
    idx = 0
    while idx < n:
        ct = int(seq[idx].get("_commit_tick", seq[idx].get("commit_tick", 0)) or 0)
        if ct > 0 and ct >= t_start_global_tick:
            break
        idx += 1
    # 向后对齐到 macro head
    while idx < n:
        prev = seq[idx - 1] if idx > 0 else None
        if is_macro_head(seq[idx], prev):
            break
        idx += 1
    return idx


def advance_columnar_cursor_past_warmup(trace,
                                        t_start_global_tick: int) -> int:
    if t_start_global_tick <= 0:
        return 0
    ticks = np.asarray(trace.ticks)
    n = len(trace)
    idx = int(np.searchsorted(ticks, int(t_start_global_tick), side="left"))
    while idx < n and not _columnar_is_macro_head(trace, idx):
        idx += 1
    return idx


def warm_shared_state_columnar(
    shared_state: SharedStateFeatureEngine,
    traces_by_core: Mapping[int, object],
    trace_cache: Mapping[int, Mapping[str, Sequence]],
    cursor_start: Mapping[int, int],
) -> None:
    events: List[Tuple[int, int, int, int, bool]] = []
    for c, end in cursor_start.items():
        c = int(c)
        end = int(end)
        if end <= 0:
            continue
        tr = traces_by_core[c]
        cached = trace_cache[c]
        is_mem = np.asarray(cached["mem"][:end]).astype(bool, copy=False)
        idxs = np.nonzero(is_mem)[0]
        if idxs.size == 0:
            continue
        ticks = np.asarray(tr.ticks)
        line_keys = cached["line"]
        is_store = cached["store"]
        micro_seq = tr.col("micro_seq", 0)
        for idx in idxs.tolist():
            tick = int(ticks[idx] or 0)
            line = int(line_keys[idx])
            if tick <= 0 or not line:
                continue
            ms = int(micro_seq[idx] or 0)
            if ms == 0:
                ms = int(idx)
            events.append((tick, c, ms, line, bool(is_store[idx])))
    events.sort(key=lambda x: (x[0], x[1], x[2]))
    for _tick, c, _ms, line, store in events:
        shared_state.update_cached_event(c, line, store)


def build_warmup_mem_event_lines(merged: Dict[int, List[dict]],
                                 cursor_start: Dict[int, int],
                                 tick_per_cycle: int,
                                 workload: str,
                                 seq_start: int) -> Tuple[List[str], int]:
    """收集 warmup 前缀（seq[0:cursor_start[c]]）的访存事件，按 commit_tick 全局排序。

    用 commit_tick / tick_per_cycle 作为 t_pred_cycle，使 shared_system 看到
    与各核 wall-clock 一致的内存请求顺序（这是 warmup 段唯一可信的时间源，
    因为模型还没参与）。
    """
    events = []
    for c, end in cursor_start.items():
        seq = merged[c]
        for idx in range(end):
            rec = seq[idx]
            if not (int(rec.get("is_load", 0) or 0)
                    or int(rec.get("is_store", 0) or 0)
                    or int(rec.get("is_atomic", 0) or 0)):
                continue
            ct = int(rec.get("_commit_tick", rec.get("commit_tick", 0)) or 0)
            if ct <= 0:
                continue
            t_cycle = float(ct) / float(tick_per_cycle)
            paddr = int(rec.get("paddr", 0) or 0)
            cl_paddr = int(rec.get("cacheline_paddr", 0) or 0)
            if paddr == 0:
                paddr = cl_paddr or int(rec.get("cacheline_addr", 0) or 0)
            if cl_paddr == 0:
                cl_paddr = paddr & ~63
            events.append((
                t_cycle, int(c), int(rec.get("micro_seq", idx) or idx), {
                    "event_type": "mem",
                    "workload": workload,
                    "window": -1,
                    "t_pred_cycle": t_cycle,
                    "core_id": int(c),
                    "thread_id": int(rec.get("thread_id", c) or c),
                    "paddr": paddr,
                    "cacheline_paddr": cl_paddr,
                    "cacheline_addr": cl_paddr,
                    "is_load": int(rec.get("is_load", 0) or 0),
                    "is_store": int(rec.get("is_store", 0) or 0),
                    "is_atomic": int(rec.get("is_atomic", 0) or 0),
                    "size": int(rec.get("size", 0) or 0),
                    "micro_seq": int(rec.get("micro_seq", idx) or idx),
                    "macro_pc": int(rec.get("macro_pc", 0) or 0),
                    "micro_pc": int(rec.get("micro_pc", 0) or 0),
                }
            ))
    events.sort(key=lambda x: (x[0], x[1], x[2]))
    lines = []
    seq_id = seq_start
    for _, _, _, obj in events:
        obj["seq"] = seq_id
        seq_id += 1
        lines.append(json.dumps(obj, separators=(",", ":")))
    return lines, seq_id


def eval_workload(model, cfg: dict, workload: str,
                  trace_dir: str, stats_path: str, args: argparse.Namespace,
                  device: str) -> dict:
    columnar_bundle = load_eval_columnar_bundle(trace_dir, args)
    columnar_traces = None
    if columnar_bundle is not None:
        columnar_traces = columnar_bundle["traces_by_core"]
        merged = {
            int(c): EvalColumnarSeq(tr)
            for c, tr in sorted(columnar_traces.items())
        }
        print(
            f"[eval-cache] {'hit' if columnar_bundle.get('cache_hit') else 'built'} "
            f"path={columnar_bundle.get('cache_path')} "
            f"build_s={float(columnar_bundle.get('build_s', 0.0)):.1f} "
            f"load_s={float(columnar_bundle.get('load_s', 0.0)):.1f}",
            flush=True,
        )
    else:
        merged = load_workload_rows(
            trace_dir, max_rows_per_core=max(0, args.load_max_rows_per_core))
        if args.load_max_rows_per_core:
            print(
                f"[diag] load_max_rows_per_core={args.load_max_rows_per_core} "
                f"loaded_rows={{{', '.join(f'{c}: {len(seq)}' for c, seq in sorted(merged.items()))}}}",
                flush=True,
            )
        for seq in merged.values():
            annotate_rd_stride(seq, rd_window=args.rd_window)
            annotate_functional_proxies(seq)
        annotate_cross_core_functional_proxies(merged)
    raw_model = model.module if hasattr(model, "module") else model
    field_count = int(getattr(
        getattr(raw_model, "cfg", None),
        "uop_field_count",
        tk.V26_UOP_FIELD_COUNT,
    ))
    if field_count < tk.V26_UOP_FIELD_COUNT:
        raise RuntimeError(
            f"current v26 eval requires {tk.V26_UOP_FIELD_COUNT} UOP fields, "
            f"checkpoint has {field_count}"
        )
    shared_state = (
        SharedStateFeatureEngine()
        if field_count > tk.V26_UOP_FIELD_COUNT else None
    )
    cache_field_count = min(field_count, tk.V26_UOP_FIELD_COUNT)
    if columnar_bundle is not None:
        field_cache = {
            int(c): rows[:, :cache_field_count].contiguous()
            for c, rows in columnar_bundle["field_cache"].items()
        }
        shared_trace_cache = (
            columnar_bundle["trace_cache"] if shared_state is not None else None
        )
    else:
        field_cache = {
            c: torch.as_tensor(
                [tk.encode_uop_fields_v26(rec)[:cache_field_count] for rec in seq],
                dtype=torch.int16,
            )
            for c, seq in merged.items()
        }
        shared_trace_cache = (
            {c: build_trace_cache(seq) for c, seq in merged.items()}
            if shared_state is not None else None
        )
    cores = sorted(merged.keys())
    g_cyc, g_ins, cpi_gem5 = parse_gem5_stats(stats_path)
    tick_per_cycle = int(cfg.get("tick_per_cycle", 333))
    if columnar_traces is not None:
        t_start_global_tick = compute_columnar_warmup_start_tick(
            columnar_traces, args.warmup_dt, tick_per_cycle)
    else:
        t_start_global_tick = compute_warmup_start_tick(
            merged, args.warmup_dt, tick_per_cycle)
    warmup_cursor = {c: 0 for c in cores}
    if t_start_global_tick > 0:
        for c in cores:
            if columnar_traces is not None:
                warmup_cursor[c] = advance_columnar_cursor_past_warmup(
                    columnar_traces[c], t_start_global_tick)
            else:
                warmup_cursor[c] = advance_cursor_past_warmup(
                    merged[c], t_start_global_tick)
        if shared_state is not None:
            if columnar_traces is not None and shared_trace_cache is not None:
                warm_shared_state_columnar(
                    shared_state, columnar_traces,
                    shared_trace_cache, warmup_cursor)
            else:
                warm_events = []
                for c in cores:
                    for rec in merged[c][:warmup_cursor[c]]:
                        tick = _commit_tick(rec)
                        if tick > 0:
                            warm_events.append((tick, c, rec))
                warm_events.sort(key=lambda x: (x[0], x[1]))
                for _tick, c, rec in warm_events:
                    shared_state.update_event(c, rec)
        warm_total = sum(warmup_cursor.values())
        roi_total = sum(len(merged[c]) - warmup_cursor[c] for c in cores)
        print(
            f"[warmup] dt={args.warmup_dt}cyc "
            f"t_start_global_tick={t_start_global_tick} "
            f"warmup_uops={warm_total} roi_uops={roi_total}",
            flush=True,
        )
    if columnar_traces is not None:
        roi_stats = columnar_roi_stats(
            columnar_traces, tick_per_cycle,
            t_start_global_tick=t_start_global_tick)
        roi_pmu = aggregate_columnar_trace_pmu_eval(
            columnar_traces, tick_per_cycle,
            t_start_global_tick=t_start_global_tick)
    else:
        roi_stats = compute_trace_roi_stats(
            merged, tick_per_cycle, t_start_global_tick=t_start_global_tick)
        roi_pmu = aggregate_trace_pmu_eval(
            merged, tick_per_cycle, t_start_global_tick=t_start_global_tick)
    roi_pmu["cpi_uop"] = roi_stats["cpi_uop"]
    gem5_pmu = {k: None for k in PMU_KEYS}
    # gem5 stats.txt 全程 numCycles/numInsts 给出的是 cpi_macro；这里直接放到
    # cpi_uop 槽位会量纲错位，因此 gem5 列在主对比表里以 cpi_macro 单独打印。
    cursor = {c: warmup_cursor[c] for c in cores}
    # This state drives both the t_start feature and the next-window planner.
    # In normal deployment it is advanced with predicted CPI. In label/oracle
    # diagnostic mode it is advanced with the true label CPI to remove model
    # error accumulation from the window planner.
    pred_start_cycle = {c: 0.0 for c in cores}
    roi_origin_ticks = []
    for c in cores:
        if columnar_traces is not None:
            tr = columnar_traces[c]
            if cursor[c] < len(tr):
                ticks = np.asarray(tr.ticks[cursor[c]:])
                valid = ticks[ticks > 0]
                if valid.size:
                    roi_origin_ticks.append(int(valid[0]))
            continue
        seq = merged[c]
        for j in range(cursor[c], len(seq)):
            ct = int(seq[j].get("_commit_tick", 0) or 0)
            if ct > 0:
                roi_origin_ticks.append(ct)
                break
    true_cycle_origin_tick = min(roi_origin_ticks) if roi_origin_ticks else 0
    true_cycle_origin = true_cycle_origin_tick / float(tick_per_cycle)
    # 用每核 trace 内 commit_tick 端点差作为 ROI cycles 真值；stats.txt
    # 全程 numCycles 可能包含 ROI 外 setup/drain，仅保留为参考。
    first_tick: Dict[int, int] = {}
    last_tick: Dict[int, int] = {}
    planner = OnlineQuotaPlanner(
        n_core=len(cores),
        max_len=args.max_len,
        n_min=args.nmin,
        n_floor_min=args.nmin_floor_min,
        dt_init=args.dt_target,
        dt_min=args.dt_min,
        dt_max=args.dt_max,
        dt_alpha=args.dt_alpha,
        dt_target_load=args.dt_target_load,
        dt_step_clip=args.dt_step_clip,
        dt_warmup=args.dt_warmup,
        query_placement=args.query_placement,
    )
    next_counts = {c: n for c, n in zip(cores, planner.cold_start(args.seed_n))}
    windows = 0
    sum_cyc_pred = 0.0
    sum_macro = 0.0
    sum_uops = 0.0
    ape_sum = 0.0
    ape_cnt = 0.0
    win_cpi_ape = []
    split_sum = 0.0
    split_cnt = 0
    pmu_ape_sum = {k: 0.0 for k in PMU_KEYS}
    pmu_ape_cnt = {k: 0 for k in PMU_KEYS}
    pred_pmu_acc = _new_pmu_acc()
    label_pmu_acc = _new_pmu_acc()
    core_cpi_rel_err = []
    core_cpi_signed_rel_err = []
    core_cpi_win_corr = []
    core_cpi_win_spearman = []
    core_cpi_pred_cv = []
    core_cpi_label_cv = []
    slowest_top1_hit = 0
    slowest_top2_hit = 0
    slowest_top_cnt = 0
    align_start_abs_err = []
    align_end_abs_err = []
    align_pred_start_skew = []
    align_true_start_skew = []
    timing_sum = {
        "build_s": 0.0,
        "encode_s": 0.0,
        "tensor_s": 0.0,
        "forward_s": 0.0,
        "update_plan_s": 0.0,
        "total_s": 0.0,
    }
    mem_sink = MemEventSink(args, workload)
    dump_fh = None
    dump_path = None
    if args.dump_window_jsonl_dir:
        os.makedirs(args.dump_window_jsonl_dir, exist_ok=True)
        dump_path = os.path.join(
            args.dump_window_jsonl_dir, f"{workload}.windows.jsonl")
        dump_fh = open(dump_path, "w", buffering=1)
    if t_start_global_tick > 0 and mem_sink.enabled():
        warmup_lines, mem_sink.event_seq = build_warmup_mem_event_lines(
            merged=merged,
            cursor_start=warmup_cursor,
            tick_per_cycle=tick_per_cycle,
            workload=workload,
            seq_start=mem_sink.event_seq,
        )
        mem_sink.emit_warmup_prefix(warmup_lines)
        print(
            f"[warmup] emitted {len(warmup_lines)} warmup mem events "
            f"+ roi_begin marker",
            flush=True,
        )
    columnar_native = (
        columnar_traces is not None
        and not args.dump_window_jsonl_dir
        and not mem_sink.enabled()
        and args.planner_state_source != "tq_forward"
        and (shared_state is None or shared_trace_cache is not None)
    )
    if columnar_traces is not None:
        print(
            f"[eval-cache] columnar_native={'on' if columnar_native else 'off'}",
            flush=True,
        )
    t0 = time.time()

    try:
        while True:
            if args.max_windows and windows >= args.max_windows:
                break
            t_win0 = time.perf_counter()
            active_cores = [c for c in cores if cursor[c] < len(merged[c])]
            if not active_cores:
                break
            if args.planner_state_source == "tq_forward":
                planned_counts, tq_stats = plan_tq_forward_counts(
                    merged=merged,
                    cursor=cursor,
                    active_cores=active_cores,
                    min_uops=args.nmin,
                    tick_per_cycle=tick_per_cycle,
                )
                tq_stats["uop_budget"] = int(planner.uop_budget)
                planner.last_plan_stats = tq_stats
                next_counts.update(planned_counts)
            else:
                planned_counts = {
                    c: min(
                        max(1, int(next_counts.get(c, args.nmin))),
                        len(merged[c]) - cursor[c],
                    )
                    for c in active_cores
                }
            fit_retries = 0
            fit_token_len = 0
            fit_floor_eff = int(
                planner.last_plan_stats.get(
                    "nmin_floor_eff",
                    planner._effective_floor_min(len(active_cores)),
                )
            )
            macro_align_disabled = False
            while True:
                per_core_wins: Dict[int, List[dict]] = {}
                per_core_prev: Dict[int, Optional[dict]] = {}
                per_core_slices: Dict[int, Tuple[int, int]] = {}
                win_start: Dict[int, int] = {}
                win_end: Dict[int, int] = {}
                tok_per_core: Dict[int, int] = {}
                # uop 单路径：planner 直接给 uop 配额。默认不补齐 macro 边界，
                # 因为个别 x86 macro 会展开成数千 µop，补齐会撑爆上下文。
                for c in active_cores:
                    i = cursor[c]
                    seq_len = len(merged[c])
                    remaining = seq_len - i
                    n_u = min(max(1, planned_counts.get(c, 1)), remaining)
                    if columnar_native:
                        end, _got_macro = take_uop_window_columnar(
                            columnar_traces[c], i, n_u,
                            align_macro_boundary=(
                                args.align_macro_boundary and not macro_align_disabled
                            ),
                        )
                    else:
                        seq = merged[c]
                        end, _got_macro = take_uop_window(
                            seq, i, n_u,
                            align_macro_boundary=(
                                args.align_macro_boundary and not macro_align_disabled
                            ),
                        )
                    if end <= i:
                        cursor[c] = seq_len
                        continue
                    win_start[c] = i
                    per_core_slices[c] = (i, end)
                    if not columnar_native:
                        seq = merged[c]
                        per_core_wins[c] = seq[i:end]
                        per_core_prev[c] = seq[i - 1] if i > 0 else None
                    win_end[c] = end
                    tok_per_core[c] = end - i
                window_cores = sorted(per_core_slices.keys())
                if not window_cores:
                    break

                fit_token_len = estimate_v9_token_len(
                    cfg, len(window_cores), sum(tok_per_core.values()),
                    query_placement=args.query_placement)
                if fit_token_len <= args.max_len:
                    break
                if fit_retries >= max(0, args.fit_retry_max):
                    raise ValueError(
                        f"{workload}: unable to fit window after {fit_retries} "
                        f"retries, token_len={fit_token_len} max_len={args.max_len} "
                        f"counts={tok_per_core}"
                    )
                fit_retries += 1
                if args.align_macro_boundary and not macro_align_disabled:
                    macro_align_disabled = True
                    continue
                uop_budget_exact = args.max_len - estimate_v9_token_len(
                    cfg, len(window_cores), 0,
                    query_placement=args.query_placement)
                planned_counts, fit_floor_eff = _shrink_count_map_to_budget(
                    counts=tok_per_core,
                    active_cores=window_cores,
                    uop_budget=uop_budget_exact,
                    floor=fit_floor_eff,
                )
                if args.planner_state_source == "tq_forward":
                    planner.last_plan_stats.update({
                        "fit_shrunk_to_budget": True,
                        "counts_sum_after_fit": int(sum(planned_counts.values())),
                        "nmin_floor_eff_after_fit": int(fit_floor_eff),
                    })
            if not window_cores:
                break

            if args.planner_state_source == "tq_forward":
                pred_start_cycle.update(_window_true_start_cycles(
                    per_core_wins=per_core_wins,
                    tick_per_cycle=tick_per_cycle,
                    true_cycle_origin=true_cycle_origin,
                ))

            t_build_done = time.perf_counter()
            if columnar_native:
                step = predict_window_v26_columnar(
                    model, cfg, columnar_traces, per_core_slices,
                    pred_start_cycle, device, args.max_len,
                    field_cache=field_cache,
                    shared_state=shared_state,
                    shared_trace_cache=shared_trace_cache,
                )
            else:
                step = predict_window_v26(
                    model, cfg, per_core_wins, per_core_prev,
                    pred_start_cycle, device, args.max_len,
                    field_cache=field_cache,
                    per_core_slices=per_core_slices,
                    shared_state=shared_state,
                    shared_trace_cache=shared_trace_cache,
                )
            t_update0 = time.perf_counter()
            pred_pmu = step["pred_pmu"]
            pred_start_before = {
                c: float(pred_start_cycle.get(c, 0.0))
                for c in window_cores
            }
            valid_pred_cpi = []
            valid_label_cpi = []
            pred_start_vals = []
            true_start_vals = []
            for ci, c in enumerate(window_cores):
                pred_cpi_uop = float(pred_pmu[ci, CPI_UOP_IDX].item())
                label_cpi_uop = float(step["label"][ci][CPI_UOP_IDX])
                uops_ci = float(step["uops"][ci])
                pred_start_c = float(pred_start_before[c])
                pred_end_c = pred_start_c + pred_cpi_uop * uops_ci
                pred_start_vals.append(pred_start_c)
                if columnar_native:
                    tick_span = _columnar_window_tick_span(
                        columnar_traces[c], win_start[c], win_end[c])
                else:
                    ticks = [
                        int(w["_commit_tick"]) for w in per_core_wins[c]
                        if int(w.get("_commit_tick", 0) or 0) > 0
                    ]
                    tick_span = (min(ticks), max(ticks)) if ticks else None
                if tick_span is not None:
                    true_start_c = tick_span[0] / float(tick_per_cycle)
                    true_end_c = tick_span[1] / float(tick_per_cycle)
                    true_start_rel = true_start_c - true_cycle_origin
                    true_end_rel = true_end_c - true_cycle_origin
                    true_start_vals.append(true_start_rel)
                    align_start_abs_err.append(abs(pred_start_c - true_start_rel))
                    align_end_abs_err.append(abs(pred_end_c - true_end_rel))
                if math.isfinite(label_cpi_uop):
                    denom = abs(label_cpi_uop) + 1.0e-6
                    core_cpi_rel_err.append(
                        abs(pred_cpi_uop - label_cpi_uop) / denom
                    )
                    core_cpi_signed_rel_err.append(
                        (pred_cpi_uop - label_cpi_uop) / denom
                    )
                    valid_pred_cpi.append(pred_cpi_uop)
                    valid_label_cpi.append(label_cpi_uop)
            if len(valid_pred_cpi) >= 1:
                core_cpi_pred_cv.append(_cv(valid_pred_cpi))
                core_cpi_label_cv.append(_cv(valid_label_cpi))
            if len(valid_pred_cpi) >= 2:
                core_cpi_win_corr.append(
                    _pearson(valid_pred_cpi, valid_label_cpi)
                )
                core_cpi_win_spearman.append(
                    _spearman(valid_pred_cpi, valid_label_cpi)
                )
                slowest_label_i = max(
                    range(len(valid_label_cpi)),
                    key=lambda i: valid_label_cpi[i],
                )
                pred_order = sorted(
                    range(len(valid_pred_cpi)),
                    key=lambda i: valid_pred_cpi[i],
                    reverse=True,
                )
                slowest_top1_hit += int(pred_order[0] == slowest_label_i)
                slowest_top2_hit += int(
                    slowest_label_i in pred_order[:min(2, len(pred_order))]
                )
                slowest_top_cnt += 1
            if len(pred_start_vals) >= 2:
                align_pred_start_skew.append(
                    max(pred_start_vals) - min(pred_start_vals)
                )
            if len(true_start_vals) >= 2:
                align_true_start_skew.append(
                    max(true_start_vals) - min(true_start_vals)
                )
            if mem_sink.enabled():
                lines, mem_sink.event_seq = build_serial_mem_event_lines(
                    per_core_wins=per_core_wins,
                    pred_pmu=pred_pmu,
                    uops_per_core=step["uops"],
                    pred_start_cycle=pred_start_cycle,
                    workload=workload,
                    window_id=windows,
                    seq_start=mem_sink.event_seq,
                )
                mem_sink.emit_window(
                    lines, windows, args.shared_system_flush_windows)
            if dump_fh is not None:
                core_summaries = []
                hidden_summaries = []
                core_rows = []
                for ci, c in enumerate(window_cores):
                    win = per_core_wins[c]
                    _summary_tokens, summary = build_core_summary_tokens(win)
                    hidden = summarize_hidden_diagnostics(win)
                    label_pmu = aggregate_pmu(
                        win, tick_per_cycle, prev=per_core_prev.get(c)
                    )
                    pred_vals = [
                        float(pred_pmu[ci, ki].item())
                        for ki in range(len(PMU_KEYS))
                    ]
                    label_vals = (
                        {k: float(label_pmu[k]) for k in PMU_KEYS}
                        if label_pmu is not None else
                        {k: float("nan") for k in PMU_KEYS}
                    )
                    label_cpi_uop = label_vals.get("cpi_uop", float("nan"))
                    label_cpi_macro = (
                        float(label_pmu["cpi_macro"])
                        if label_pmu is not None else float("nan")
                    )
                    pred_cpi_uop = pred_vals[CPI_UOP_IDX]
                    instr = float(step["instr_retired"][ci])
                    uops_ci = float(step["uops"][ci])
                    pred_cpi_macro = (
                        pred_cpi_uop * uops_ci / instr if instr > 0 else float("nan")
                    )
                    ticks = [
                        int(w["_commit_tick"]) for w in win
                        if int(w.get("_commit_tick", 0) or 0) > 0
                    ]
                    true_start_tick = min(ticks) if ticks else None
                    true_end_tick = max(ticks) if ticks else None
                    true_start_cycle = (
                        true_start_tick / float(tick_per_cycle)
                        if true_start_tick is not None else float("nan")
                    )
                    true_end_cycle = (
                        true_end_tick / float(tick_per_cycle)
                        if true_end_tick is not None else float("nan")
                    )
                    true_start_cycle_rel = (
                        true_start_cycle - true_cycle_origin
                        if true_start_tick is not None else float("nan")
                    )
                    true_end_cycle_rel = (
                        true_end_cycle - true_cycle_origin
                        if true_end_tick is not None else float("nan")
                    )
                    pred_start_before_c = pred_start_before[c]
                    pred_cycles_delta = pred_cpi_uop * uops_ci
                    pred_end_after_c = pred_start_before_c + pred_cycles_delta
                    core_summaries.append(summary)
                    hidden_summaries.append(hidden)
                    core_rows.append({
                        "core_id": int(c),
                        "cursor_start": int(cursor[c]),
                        "cursor_end": int(win_end[c]),
                        "uops": int(len(win)),
                        "instr_retired": instr,
                        "tokens": int(tok_per_core[c]),
                        "tokens_per_macro": (
                            float(tok_per_core[c]) / instr if instr > 0 else 0.0
                        ),
                        "tokens_per_uop": (
                            float(tok_per_core[c]) / uops_ci if uops_ci > 0 else 0.0
                        ),
                        "pred": {
                            k: pred_vals[ki] for ki, k in enumerate(PMU_KEYS)
                        },
                        "label": label_vals,
                        "pred_cpi_macro": pred_cpi_macro,
                        "label_cpi_macro": label_cpi_macro,
                        "true_start_tick": (
                            int(true_start_tick)
                            if true_start_tick is not None else None
                        ),
                        "true_end_tick": (
                            int(true_end_tick)
                            if true_end_tick is not None else None
                        ),
                        "true_start_cycle": float(true_start_cycle),
                        "true_end_cycle": float(true_end_cycle),
                        "true_start_cycle_rel": float(true_start_cycle_rel),
                        "true_end_cycle_rel": float(true_end_cycle_rel),
                        "true_cycle_span": (
                            float(true_end_cycle - true_start_cycle)
                            if true_start_tick is not None
                            and true_end_tick is not None else float("nan")
                        ),
                        "pred_start_cycle_before": float(pred_start_before_c),
                        "pred_cycle_delta": float(pred_cycles_delta),
                        "pred_end_cycle_after": float(pred_end_after_c),
                        "pred_true_start_cycle_err": (
                            float(pred_start_before_c - true_start_cycle_rel)
                            if true_start_tick is not None else float("nan")
                        ),
                        "pred_true_end_cycle_err": (
                            float(pred_end_after_c - true_end_cycle_rel)
                            if true_end_tick is not None else float("nan")
                        ),
                        "cpi_uop_abs_err": (
                            abs(pred_cpi_uop - label_cpi_uop)
                            if not math.isnan(label_cpi_uop) else float("nan")
                        ),
                        "cpi_uop_rel_err": (
                            abs(pred_cpi_uop - label_cpi_uop) / (abs(label_cpi_uop) + 1e-6)
                            if not math.isnan(label_cpi_uop) else float("nan")
                        ),
                        "summary": summary,
                        "hidden": hidden,
                    })
                win_macro = sum(float(x["instr_retired"]) for x in core_rows)
                win_uops = sum(float(x["uops"]) for x in core_rows)
                win_pred_cyc = sum(
                    float(x["pred"]["cpi_uop"]) * float(x["uops"])
                    for x in core_rows
                )
                valid_label = [
                    x for x in core_rows
                    if not math.isnan(float(x["label"].get("cpi_uop", float("nan"))))
                ]
                win_label_cyc = sum(
                    float(x["label"]["cpi_uop"]) * float(x["uops"])
                    for x in valid_label
                )
                win_pred_cpi_uop = win_pred_cyc / max(win_uops, 1e-9)
                win_pred_cpi_macro = win_pred_cyc / max(win_macro, 1e-9)
                if valid_label:
                    vlabel_uops = sum(float(x["uops"]) for x in valid_label)
                    vlabel_macro = sum(float(x["instr_retired"]) for x in valid_label)
                    win_label_cpi_uop = win_label_cyc / max(vlabel_uops, 1e-9)
                    win_label_cpi_macro = win_label_cyc / max(vlabel_macro, 1e-9)
                else:
                    win_label_cpi_uop = float("nan")
                    win_label_cpi_macro = float("nan")

                def finite_range(rows, key: str) -> float:
                    vals = [
                        float(x[key]) for x in rows
                        if key in x and math.isfinite(float(x[key]))
                    ]
                    return max(vals) - min(vals) if vals else float("nan")

                def finite_mean_abs(rows, key: str) -> float:
                    vals = [
                        abs(float(x[key])) for x in rows
                        if key in x and math.isfinite(float(x[key]))
                    ]
                    return sum(vals) / len(vals) if vals else float("nan")

                dump_obj = {
                    "workload": workload,
                    "window": int(windows),
                    "progress_before": float(sum_uops / max(roi_stats["uops"], 1e-9)),
                    "progress_after": float((sum_uops + win_uops) / max(roi_stats["uops"], 1e-9)),
                    "dt_target": float(planner.dt_target),
                    "load_ema": float(planner.load_ema),
                    "uop_budget": int(planner.uop_budget),
                    "planner": planner.last_plan_stats,
                    "planner_state_source": args.planner_state_source,
                    "fit": {
                        "retries": int(fit_retries),
                        "token_len_est": int(fit_token_len),
                        "max_len": int(args.max_len),
                        "nmin_floor_eff": int(fit_floor_eff),
                        "macro_align_disabled": bool(macro_align_disabled),
                    },
                    "active_cores": [int(c) for c in window_cores],
                    "next_counts": {
                        str(c): int(next_counts[c]) for c in window_cores
                    },
                    "planned_counts": {
                        str(c): int(planned_counts.get(c, 0))
                        for c in window_cores
                    },
                    "token_total_uop_slots": int(sum(tok_per_core.values())),
                    "token_total_window": int(sum(x["tokens"] for x in core_rows)),
                    "uop_total_window": float(win_uops),
                    "macro_total_window": float(win_macro),
                    "true_cycle_origin_tick": int(true_cycle_origin_tick),
                    "true_cycle_origin": float(true_cycle_origin),
                    "alignment": {
                        "pred_start_skew_cycle": finite_range(
                            core_rows, "pred_start_cycle_before"),
                        "pred_end_skew_cycle": finite_range(
                            core_rows, "pred_end_cycle_after"),
                        "true_start_skew_cycle": finite_range(
                            core_rows, "true_start_cycle"),
                        "true_end_skew_cycle": finite_range(
                            core_rows, "true_end_cycle"),
                        "pred_true_start_err_mean_abs": finite_mean_abs(
                            core_rows, "pred_true_start_cycle_err"),
                        "pred_true_end_err_mean_abs": finite_mean_abs(
                            core_rows, "pred_true_end_cycle_err"),
                    },
                    "pred_cpi_uop": float(win_pred_cpi_uop),
                    "label_cpi_uop": float(win_label_cpi_uop),
                    "pred_cpi_macro": float(win_pred_cpi_macro),
                    "label_cpi_macro": float(win_label_cpi_macro),
                    "cpi_uop_residual": (
                        float(win_pred_cpi_uop - win_label_cpi_uop)
                        if not math.isnan(win_label_cpi_uop) else float("nan")
                    ),
                    "cpi_uop_rel_err": (
                        float(abs(win_pred_cpi_uop - win_label_cpi_uop)
                              / (abs(win_label_cpi_uop) + 1e-6))
                        if not math.isnan(win_label_cpi_uop) else float("nan")
                    ),
                    "summary_avg": _avg_core_summaries(core_summaries),
                    "hidden_avg": _avg_hidden_summaries(hidden_summaries),
                    "cores": core_rows,
                }
                dump_fh.write(json.dumps(dump_obj, separators=(",", ":")))
                dump_fh.write("\n")
            for ci, c in enumerate(window_cores):
                label_cpi_uop = float(step["label"][ci][CPI_UOP_IDX])
                pred_vals = [
                    float(pred_pmu[ci, ki].item())
                    for ki in range(len(PMU_KEYS))
                ]
                pred_cpi_uop = pred_vals[CPI_UOP_IDX]
                macro = float(step["instr_retired"][ci])
                uops_ci = float(step["uops"][ci])
                sum_cyc_pred += pred_cpi_uop * uops_ci
                sum_macro += macro
                sum_uops += uops_ci
                # NaN label：本窗 lab 缺失，跳过 ape 累加但 sum_uops/sum_macro/pred 仍记
                if not math.isnan(label_cpi_uop):
                    cpi_ape = (
                        abs(pred_cpi_uop - label_cpi_uop)
                        / (abs(label_cpi_uop) + 1e-6)
                    )
                    ape_sum += cpi_ape
                    ape_cnt += 1.0
                    win_cpi_ape.append(cpi_ape)
                    if columnar_native:
                        label_pmu = (
                            step.get("label_pmu_by_core", {}) or {}
                        ).get(c)
                    else:
                        label_pmu = aggregate_pmu(
                            per_core_wins[c], tick_per_cycle,
                            prev=per_core_prev.get(c),
                        )
                    if label_pmu is not None:
                        _accumulate_pred_pmu(pred_pmu_acc, pred_vals, label_pmu)
                        _accumulate_pmu(label_pmu_acc, label_pmu)
                        for ki, k in enumerate(PMU_KEYS):
                            y = float(label_pmu[k])
                            p = pred_vals[ki]
                            if not math.isnan(y):
                                pmu_ape_sum[k] += abs(p - y) / (abs(y) + 1e-6)
                                pmu_ape_cnt[k] += 1
                split_sum += macro
                split_cnt += 1
                # 端点差累计：跟踪每核首末 commit_tick，最后端点差给出 cycles_label
                if columnar_native:
                    tick_span = _columnar_window_tick_span(
                        columnar_traces[c], win_start[c], win_end[c])
                else:
                    ticks = [w["_commit_tick"] for w in per_core_wins[c]
                             if w["_commit_tick"] > 0]
                    tick_span = (min(ticks), max(ticks)) if ticks else None
                if tick_span is not None:
                    lo, hi = tick_span
                    first_tick[c] = min(first_tick.get(c, lo), lo)
                    last_tick[c] = max(last_tick.get(c, hi), hi)

            # v9 min-uop planner 不再按装载率扩大窗口；这里仅更新诊断 load。
            uops_total = float(sum(step["uops"]))
            planner.update_dt_target(uops_total)

            # 先用本窗预测推进各核 pred_start_cycle，再据此为下一窗做终点对齐
            shared_cpi_by_core = {}
            for ci, c in enumerate(window_cores):
                cursor[c] = win_end[c]
                pred_cpi_uop = float(pred_pmu[ci, CPI_UOP_IDX].item())
                if args.planner_state_source == "tq_forward":
                    ticks = [
                        _commit_tick(w) for w in per_core_wins[c]
                        if _commit_tick(w) > 0
                    ]
                    if ticks:
                        pred_start_cycle[c] = (
                            max(ticks) / float(tick_per_cycle)
                            - true_cycle_origin
                        )
                    shared_cpi_by_core[c] = pred_cpi_uop
                    continue
                state_cpi_uop = pred_cpi_uop
                if args.planner_state_source == "label":
                    label_cpi_for_state = float(step["label"][ci][CPI_UOP_IDX])
                    if math.isfinite(label_cpi_for_state):
                        state_cpi_uop = label_cpi_for_state
                shared_cpi_by_core[c] = state_cpi_uop
                pred_start_cycle[c] += state_cpi_uop * float(step["uops"][ci])
            if shared_state is not None:
                if columnar_native:
                    shared_state.replay_window_cached(
                        per_core_slices=per_core_slices,
                        trace_cache=shared_trace_cache,
                        cores=window_cores,
                        start_cycles=pred_start_before,
                        cpi_by_core=shared_cpi_by_core,
                    )
                else:
                    shared_state.replay_window(
                        per_core_wins=per_core_wins,
                        cores=window_cores,
                        start_cycles=pred_start_before,
                        cpi_by_core=shared_cpi_by_core,
                    )

            remaining_active = [
                c for c in window_cores if cursor[c] < len(merged[c])
            ]
            if remaining_active and args.planner_state_source != "tq_forward":
                active_idx = {c: ci for ci, c in enumerate(window_cores)}
                plan_cpi_uop = []
                for c in remaining_active:
                    ci = active_idx[c]
                    pred_cpi_uop = float(pred_pmu[ci, CPI_UOP_IDX].item())
                    if args.planner_state_source == "label":
                        label_cpi_uop = float(step["label"][ci][CPI_UOP_IDX])
                        plan_cpi_uop.append(
                            label_cpi_uop if math.isfinite(label_cpi_uop)
                            else pred_cpi_uop
                        )
                    else:
                        plan_cpi_uop.append(pred_cpi_uop)
                nxt = planner.plan(
                    pred_cpi_uop=plan_cpi_uop,
                    pred_start_cycle=[pred_start_cycle[c] for c in remaining_active],
                )
                for ci, c in enumerate(remaining_active):
                    next_counts[c] = nxt[ci]

            t_done = time.perf_counter()
            timing_sum["build_s"] += t_build_done - t_win0
            timing_sum["encode_s"] += step["timing"]["encode_s"]
            timing_sum["tensor_s"] += step["timing"]["tensor_s"]
            timing_sum["forward_s"] += step["timing"]["forward_s"]
            timing_sum["update_plan_s"] += t_done - t_update0
            timing_sum["total_s"] += t_done - t_win0
            windows += 1
            if windows % 20 == 0:
                el = time.time() - t0
                run_pred_cpi_uop = sum_cyc_pred / max(sum_uops, 1e-9)
                run_pred_cpi_macro = sum_cyc_pred / max(sum_macro, 1e-9)
                cyc_label_now = sum(
                    (last_tick[c] - first_tick[c]) / tick_per_cycle
                    for c in cores if c in first_tick
                )
                run_label_cpi_uop = cyc_label_now / max(sum_uops, 1e-9)
                run_label_cpi_macro = cyc_label_now / max(sum_macro, 1e-9)
                progress = 100.0 * sum_uops / max(roi_stats["uops"], 1e-9)
                uops_s = sum_uops / max(el, 1e-9)
                denom = max(windows, 1)
                timing_dbg = (
                    f"build={timing_sum['build_s'] / denom * 1000:.1f}ms "
                    f"encode={timing_sum['encode_s'] / denom * 1000:.1f}ms "
                    f"tensor={timing_sum['tensor_s'] / denom * 1000:.1f}ms "
                    f"forward={timing_sum['forward_s'] / denom * 1000:.1f}ms "
                    f"update={timing_sum['update_plan_s'] / denom * 1000:.1f}ms "
                    f"total={timing_sum['total_s'] / denom * 1000:.1f}ms"
                )
                print(
                    f"   [{workload}] {windows} windows ({el:.0f}s) "
                    f"progress={progress:.1f}% uops/s={uops_s:.0f} "
                    f"running cpi_uop pred={run_pred_cpi_uop:.4f} "
                    f"label={run_label_cpi_uop:.4f} "
                    f"roi={roi_stats['cpi_uop']:.4f} | "
                    f"cpi_macro pred={run_pred_cpi_macro:.4f} "
                    f"label={run_label_cpi_macro:.4f} "
                    f"roi={roi_stats['cpi_macro']:.4f} "
                    f"gem5_full={cpi_gem5:.4f} "
                    f"dt={planner.dt_target:.0f}cyc load_ema={planner.load_ema:.2f}",
                    flush=True,
                )
                print(f"      timing(avg/window): {timing_dbg}", flush=True)
    finally:
        mem_sink.close(windows)
        if dump_fh is not None:
            dump_fh.close()

    sum_cyc_label = sum(
        (last_tick[c] - first_tick[c]) / tick_per_cycle
        for c in cores if c in first_tick
    )
    cpi_uop_pred = sum_cyc_pred / max(sum_uops, 1e-9)
    cpi_uop_label = sum_cyc_label / max(sum_uops, 1e-9)
    cpi_macro_pred = sum_cyc_pred / max(sum_macro, 1e-9)
    cpi_macro_label = sum_cyc_label / max(sum_macro, 1e-9)
    pred_pmu_global = _finalize_pmu_acc(pred_pmu_acc)
    label_pmu_global = _finalize_pmu_acc(label_pmu_acc)
    # CPI 的部署侧全局口径仍以连续推进的 cycles 聚合为准。
    pred_pmu_global["cpi_uop"] = cpi_uop_pred
    label_pmu_global["cpi_uop"] = cpi_uop_label
    pmu_window_mape = {
        k: (pmu_ape_sum[k] / pmu_ape_cnt[k] if pmu_ape_cnt[k] else float("nan"))
        for k in PMU_KEYS
    }
    pmu_global = {}
    for k in PMU_KEYS:
        pred_v = pred_pmu_global.get(k)
        label_v = label_pmu_global.get(k)
        roi_v = roi_pmu.get(k)
        gem5_v = gem5_pmu.get(k)
        pmu_global[k] = {
            "pred": pred_v,
            "label": label_v,
            "roi": roi_v,
            "gem5": gem5_v,
            "pred_vs_label": relerr(pred_v, label_v),
            "pred_vs_roi": relerr(pred_v, roi_v),
            "label_vs_roi": relerr(label_v, roi_v),
            "gem5_vs_roi": relerr(gem5_v, roi_v),
            "window_mape": pmu_window_mape[k],
        }
    core_cpi_pred_cv_mean = _mean(core_cpi_pred_cv)
    core_cpi_label_cv_mean = _mean(core_cpi_label_cv)
    core_cpi_cv_ratio = (
        core_cpi_pred_cv_mean / core_cpi_label_cv_mean
        if math.isfinite(core_cpi_pred_cv_mean)
        and math.isfinite(core_cpi_label_cv_mean)
        and abs(core_cpi_label_cv_mean) > 1.0e-12
        else float("nan")
    )
    return {
        "workload": workload,
        "windows": windows,
        "pred_cpi_uop": cpi_uop_pred,
        "label_cpi_uop": cpi_uop_label,
        "pred_cpi_macro": cpi_macro_pred,
        "label_cpi_macro": cpi_macro_label,
        "roi_stats_cpi_uop": roi_stats["cpi_uop"],
        "roi_stats_cpi_macro": roi_stats["cpi_macro"],
        "gem5_cpi_macro": cpi_gem5,
        "pred_vs_label_cpi_uop": relerr(cpi_uop_pred, cpi_uop_label),
        "pred_vs_roi_cpi_uop": relerr(cpi_uop_pred, roi_stats["cpi_uop"]),
        "label_vs_roi_cpi_uop": relerr(cpi_uop_label, roi_stats["cpi_uop"]),
        "pred_vs_label_cpi_macro": relerr(cpi_macro_pred, cpi_macro_label),
        "pred_vs_roi_cpi_macro": relerr(cpi_macro_pred, roi_stats["cpi_macro"]),
        "label_vs_roi_cpi_macro": relerr(cpi_macro_label, roi_stats["cpi_macro"]),
        "gem5_full_vs_roi_cpi_macro": relerr(cpi_gem5, roi_stats["cpi_macro"]),
        "win_mape_cpi_uop": ape_sum / max(ape_cnt, 1.0),
        "win_mape_cpi_uop_p90": _quantile(win_cpi_ape, 0.90),
        "win_mape_cpi_uop_p99": _quantile(win_cpi_ape, 0.99),
        "core_cpi_mape": _mean(core_cpi_rel_err),
        "core_cpi_mape_p50": _quantile(core_cpi_rel_err, 0.50),
        "core_cpi_mape_p90": _quantile(core_cpi_rel_err, 0.90),
        "core_cpi_mape_p99": _quantile(core_cpi_rel_err, 0.99),
        "core_cpi_signed_bias": _mean(core_cpi_signed_rel_err),
        "core_cpi_win_corr": _mean(core_cpi_win_corr),
        "core_cpi_win_spearman": _mean(core_cpi_win_spearman),
        "core_cpi_slowest_top1_acc": (
            slowest_top1_hit / slowest_top_cnt
            if slowest_top_cnt else float("nan")
        ),
        "core_cpi_slowest_top2_acc": (
            slowest_top2_hit / slowest_top_cnt
            if slowest_top_cnt else float("nan")
        ),
        "core_cpi_pred_cv": core_cpi_pred_cv_mean,
        "core_cpi_label_cv": core_cpi_label_cv_mean,
        "core_cpi_cv_ratio": core_cpi_cv_ratio,
        "align_start_err_mean_abs": _mean(align_start_abs_err),
        "align_start_err_p90_abs": _quantile(align_start_abs_err, 0.90),
        "align_end_err_mean_abs": _mean(align_end_abs_err),
        "align_end_err_p90_abs": _quantile(align_end_abs_err, 0.90),
        "align_pred_start_skew_mean": _mean(align_pred_start_skew),
        "align_true_start_skew_mean": _mean(align_true_start_skew),
        "avg_instr_per_core": split_sum / max(split_cnt, 1),
        "sum_macro": sum_macro,
        "sum_uops": sum_uops,
        "sum_cyc_pred": sum_cyc_pred,
        "sum_cyc_label": sum_cyc_label,
        "roi_stats_instr": roi_stats["instr"],
        "roi_stats_uops": roi_stats["uops"],
        "roi_stats_cycles": roi_stats["cycles"],
        "roi_missing_label_uops": roi_stats["missing_label_uops"],
        "roi_pmu_valid_cores": roi_pmu.get("_valid_cores", 0),
        "roi_pmu_valid_uops": roi_pmu.get("_valid_uops", 0),
        "roi_pmu_missing_label_uops": roi_pmu.get("_missing_label_uops", 0),
        "roi_pmu_warmup_filtered_uops": roi_pmu.get("_warmup_filtered_uops", 0),
        "roi_pmu_coverage": (
            float(roi_pmu.get("_valid_uops", 0)) / float(roi_stats["uops"])
            if float(roi_stats["uops"] or 0.0) > 0 else float("nan")
        ),
        "mem_events_path": mem_sink.event_path,
        "shared_system_pmu_path": mem_sink.shared_snapshot_path,
        "window_dump_path": dump_path,
        "pmu_global": pmu_global,
    }


def load_v26_model(args: argparse.Namespace, device: str):
    ckpt_path = os.path.join(args.ckpt, "best.pt")
    if not os.path.isfile(ckpt_path):
        ckpt_path = os.path.join(args.ckpt, "last.pt")
    if not os.path.isfile(ckpt_path):
        raise FileNotFoundError(
            f"v26 checkpoint not found under {args.ckpt}; expected best.pt "
            "or last.pt"
        )

    sd = torch.load(ckpt_path, map_location="cpu")
    schema = str(sd.get("schema", ""))
    if not (schema.startswith("v26") or schema.startswith("v27")) or "doc_qkvr" not in schema:
        raise RuntimeError(
            f"unsupported checkpoint schema {schema!r}; current eval only "
            "accepts v26/v27 doc_qkvr checkpoints"
        )

    from model.v26_kvqr import V26KVQRConfig, V26KVQRModel

    model_cfg = dict(sd["config"])
    model_cfg["sdpa_backend"] = args.sdpa_backend
    model = V26KVQRModel(V26KVQRConfig(**model_cfg))
    model.load_state_dict(sd["model"])
    model = model.to(device)
    if device.startswith("cuda"):
        if args.infer_dtype == "bf16":
            model = model.to(dtype=torch.bfloat16)
        elif args.infer_dtype == "fp16":
            model = model.to(dtype=torch.float16)
    model.eval()
    return model, schema, ckpt_path


def choose_device(device_arg: str | None) -> str:
    if device_arg:
        return device_arg
    return "cuda" if torch.cuda.is_available() else "cpu"


def _fmt_pct(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float) and math.isnan(v):
        return "-"
    return f"{v * 100:.2f}%"


def main() -> None:
    args = parse_args()
    device = choose_device(args.device)
    cfg = load_cfg(args.uarch_config)
    targets = resolve_targets(args.raw_root, args.workload)
    if not targets:
        raise SystemExit("[err] no workloads to evaluate")

    model, schema, ckpt_path = load_v26_model(args, device)
    print(
        f"[init] device={device} ckpt={args.ckpt} max_len={args.max_len} "
        f"planner=min_uop_tail_align seed_n={args.seed_n} "
        f"nmin_target={args.nmin} nmin_floor_min={args.nmin_floor_min} "
        f"query_placement={args.query_placement} "
        f"infer_dtype={args.infer_dtype} "
        f"effective_dtype={next(model.parameters()).dtype} "
        f"sdpa_backend={args.sdpa_backend} "
        f"eval_cache_mode={args.eval_cache_mode} "
        f"model_schema={schema} checkpoint={ckpt_path}",
        flush=True,
    )
    if args.max_len != args.train_max_len:
        print(
            f"[WARN] eval max_len={args.max_len} differs from train_max_len="
            f"{args.train_max_len}; this can increase nmin_eff downgrades and "
            "shift the deployment window distribution.",
            flush=True,
        )
    print("[init] v26 structured model ready", flush=True)
    print("=" * 78, flush=True)
    print("v9部署侧验证（soft-nmin tail-aligned 切窗）", flush=True)
    print("=" * 78, flush=True)

    summary = []
    for name, stats_path in targets:
        trace_dir = os.path.join(args.raw_root, name, "tao_trace")
        if not os.path.isdir(trace_dir):
            print(f"[skip] {name}: missing {trace_dir}", flush=True)
            continue
        print(f"\n## {name}: trace={trace_dir}", flush=True)
        res = eval_workload(
            model, cfg, name, trace_dir, stats_path, args, device,
        )
        print(f"\n## {name}   (windows={res['windows']})", flush=True)
        print(f"  cpi_uop   pred ={res['pred_cpi_uop']:.4f}", flush=True)
        print(f"  cpi_uop   label={res['label_cpi_uop']:.4f}   (窗口标签聚合)", flush=True)
        print(f"  cpi_uop   roi  ={res['roi_stats_cpi_uop']:.4f}   (trace ROI stats)", flush=True)
        print(f"  cpi_macro pred ={res['pred_cpi_macro']:.4f}   (cycles/macros 反推)", flush=True)
        print(f"  cpi_macro label={res['label_cpi_macro']:.4f}   (窗口标签聚合)", flush=True)
        print(f"  cpi_macro roi  ={res['roi_stats_cpi_macro']:.4f}   (trace ROI stats)", flush=True)
        print(f"  cpi_macro gem5 ={res['gem5_cpi_macro']:.4f}   (stats.txt 全程，仅参考)", flush=True)
        print(f"  误差 cpi_uop   pred vs label = {_fmt_pct(res['pred_vs_label_cpi_uop'])}", flush=True)
        print(f"  误差 cpi_uop   pred vs ROI   = {_fmt_pct(res['pred_vs_roi_cpi_uop'])}", flush=True)
        print(f"  参考 cpi_uop   label vs ROI  = {_fmt_pct(res['label_vs_roi_cpi_uop'])}", flush=True)
        print(f"  误差 cpi_macro pred vs label = {_fmt_pct(res['pred_vs_label_cpi_macro'])}", flush=True)
        print(f"  误差 cpi_macro pred vs ROI   = {_fmt_pct(res['pred_vs_roi_cpi_macro'])}", flush=True)
        print(f"  参考 cpi_macro label vs ROI  = {_fmt_pct(res['label_vs_roi_cpi_macro'])}", flush=True)
        print(f"  参考 cpi_macro gem5 vs ROI   = {_fmt_pct(res['gem5_full_vs_roi_cpi_macro'])}", flush=True)
        print(
            "  per-window cpi_uop MAPE = "
            f"mean {_fmt_pct(res['win_mape_cpi_uop'])}, "
            f"p90 {_fmt_pct(res.get('win_mape_cpi_uop_p90'))}, "
            f"p99 {_fmt_pct(res.get('win_mape_cpi_uop_p99'))}",
            flush=True,
        )
        print(
            "  per-core   cpi_uop MAPE = "
            f"mean {_fmt_pct(res.get('core_cpi_mape'))}, "
            f"p50 {_fmt_pct(res.get('core_cpi_mape_p50'))}, "
            f"p90 {_fmt_pct(res.get('core_cpi_mape_p90'))}, "
            f"p99 {_fmt_pct(res.get('core_cpi_mape_p99'))}",
            flush=True,
        )
        print(
            "  per-core   signed bias  = "
            f"{_fmt_pct(res.get('core_cpi_signed_bias'))}; "
            "win corr pearson/spearman = "
            f"{res.get('core_cpi_win_corr', float('nan')):.3f} / "
            f"{res.get('core_cpi_win_spearman', float('nan')):.3f}",
            flush=True,
        )
        print(
            "  slowest core top1/top2  = "
            f"{_fmt_pct(res.get('core_cpi_slowest_top1_acc'))} / "
            f"{_fmt_pct(res.get('core_cpi_slowest_top2_acc'))}; "
            "pred/label core CV = "
            f"{res.get('core_cpi_pred_cv', float('nan')):.3f} / "
            f"{res.get('core_cpi_label_cv', float('nan')):.3f} "
            f"(ratio {res.get('core_cpi_cv_ratio', float('nan')):.3f})",
            flush=True,
        )
        print(
            "  align cycle abs err     = "
            f"start mean/p90 {res.get('align_start_err_mean_abs', float('nan')):.1f} / "
            f"{res.get('align_start_err_p90_abs', float('nan')):.1f}; "
            f"end mean/p90 {res.get('align_end_err_mean_abs', float('nan')):.1f} / "
            f"{res.get('align_end_err_p90_abs', float('nan')):.1f}",
            flush=True,
        )
        print(
            "  start skew pred/true    = "
            f"{res.get('align_pred_start_skew_mean', float('nan')):.1f} / "
            f"{res.get('align_true_start_skew_mean', float('nan')):.1f} cycles",
            flush=True,
        )
        print(f"  ROI uops/instr/cycles   = {res['roi_stats_uops']:.0f} / "
              f"{res['roi_stats_instr']:.0f} / "
              f"{res['roi_stats_cycles']:.1f}", flush=True)
        if res["roi_missing_label_uops"]:
            print(f"  ROI missing label uops = {res['roi_missing_label_uops']}", flush=True)
        print(
            "  ROI PMU coverage      = "
            f"valid_cores={res.get('roi_pmu_valid_cores', 0)} "
            f"valid_uops={res.get('roi_pmu_valid_uops', 0)} "
            f"coverage={_fmt_pct(res.get('roi_pmu_coverage'))}",
            flush=True,
        )
        if res.get("roi_pmu_missing_label_uops", 0):
            print(
                "  ROI PMU missing/warmup uops = "
                f"{res.get('roi_pmu_missing_label_uops', 0)} / "
                f"{res.get('roi_pmu_warmup_filtered_uops', 0)}",
                flush=True,
            )
        if res.get("mem_events_path"):
            print(f"  全局访存序列         = {res['mem_events_path']}", flush=True)
        if res.get("shared_system_pmu_path"):
            print(f"  shared_system PMU   = {res['shared_system_pmu_path']}", flush=True)
        if res.get("window_dump_path"):
            print(f"  逐窗诊断 dump        = {res['window_dump_path']}", flush=True)
        print(f"  平均每核指令数       = {res['avg_instr_per_core']:.1f}", flush=True)
        print("  PMU metrics (global + per-window):", flush=True)
        print(
            "    "
            f"{'pmu':<14} {'pred':>10} {'label':>10} {'roi':>10} {'gem5':>10} "
            f"{'pVl%':>8} {'pVr%':>8} {'lVr%':>8} {'gVr%':>8} {'winMAPE%':>9}",
            flush=True,
        )
        for k in PMU_KEYS:
            m = res["pmu_global"][k]

            def fmt(v):
                return "-" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:.4f}"

            def pct(v):
                return "-" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v*100:.2f}"

            print(
                "    "
                f"{k:<14} {fmt(m['pred']):>10} {fmt(m['label']):>10} "
                f"{fmt(m['roi']):>10} {fmt(m['gem5']):>10} "
                f"{pct(m['pred_vs_label']):>8} {pct(m['pred_vs_roi']):>8} "
                f"{pct(m['label_vs_roi']):>8} {pct(m['gem5_vs_roi']):>8} "
                f"{pct(m['window_mape']):>9}",
                flush=True,
            )
        summary.append(res)

    print("\n" + "=" * 78, flush=True)
    print("Summary", flush=True)
    print("=" * 78, flush=True)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
