"""eval_quota_cycles.py — 方案C（CPI配额自举切窗）部署侧验证。

核心流程：
  1) 从 raw trace 的程序序序列出发，窗口0用 seed_n。
  2) 用当前窗口预测的 CPI 决定下一窗口各核配额：
       N_c(k+1) = clamp(round(dt_target / CPI_pred_c(k)), nmin, nmax)
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
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import torch
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.build_windows import (  # noqa: E402
    annotate_rd_stride,
    aggregate_pmu,
    build_core_summary_tokens,
    is_macro_head,
)
from data.roi_stats import (  # noqa: E402
    compute_trace_roi_stats,
    count_macros,
    load_workload_rows,
    parse_gem5_stats,
)
from model.llm_wrapper import LLMSimModel, WrapperConfig, build_tokenizer  # noqa: E402
from model.regression_head import PMU_KEYS  # noqa: E402
from model import tokenizer as tk  # noqa: E402
from train.loss import invert_pred  # noqa: E402


CPI_IDX = PMU_KEYS.index("cpi")
DENOM_KEYS = {
    "mpki_br": "branch_count",
    "mr_l1d_ld": "loads",
    "mr_l1d_st": "stores",
    "mr_l1i": "fetch_groups",
    "mr_llc": "mem_ops",
    "mshr_avg": "mem_ops",
}
COUNT_KEYS = {"dtlb_miss", "itlb_miss", "inv_recv"}


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
    ap.add_argument("--seed-n", type=int, default=160,
                    help="窗口0每核种子指令数")
    ap.add_argument("--dt-target", type=float, default=1000.0,
                    help="dt_target 初值（cycle）；planner 会按 token 装载率自适应")
    ap.add_argument("--dt-min", type=float, default=200.0,
                    help="dt_target 下限（cycle）")
    ap.add_argument("--dt-max", type=float, default=8000.0,
                    help="dt_target 上限（cycle）")
    ap.add_argument("--dt-alpha", type=float, default=0.3,
                    help="dt_target EWMA 装载率系数")
    ap.add_argument("--dt-target-load", type=float, default=0.95,
                    help="目标 token 装载率（占 budget 比例）")
    ap.add_argument("--rd-window", type=int, default=8192,
                    help="bounded sliding RD 窗口，单位是每核 memory reference 数")
    ap.add_argument("--dt-step-clip", type=float, default=0.3,
                    help="单窗 dt_target 变化幅度上限（±比例）")
    ap.add_argument("--dt-warmup", type=int, default=2,
                    help="dt_target 自适应前预留的 warmup 窗数（不调整 dt）")
    ap.add_argument("--nmin", type=int, default=8)
    ap.add_argument("--tpm-init", type=float, default=30.0,
                    help="冷启动 tokens_per_macro 估计（保守值；首窗即用真实测重校准）")
    ap.add_argument("--ewma-alpha", type=float, default=0.2,
                    help="tokens_per_macro EWMA 平滑系数")
    ap.add_argument("--ucb-lambda", type=float, default=1.0,
                    help="UCB margin = lambda * sigma(tpm)")
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
    instr = float(pmu.get("instr_retired", 0.0) or 0.0)
    denoms = pmu.get("_denoms", {}) or {}
    for k in PMU_KEYS:
        v = float(pmu.get(k, 0.0) or 0.0)
        if k == "cpi":
            den = instr
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
    instr = float(label_pmu.get("instr_retired", 0.0) or 0.0)
    denoms = label_pmu.get("_denoms", {}) or {}
    for i, k in enumerate(PMU_KEYS):
        v = float(pred_vals[i])
        if k == "cpi":
            den = instr
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


def load_cfg(name: str) -> dict:
    cfg_path = "/data00/yinhaolang/LLMSim/config/uarch_configs.yaml"
    with open(cfg_path) as f:
        all_cfg = yaml.safe_load(f)
    return all_cfg["configs"][name]


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
                                 instr_retired: List[float],
                                 pred_start_cycle: Dict[int, float],
                                 workload: str,
                                 window_id: int,
                                 seq_start: int) -> Tuple[List[str], int]:
    events = []
    cores = sorted(per_core_wins.keys())
    for ci, c in enumerate(cores):
        pred_cpi = float(pred_pmu[ci, CPI_IDX].item())
        macro = float(instr_retired[ci])
        win_start = float(pred_start_cycle[c])
        win_cycles = max(0.0, pred_cpi * macro)
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


def encode_sample(hf_tokenizer, cfg: dict, per_core_wins: Dict[int, List[dict]],
                  t_start_rel: List[float], max_len: int) -> dict:
    cfg_tok = tk.cfg_tokens(cfg)
    cores = sorted(per_core_wins.keys())
    tokens: List[str] = ["<SYS>"] + cfg_tok + ["<TRACE>"]
    core_split: List[int] = []
    instr_retired: List[float] = []
    labels: List[List[float]] = []
    for ci, c in enumerate(cores):
        win = per_core_wins[c]
        pmu = aggregate_pmu(win, int(cfg.get("tick_per_cycle", 333)))
        if pmu is None:
            # 窗口含 commit_tick<=0 µop（outer-join 救回的 lab 缺失项）。
            # 推理本身不依赖 commit_tick，单窗 label 不可用：用 NaN 占位
            # 让外层跳过 label / ape 累加，但仍推进 cursor 与 pred。
            labels.append([float("nan")] * len(PMU_KEYS))
        else:
            labels.append([pmu[k] for k in PMU_KEYS])
        tokens.append(f"<C{ci}_BEGIN>")
        summary_tokens, _summary = build_core_summary_tokens(win)
        tokens.extend(summary_tokens)
        for rec in win:
            tokens.extend(tk.encode_uop(rec))
        tokens.append(f"<C{ci}_END>")
        core_split.append(len(win))
        # instr_retired 来自 rec 自身的 macro head 计数，独立于 commit_tick，
        # 保证 Σ sum_macro 与 ROI instr 对齐，不被 NaN-label 窗污染。
        instr_retired.append(float(count_macros(win)))
    tokens.append("<TRACE_END>")
    for ci in range(len(cores)):
        tokens.append(f"<QUERY_C{ci}>")

    ids = hf_tokenizer.convert_tokens_to_ids(tokens)
    if any(i is None or i == hf_tokenizer.unk_token_id for i in ids):
        raise ValueError("tokenizer produced unknown ids")
    if len(ids) > max_len:
        raise ValueError(
            f"tokenized length {len(ids)} exceeds max_len={max_len}; "
            "reduce dt-target or nmax"
        )
    qpos = []
    for ci in range(len(cores)):
        qt = hf_tokenizer.convert_tokens_to_ids(f"<QUERY_C{ci}>")
        pos = len(ids) - 1 - ids[::-1].index(qt)
        qpos.append(pos)
    return {
        "ids": ids,
        "qpos": qpos,
        "label": labels,
        "instr_retired": instr_retired,
        "t_start_rel": t_start_rel,
        "core_split": core_split,
    }


def predict_window(model: LLMSimModel, hf_tokenizer, cfg: dict,
                   per_core_wins: Dict[int, List[dict]],
                   pred_start_cycle: Dict[int, float],
                   use_tstart: bool, device: str,
                   max_len: int) -> dict:
    cores = sorted(per_core_wins.keys())
    min_start = min(pred_start_cycle[c] for c in cores)
    t_start_rel = [float(pred_start_cycle[c] - min_start) for c in cores]
    t_encode0 = time.perf_counter()
    sample = encode_sample(hf_tokenizer, cfg, per_core_wins, t_start_rel, max_len)
    t_tensor0 = time.perf_counter()
    input_ids = torch.tensor([sample["ids"]], dtype=torch.long, device=device)
    attn = torch.ones_like(input_ids, device=device)
    qpos = torch.tensor([sample["qpos"]], dtype=torch.long, device=device)
    if use_tstart:
        ts = torch.tensor([sample["t_start_rel"]], dtype=torch.float32, device=device)
    else:
        ts = None
    t_forward0 = time.perf_counter()
    with torch.no_grad():
        raw = model(input_ids, attn, qpos, ts)
        pmu = invert_pred(raw.float()).cpu()[0]  # [nc,K]
    if device.startswith("cuda"):
        torch.cuda.synchronize()
    t_done = time.perf_counter()
    return {
        "pred_pmu": pmu,
        "label": sample["label"],
        "instr_retired": sample["instr_retired"],
        "t_start_rel": sample["t_start_rel"],
        "core_split": sample["core_split"],
        "timing": {
            "encode_s": t_tensor0 - t_encode0,
            "tensor_s": t_forward0 - t_tensor0,
            "forward_s": t_done - t_forward0,
        },
    }


class OnlineQuotaPlanner:
    """在线 token-budget 配额规划器。

    每核维护 EWMA(tokens_per_macro) + EWMVar，结合终点对齐反馈给出
    "理想 macro 数 → token 需求"，再用 water-filling（按超前程度反向加权）
    把总需求压回 budget 内。

    冷启动：第 0 窗各核 macro 数等分，token 估计采用 tpm_init（保守常数）。
    """

    def __init__(self, n_core: int, max_len: int,
                 tpm_init: float = 3.0, alpha: float = 0.2,
                 lam: float = 1.0, n_min: int = 8,
                 overhead: int = 64, carry_decay: float = 0.5,
                 dt_init: float = 1000.0,
                 dt_min: float = 200.0, dt_max: float = 8000.0,
                 dt_alpha: float = 0.3,
                 dt_target_load: float = 0.95,
                 dt_step_clip: float = 0.3,
                 dt_warmup: int = 2):
        self.n_core = n_core
        self.alpha = alpha
        self.lam = lam
        self.n_min = n_min
        self.budget = max(0, max_len - overhead)
        self.tpm = [tpm_init] * n_core
        self.var = [0.0] * n_core
        self.carry = 0.0
        self.carry_decay = carry_decay
        # dt_target 自适应状态（cycle 单位）
        self.dt_target = float(dt_init)
        self.dt_min = float(dt_min)
        self.dt_max = float(dt_max)
        self.dt_alpha = float(dt_alpha)
        self.dt_target_load = float(dt_target_load)
        self.dt_step_clip = float(dt_step_clip)
        self.dt_warmup = int(dt_warmup)
        self.load_ema = float(dt_target_load)  # 初值锚到目标，避免冷启动剧烈调整
        self.step_count = 0

    def cold_start(self, seed_n: int) -> List[int]:
        """第 0 窗：各核等分 macro 数，受 token budget 约束。

        n0 = min(seed_n, floor(budget / N / tpm_init)) 保证不会因 tpm_init
        过低（保守值）而爆 max_len。
        """
        budget_per_core = self.budget // max(self.n_core, 1)
        cap = max(self.n_min, int(budget_per_core / max(self.tpm[0], 1e-3)))
        n0 = max(self.n_min, min(seed_n, cap))
        return [n0] * self.n_core

    def plan(self, pred_cpi: List[float],
             pred_start_cycle: List[float],
             dt_target: float | None = None) -> List[int]:
        N = self.n_core
        assert len(pred_cpi) == N and len(pred_start_cycle) == N
        if dt_target is None:
            dt_target = self.dt_target

        # Step 1: 终点对齐反推 ideal macro，加 UCB margin 后估 token 需求
        t_end = max(pred_start_cycle) + dt_target
        cpi = [max(c, 1e-4) for c in pred_cpi]
        n_ideal = [
            max(self.n_min, int(round((t_end - pred_start_cycle[c]) / cpi[c])))
            for c in range(N)
        ]
        sigma = [self.var[c] ** 0.5 for c in range(N)]
        T = [n_ideal[c] * (self.tpm[c] + self.lam * sigma[c]) for c in range(N)]

        # Step 2: 检查可行性
        slack = self.budget + self.carry - sum(T)

        # Step 3: 不可行则 water-filling 削减
        if slack < 0:
            shortfall = -slack
            min_start = min(pred_start_cycle)
            p = [max(0.0, pred_start_cycle[c] - min_start) for c in range(N)]
            if sum(p) == 0.0:
                # 罕见：所有核齐步，按 1/CPI 兜底（快核先让）
                p = [1.0 / cpi[c] for c in range(N)]
            sp = sum(p)
            t_floor = [self.n_min * max(self.tpm[c], 1e-3) for c in range(N)]
            for c in range(N):
                room = max(0.0, T[c] - t_floor[c])
                cut = min(room, shortfall * p[c] / sp)
                T[c] -= cut
                shortfall -= cut
            if shortfall > 0:
                # 仍不够：所有核按比例再缩
                scale = max(0.0, (self.budget + self.carry) / max(sum(T), 1e-9))
                T = [t * scale for t in T]

        # Step 4: token → macro 数
        n_c = [
            max(self.n_min, int(T[c] / max(self.tpm[c], 1e-3)))
            for c in range(N)
        ]
        return n_c

    def update(self, c: int, tokens_used: float, macro_used: float) -> None:
        if macro_used <= 0:
            return
        new_tpm = tokens_used / macro_used
        delta = new_tpm - self.tpm[c]
        self.tpm[c] = self.tpm[c] + self.alpha * delta
        # EWMVar (Welford-style indirect): var ← (1-α)(var + α·delta²)
        self.var[c] = (1.0 - self.alpha) * (self.var[c] + self.alpha * delta * delta)

    def update_carry(self, tokens_used_total: float) -> None:
        leftover = max(0.0, self.budget - tokens_used_total)
        self.carry = leftover * self.carry_decay

    def update_dt_target(self, tokens_used_total: float) -> float:
        """根据本窗实际装载率反向调整 dt_target，让下一窗趋近 target_load。

        warmup 内（前 dt_warmup 窗）不调整，给 tpm EWMA 先稳一稳；之后每窗：
            load = tokens_used / budget
            load_ema = α·load + (1-α)·load_ema
            ratio = target_load / max(load_ema, 0.1)   # 装载低 → ratio>1 → 放大 dt
            ratio = clip(ratio, 1-clip, 1+clip)        # 限速防震荡
            dt_new = clip(dt_old × ratio, dt_min, dt_max)
        返回更新后的 dt_target（同时写入 self.dt_target）。
        """
        self.step_count += 1
        if self.step_count <= self.dt_warmup:
            return self.dt_target
        load = tokens_used_total / max(self.budget, 1)
        self.load_ema = (self.dt_alpha * load
                         + (1.0 - self.dt_alpha) * self.load_ema)
        ratio = self.dt_target_load / max(self.load_ema, 0.1)
        ratio = max(1.0 - self.dt_step_clip,
                    min(1.0 + self.dt_step_clip, ratio))
        self.dt_target = max(self.dt_min,
                             min(self.dt_max, self.dt_target * ratio))
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


def eval_workload(model: LLMSimModel, hf_tokenizer, cfg: dict, workload: str,
                  trace_dir: str, stats_path: str, args: argparse.Namespace,
                  device: str, use_tstart: bool) -> dict:
    merged = load_workload_rows(trace_dir)
    for seq in merged.values():
        annotate_rd_stride(seq, rd_window=args.rd_window)
    cores = sorted(merged.keys())
    g_cyc, g_ins, cpi_gem5 = parse_gem5_stats(stats_path)
    tick_per_cycle = int(cfg.get("tick_per_cycle", 333))
    roi_stats = compute_trace_roi_stats(merged, tick_per_cycle)
    roi_pmu = aggregate_trace_pmu(merged, tick_per_cycle)
    roi_pmu["cpi"] = roi_stats["cpi"]
    gem5_pmu = {k: None for k in PMU_KEYS}
    gem5_pmu["cpi"] = cpi_gem5
    cursor = {c: 0 for c in cores}
    pred_start_cycle = {c: 0.0 for c in cores}
    # 用每核 trace 内 commit_tick 端点差作为 ROI cycles 真值；stats.txt
    # 全程 numCycles 可能包含 ROI 外 setup/drain，仅保留为参考。
    first_tick: Dict[int, int] = {}
    last_tick: Dict[int, int] = {}
    planner = OnlineQuotaPlanner(
        n_core=len(cores),
        max_len=args.max_len,
        tpm_init=args.tpm_init,
        alpha=args.ewma_alpha,
        lam=args.ucb_lambda,
        n_min=args.nmin,
        dt_init=args.dt_target,
        dt_min=args.dt_min,
        dt_max=args.dt_max,
        dt_alpha=args.dt_alpha,
        dt_target_load=args.dt_target_load,
        dt_step_clip=args.dt_step_clip,
        dt_warmup=args.dt_warmup,
    )
    next_counts = {c: n for c, n in zip(cores, planner.cold_start(args.seed_n))}
    windows = 0
    sum_cyc_pred = 0.0
    sum_macro = 0.0
    ape_sum = 0.0
    ape_cnt = 0.0
    split_sum = 0.0
    split_cnt = 0
    pmu_ape_sum = {k: 0.0 for k in PMU_KEYS}
    pmu_ape_cnt = {k: 0 for k in PMU_KEYS}
    pred_pmu_acc = _new_pmu_acc()
    label_pmu_acc = _new_pmu_acc()
    timing_sum = {
        "build_s": 0.0,
        "encode_s": 0.0,
        "tensor_s": 0.0,
        "forward_s": 0.0,
        "update_plan_s": 0.0,
        "total_s": 0.0,
    }
    mem_sink = MemEventSink(args, workload)
    t0 = time.time()

    try:
        while True:
            if args.max_windows and windows >= args.max_windows:
                break
            t_win0 = time.perf_counter()
            per_core_wins: Dict[int, List[dict]] = {}
            win_end: Dict[int, int] = {}
            ok = True
            # 切窗 + token 兜底：planner 估的 tpm 可能偏低导致 token 超 budget，
            # 切完后按真实 encode_uop 长度核对，超了就按比例缩 macro 数重切。
            budget_eff = int((args.max_len - 320) * 0.95)  # summary/control token margin
            attempt = 0
            while True:
                per_core_wins.clear()
                win_end.clear()
                ok = True
                tok_total = 0
                tok_per_core: Dict[int, int] = {}
                for c in cores:
                    n = next_counts[c]
                    i = cursor[c]
                    seq = merged[c]
                    end, got = take_macro_window(seq, i, n)
                    if got < n:
                        # 尾部不足一窗：若每核都仍有 nmin 条以上 macro 可凑，则
                        # 强制产出最后一窗（保证 trace 末尾每条 µop 都被推理一次）；
                        # 否则真到末尾，整体停止。
                        if got < args.nmin or end - i < 2:
                            ok = False
                            break
                    per_core_wins[c] = seq[i:end]
                    win_end[c] = end
                    # LLMSim tokenizer encodes each µop into exactly 6 tokens.
                    tok_c = 6 * (end - i)
                    tok_per_core[c] = tok_c
                    tok_total += tok_c
                if not ok or tok_total <= budget_eff or attempt >= 3:
                    break
                scale = budget_eff / tok_total
                shrunk = False
                for c in cores:
                    new_n = max(args.nmin, int(next_counts[c] * scale))
                    if new_n < next_counts[c]:
                        next_counts[c] = new_n
                        shrunk = True
                if not shrunk:
                    break
                attempt += 1
            if not ok:
                break

            t_build_done = time.perf_counter()
            step = predict_window(
                model, hf_tokenizer, cfg, per_core_wins, pred_start_cycle,
                use_tstart, device, args.max_len,
            )
            t_update0 = time.perf_counter()
            pred_pmu = step["pred_pmu"]
            if mem_sink.enabled():
                lines, mem_sink.event_seq = build_serial_mem_event_lines(
                    per_core_wins=per_core_wins,
                    pred_pmu=pred_pmu,
                    instr_retired=step["instr_retired"],
                    pred_start_cycle=pred_start_cycle,
                    workload=workload,
                    window_id=windows,
                    seq_start=mem_sink.event_seq,
                )
                mem_sink.emit_window(
                    lines, windows, args.shared_system_flush_windows)
            for ci, c in enumerate(cores):
                label_cpi = float(step["label"][ci][CPI_IDX])
                pred_vals = [
                    float(pred_pmu[ci, ki].item())
                    for ki in range(len(PMU_KEYS))
                ]
                pred_cpi = pred_vals[CPI_IDX]
                macro = float(step["instr_retired"][ci])
                sum_cyc_pred += pred_cpi * macro
                sum_macro += macro
                # NaN label：本窗 lab 缺失，跳过 ape 累加但 sum_macro / pred 仍记
                if not math.isnan(label_cpi):
                    ape_sum += abs(pred_cpi - label_cpi) / (abs(label_cpi) + 1e-6)
                    ape_cnt += 1.0
                    label_pmu = aggregate_pmu(per_core_wins[c], tick_per_cycle)
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
                ticks = [w["_commit_tick"] for w in per_core_wins[c]
                         if w["_commit_tick"] > 0]
                if ticks:
                    lo, hi = min(ticks), max(ticks)
                    first_tick[c] = min(first_tick.get(c, lo), lo)
                    last_tick[c] = max(last_tick.get(c, hi), hi)

            # 用本窗实测 token 消耗刷新 planner（tokens_per_macro 在线估计）
            tokens_total = 0.0
            for ci, c in enumerate(cores):
                tok_used = float(tok_per_core[c])
                macro_used = float(step["instr_retired"][ci])
                planner.update(c=ci, tokens_used=tok_used, macro_used=macro_used)
                tokens_total += tok_used
            planner.update_carry(tokens_total)
            # dt_target 自适应：根据本窗装载率反向调整下一窗 dt_target
            planner.update_dt_target(tokens_total)

            # 先用本窗预测推进各核 pred_start_cycle，再据此为下一窗做终点对齐
            for ci, c in enumerate(cores):
                cursor[c] = win_end[c]
                pred_cpi = float(pred_pmu[ci, CPI_IDX].item())
                pred_start_cycle[c] += pred_cpi * float(step["instr_retired"][ci])

            nxt = planner.plan(
                pred_cpi=[
                    float(pred_pmu[ci, CPI_IDX].item())
                    for ci in range(len(cores))
                ],
                pred_start_cycle=[pred_start_cycle[c] for c in cores],
            )
            for ci, c in enumerate(cores):
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
                run_pred = sum_cyc_pred / max(sum_macro, 1e-9)
                cyc_label_now = sum(
                    (last_tick[c] - first_tick[c]) / tick_per_cycle
                    for c in cores if c in first_tick
                )
                run_label = cyc_label_now / max(sum_macro, 1e-9)
                progress = 100.0 * sum_macro / max(roi_stats["instr"], 1e-9)
                macro_s = sum_macro / max(el, 1e-9)
                denom = max(windows, 1)
                timing_dbg = (
                    f"build={timing_sum['build_s'] / denom * 1000:.1f}ms "
                    f"encode={timing_sum['encode_s'] / denom * 1000:.1f}ms "
                    f"tensor={timing_sum['tensor_s'] / denom * 1000:.1f}ms "
                    f"forward={timing_sum['forward_s'] / denom * 1000:.1f}ms "
                    f"update={timing_sum['update_plan_s'] / denom * 1000:.1f}ms "
                    f"total={timing_sum['total_s'] / denom * 1000:.1f}ms"
                )
                tpm_dbg = ",".join(f"{x:.2f}" for x in planner.tpm)
                print(
                    f"   [{workload}] {windows} windows ({el:.0f}s) "
                    f"progress={progress:.1f}% macro/s={macro_s:.0f} "
                    f"running pred={run_pred:.4f} label={run_label:.4f} "
                    f"roi={roi_stats['cpi']:.4f} gem5_full={cpi_gem5:.4f} "
                    f"tpm=[{tpm_dbg}] carry={planner.carry:.0f} "
                    f"dt={planner.dt_target:.0f}cyc load_ema={planner.load_ema:.2f}",
                    flush=True,
                )
                print(f"      timing(avg/window): {timing_dbg}", flush=True)
    finally:
        mem_sink.close(windows)

    sum_cyc_label = sum(
        (last_tick[c] - first_tick[c]) / tick_per_cycle
        for c in cores if c in first_tick
    )
    cpi_pred = sum_cyc_pred / max(sum_macro, 1e-9)
    cpi_label = sum_cyc_label / max(sum_macro, 1e-9)
    pred_pmu_global = _finalize_pmu_acc(pred_pmu_acc)
    label_pmu_global = _finalize_pmu_acc(label_pmu_acc)
    # CPI 的部署侧全局口径仍以连续推进的 cycles 聚合为准。
    pred_pmu_global["cpi"] = cpi_pred
    label_pmu_global["cpi"] = cpi_label
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
    return {
        "workload": workload,
        "windows": windows,
        "pred_cpi": cpi_pred,
        "label_cpi": cpi_label,
        "roi_stats_cpi": roi_stats["cpi"],
        "gem5_cpi": cpi_gem5,
        "pred_vs_label": abs(cpi_pred - cpi_label) / abs(cpi_label),
        "pred_vs_roi_stats": abs(cpi_pred - roi_stats["cpi"]) / abs(roi_stats["cpi"]),
        "label_vs_roi_stats": abs(cpi_label - roi_stats["cpi"]) / abs(roi_stats["cpi"]),
        "pred_vs_gem5": abs(cpi_pred - cpi_gem5) / abs(cpi_gem5),
        "label_vs_gem5": abs(cpi_label - cpi_gem5) / abs(cpi_gem5),
        "gem5_full_vs_roi_stats": abs(cpi_gem5 - roi_stats["cpi"]) / abs(roi_stats["cpi"]),
        "win_mape": ape_sum / max(ape_cnt, 1.0),
        "avg_instr_per_core": split_sum / max(split_cnt, 1),
        "sum_macro": sum_macro,
        "sum_cyc_pred": sum_cyc_pred,
        "sum_cyc_label": sum_cyc_label,
        "roi_stats_instr": roi_stats["instr"],
        "roi_stats_cycles": roi_stats["cycles"],
        "roi_missing_label_uops": roi_stats["missing_label_uops"],
        "mem_events_path": mem_sink.event_path,
        "shared_system_pmu_path": mem_sink.shared_snapshot_path,
        "pmu_global": pmu_global,
    }


def load_model_and_tokenizer(args: argparse.Namespace, device: str):
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    tok = build_tokenizer()
    cfg = WrapperConfig(max_len=args.max_len)
    model = LLMSimModel(cfg, tok).to(device)
    lora_dir = os.path.join(args.ckpt, "lora_best")
    if os.path.isdir(lora_dir):
        model.backbone.load_adapter(lora_dir, adapter_name="loaded")
        model.backbone.set_adapter("loaded")
    head_pt = os.path.join(args.ckpt, "head_best.pt")
    use_tstart = False
    if os.path.isfile(head_pt):
        sd = torch.load(head_pt, map_location=device)
        model.head.load_state_dict(sd["head"])
        if "tstart_proj" in sd:
            model.tstart_proj.load_state_dict(sd["tstart_proj"])
        use_tstart = bool(sd.get("use_tstart", False))
        if "new_token_embedding" in sd:
            with torch.no_grad():
                start = sd["new_token_start"]
                emb = model.input_embedding.weight
                emb[start:] = sd["new_token_embedding"].to(emb.dtype).to(device)
        else:
            print("[WARN] ckpt 缺 new_token_embedding，推理结果无效！", flush=True)
    model.eval()
    return model, tok, use_tstart


def choose_device(device_arg: str | None) -> str:
    if device_arg:
        return device_arg
    return "cuda" if torch.cuda.is_available() else "cpu"


def main() -> None:
    args = parse_args()
    device = choose_device(args.device)
    cfg = load_cfg(args.uarch_config)
    targets = resolve_targets(args.raw_root, args.workload)
    if not targets:
        raise SystemExit("[err] no workloads to evaluate")

    print(
        f"[init] device={device} ckpt={args.ckpt} max_len={args.max_len} "
        f"dt_init={args.dt_target} dt_range=[{args.dt_min},{args.dt_max}] "
        f"target_load={args.dt_target_load} dt_alpha={args.dt_alpha} "
        f"seed_n={args.seed_n} nmin={args.nmin} tpm_init={args.tpm_init} "
        f"alpha={args.ewma_alpha} lam={args.ucb_lambda}",
        flush=True,
    )
    model, tok, use_tstart = load_model_and_tokenizer(args, device)
    print(f"[init] model ready, use_tstart={use_tstart}", flush=True)
    print("=" * 78, flush=True)
    print("方案C部署侧验证（CPI配额自举切窗）", flush=True)
    print("=" * 78, flush=True)

    summary = []
    for name, stats_path in targets:
        trace_dir = os.path.join(args.raw_root, name, "tao_trace")
        if not os.path.isdir(trace_dir):
            print(f"[skip] {name}: missing {trace_dir}", flush=True)
            continue
        print(f"\n## {name}: trace={trace_dir}", flush=True)
        res = eval_workload(
            model, tok, cfg, name, trace_dir, stats_path,
            args, device, use_tstart,
        )
        print(f"\n## {name}   (windows={res['windows']})", flush=True)
        print(f"  全局CPI  pred ={res['pred_cpi']:.4f}", flush=True)
        print(f"  全局CPI  label={res['label_cpi']:.4f}   (窗口标签聚合)", flush=True)
        print(f"  全局CPI  roi  ={res['roi_stats_cpi']:.4f}   (trace ROI stats)", flush=True)
        print(f"  全局CPI  gem5 ={res['gem5_cpi']:.4f}   (stats.txt 全程，仅参考)", flush=True)
        print(f"  误差  pred vs label = {res['pred_vs_label']*100:.2f}%", flush=True)
        print(f"  误差  pred vs ROI   = {res['pred_vs_roi_stats']*100:.2f}%", flush=True)
        print(f"  参考  label vs ROI  = {res['label_vs_roi_stats']*100:.2f}%", flush=True)
        print(f"  参考  gem5 vs ROI   = {res['gem5_full_vs_roi_stats']*100:.2f}%", flush=True)
        print(f"  per-window CPI MAPE = {res['win_mape']*100:.2f}%", flush=True)
        print(f"  ROI instr/cycles    = {res['roi_stats_instr']:.0f} / "
              f"{res['roi_stats_cycles']:.1f}", flush=True)
        if res["roi_missing_label_uops"]:
            print(f"  ROI missing label uops = {res['roi_missing_label_uops']}", flush=True)
        if res.get("mem_events_path"):
            print(f"  全局访存序列         = {res['mem_events_path']}", flush=True)
        if res.get("shared_system_pmu_path"):
            print(f"  shared_system PMU   = {res['shared_system_pmu_path']}", flush=True)
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
