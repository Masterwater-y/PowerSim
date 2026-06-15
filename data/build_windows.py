"""build_windows.py — gem5 ROI 原始 trace -> 窗口级训练样本 jsonl。

严格遵守 docs/design.md 的修正：
  - 程序序切窗（按 micro_seq / pos_in_thread），不依赖 commit_tick 排序输入。
  - 多核拼接按 core_id 段串接（不按 cycle）。
  - 输入只用 functional 字段（records.micro 的架构态子集）。
  - 标签全部窗口聚合 PMU，主目标用比率（CPI / MPKI / miss-rate）。
  - cycles 用窗口内 commit_tick 端点差（除以 tick_per_cycle）。

输入目录结构（taogen gem5 输出）：
  <raw>/<WNAME>/tao_trace/board.processor.cores<C>.core.tao_trace.tao_trace.records.micro.jsonl
  <raw>/<WNAME>/tao_trace/...labels.micro.jsonl

输出：
  <out>/windows.jsonl  每行一个样本（含 tokens / label / 元数据）
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import glob
import json
import os
import re
import shutil
import sys
from collections import defaultdict
from typing import Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from model import tokenizer as tk  # noqa: E402

CORE_RE = re.compile(r"cores(\d+)\.core")

# path_class 阈值（见 config/pmu_keys.yaml）
PC_L2 = 1      # >=1 视为 L1 miss
PC_DRAM = 4    # >=4 视为 LLC miss
# coh_oracle: 2=REMOTE_HIT_CLEAN 3=REMOTE_HIT_DIRTY
COH_REMOTE = {2, 3}

# label 维度顺序（与 pmu_keys.yaml keys 顺序一致）
PMU_KEYS = [
    "cpi", "mpki_br", "mr_l1d_ld", "mr_l1d_st",
    "mr_l1i", "mr_llc", "dtlb_miss", "itlb_miss",
    "inv_recv", "mshr_avg",
]


def load_core_files(trace_dir: str) -> Dict[int, dict]:
    """返回 {core_id: {'rec': path, 'lab': path}}。"""
    out: Dict[int, dict] = defaultdict(dict)
    for p in glob.glob(os.path.join(trace_dir, "*.records.micro.jsonl")):
        m = CORE_RE.search(p)
        if m:
            out[int(m.group(1))]["rec"] = p
    for p in glob.glob(os.path.join(trace_dir, "*.labels.micro.jsonl")):
        m = CORE_RE.search(p)
        if m:
            out[int(m.group(1))]["lab"] = p
    return {c: v for c, v in out.items() if "rec" in v and "lab" in v}


def read_jsonl(path: str) -> List[dict]:
    rows = []
    with open(path) as f:
        for ln in f:
            s = ln.strip()
            if not s.startswith("{"):
                continue
            try:
                rows.append(json.loads(s))
            except Exception:
                pass
    return rows


def merge_rec_lab(recs: List[dict], labs: List[dict]) -> List[dict]:
    """按 (thread_id, micro_seq) 对齐 records 与 labels。"""
    lab_idx = {(r["thread_id"], r["micro_seq"]): r for r in labs}
    merged = []
    for r in recs:
        key = (r["thread_id"], r["micro_seq"])
        lab = lab_idx.get(key)
        if lab is None:
            continue
        r["_commit_tick"] = lab.get("commit_tick", 0)
        r["_mispredicted"] = lab.get("mispredicted", 0)
        merged.append(r)
    # 程序序：按 (thread_id, micro_seq) 升序，不依赖 commit_tick
    merged.sort(key=lambda x: (x["thread_id"], x["micro_seq"]))
    return merged


def is_macro_head(rec: dict, prev: Optional[dict]) -> bool:
    """动态 macro 首条 µop：上一条 macro 结束或 macro_pc 变化。"""
    if prev is None:
        return True
    prev_ended = (prev.get("is_microop", 0) == 0
                  or prev.get("is_last_microop", 0) == 1)
    return prev_ended or rec.get("macro_pc") != prev.get("macro_pc")


def aggregate_pmu(window: List[dict], tick_per_cycle: int) -> Optional[dict]:
    """对一个核窗口聚合 PMU 标签（绝对计数 + 派生比率分母）。"""
    if len(window) < 2:
        return None
    # cycles：窗口内 commit_tick 端点差
    ticks = [w["_commit_tick"] for w in window if w["_commit_tick"] > 0]
    if len(ticks) < 2:
        return None
    cycles = (max(ticks) - min(ticks)) / float(tick_per_cycle)
    if cycles <= 0:
        return None

    instr_retired = 0   # macro 指令数
    branch_count = loads = stores = mem_ops = fetch_groups = 0
    branch_miss = l1d_ld_miss = l1d_st_miss = l1i_miss = llc_miss = 0
    dtlb_miss = itlb_miss = inv_recv = 0
    mshr_sum = mshr_n = 0

    prev = None
    for w in window:
        head = is_macro_head(w, prev)
        if head:
            instr_retired += 1
            fetch_groups += 1
            # i-side miss 仅在 fetch-group head 计
            if int(w.get("i_path_class", 0)) >= PC_L2:
                l1i_miss += 1
            if int(w.get("itlb_hit", 1)) == 0:
                itlb_miss += 1
        is_ld = int(w.get("is_load", 0))
        is_st = int(w.get("is_store", 0))
        is_at = int(w.get("is_atomic", 0))
        if int(w.get("is_branch", 0)):
            branch_count += 1
            if int(w.get("_mispredicted", 0)):
                branch_miss += 1
        pc = int(w.get("path_class", 0))
        if is_ld:
            loads += 1
            if pc >= PC_L2:
                l1d_ld_miss += 1
        if is_st:
            stores += 1
            if pc >= PC_L2:
                l1d_st_miss += 1
        if is_ld or is_st or is_at:
            mem_ops += 1
            if pc >= PC_DRAM:
                llc_miss += 1
            if int(w.get("dtlb_hit", 1)) == 0:
                dtlb_miss += 1
            mshr_sum += int(w.get("d_mshr_depth", 0))
            mshr_n += 1
            if int(w.get("coh_oracle", 0)) in COH_REMOTE:
                inv_recv += 1
        prev = w

    if instr_retired == 0:
        return None

    def safe_div(a, b):
        return float(a) / float(b) if b > 0 else 0.0

    # 标签：绝对值 + 分母（分母来自 functional，可在推理时复算）
    return {
        "cycles": cycles,
        "instr_retired": instr_retired,
        # 比率主目标
        "cpi": safe_div(cycles, instr_retired),
        "mpki_br": safe_div(branch_miss, max(branch_count, 1)),
        "mr_l1d_ld": safe_div(l1d_ld_miss, max(loads, 1)),
        "mr_l1d_st": safe_div(l1d_st_miss, max(stores, 1)),
        "mr_l1i": safe_div(l1i_miss, max(fetch_groups, 1)),
        "mr_llc": safe_div(llc_miss, max(mem_ops, 1)),
        # 计数头（log1p 在 dataset 侧做）
        "dtlb_miss": float(dtlb_miss),
        "itlb_miss": float(itlb_miss),
        "inv_recv": float(inv_recv),
        # direct
        "mshr_avg": safe_div(mshr_sum, max(mshr_n, 1)),
        # 分母（供推理反算绝对值）
        "_denoms": {
            "branch_count": branch_count, "loads": loads,
            "stores": stores, "fetch_groups": fetch_groups,
            "mem_ops": mem_ops,
        },
    }


def build_samples(merged_by_core: Dict[int, List[dict]], wname: str,
                  cfg: dict, W: int, stride: int) -> List[dict]:
    """对齐多核程序序起点切窗，每窗一个样本。"""
    tpc = int(cfg.get("tick_per_cycle", 333))
    cfg_tok = tk.cfg_tokens(cfg)
    cores = sorted(merged_by_core.keys())
    min_len = min(len(merged_by_core[c]) for c in cores)
    samples = []
    seg = 0
    for t in range(0, min_len - W + 1, stride):
        tokens: List[str] = ["<SYS>"] + cfg_tok + ["<TRACE>"]
        labels = []
        core_split = []
        ok = True
        per_core_windows = {}
        for c in cores:
            win = merged_by_core[c][t:t + W]
            pmu = aggregate_pmu(win, tpc)
            if pmu is None:
                ok = False
                break
            per_core_windows[c] = (win, pmu)
        if not ok:
            continue
        for ci, c in enumerate(cores):
            win, pmu = per_core_windows[c]
            tokens.append(f"<C{ci}_BEGIN>")
            for w in win:
                tokens.extend(tk.encode_uop(w))
            tokens.append(f"<C{ci}_END>")
            core_split.append(len(win))
            labels.append([pmu[k] for k in PMU_KEYS])
        tokens.append("<TRACE_END>")
        for ci in range(len(cores)):
            tokens.append(f"<QUERY_C{ci}>")
        samples.append({
            "id": f"{wname},W{W},seg{seg:05d}",
            "workload": wname,
            "cfg_hash": cfg.get("cfg_hash", "A0"),
            "n_core": len(cores),
            "w_ops": W,
            "tokens": tokens,
            "core_split": core_split,
            "label": labels,                 # [n_core, K]
            "label_keys": PMU_KEYS,
            "denoms": [per_core_windows[c][1]["_denoms"] for c in cores],
            "instr_retired": [per_core_windows[c][1]["instr_retired"] for c in cores],
        })
        seg += 1
    return samples


def process_workload(wd: str, raw_root: str, out_dir: str,
                     cfg: dict, window: int, stride: int) -> tuple:
    """单个 workload 构建 shard，返回 (wd, ok, samples, shard_path, message)。"""
    trace_dir = os.path.join(raw_root, wd, "tao_trace")
    if not os.path.isdir(trace_dir):
        return wd, False, 0, "", f"[skip] {wd}: no tao_trace"

    files = load_core_files(trace_dir)
    if len(files) < 2:
        return wd, False, 0, "", f"[skip] {wd}: <2 cores"

    merged_by_core = {}
    for c, fp in files.items():
        recs = read_jsonl(fp["rec"])
        labs = read_jsonl(fp["lab"])
        merged_by_core[c] = merge_rec_lab(recs, labs)

    samples = build_samples(merged_by_core, wd, cfg, window, stride)
    shard_path = os.path.join(out_dir, f"{wd}.jsonl")
    with open(shard_path, "w") as fout:
        for s in samples:
            fout.write(json.dumps(s, separators=(",", ":")) + "\n")
    return wd, True, len(samples), shard_path, f"[ok] {wd}: cores={len(files)} samples={len(samples)}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default="/data00/yinhaolang/LLMSim/data/raw")
    ap.add_argument("--out", default="/data00/yinhaolang/LLMSim/data/windows")
    ap.add_argument("--window", type=int, default=1024)
    ap.add_argument("--stride", type=int, default=512)
    ap.add_argument("--workloads", nargs="*", default=None)
    ap.add_argument("--uarch-config", default="arch_A")
    ap.add_argument("--jobs", type=int, default=max(1, min(os.cpu_count() or 1, 8)))
    args = ap.parse_args()

    import yaml
    cfg_path = "/data00/yinhaolang/LLMSim/config/uarch_configs.yaml"
    with open(cfg_path) as f:
        all_cfg = yaml.safe_load(f)
    cfg = all_cfg["configs"][args.uarch_config]

    os.makedirs(args.out, exist_ok=True)
    out_path = os.path.join(args.out, "windows.jsonl")
    shard_dir = os.path.join(args.out, ".shards")
    wdirs = sorted([d for d in os.listdir(args.raw)
                    if os.path.isdir(os.path.join(args.raw, d))
                    and d.startswith("W") and not d.startswith("probe_")])
    if args.workloads:
        wdirs = [d for d in wdirs if d in args.workloads]

    if not wdirs:
        with open(out_path, "w"):
            pass
        print(f"[done] total samples=0 -> {out_path}")
        return

    if os.path.isdir(shard_dir):
        shutil.rmtree(shard_dir)
    os.makedirs(shard_dir, exist_ok=True)

    total = 0
    results = {}
    jobs = max(1, min(args.jobs, len(wdirs)))
    with cf.ProcessPoolExecutor(max_workers=jobs) as ex:
        future_map = {
            ex.submit(process_workload, wd, args.raw, shard_dir,
                      cfg, args.window, args.stride): wd
            for wd in wdirs
        }
        for fut in cf.as_completed(future_map):
            wd, ok, nsamp, shard_path, msg = fut.result()
            results[wd] = (ok, nsamp, shard_path, msg)
            stream = sys.stdout if ok else sys.stderr
            print(msg, file=stream)

    with open(out_path, "w") as fout:
        for wd in wdirs:
            ok, nsamp, shard_path, _ = results[wd]
            if not ok:
                continue
            with open(shard_path) as fin:
                shutil.copyfileobj(fin, fout)
            total += nsamp

    shutil.rmtree(shard_dir, ignore_errors=True)
    print(f"[done] total samples={total} -> {out_path}")


if __name__ == "__main__":
    main()
