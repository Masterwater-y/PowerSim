#!/usr/bin/env python3
"""数据体检脚本：检查 parquet 训练集是否符合当前 taogen/ml 模型预期。

检查内容：
1. schema / 关键列完整性
2. 各 workload 样本数、head 占比、mispred 占比
3. fetch / execution latency 分布
4. head 与 fetch_latency 的门控一致性
5. 基于 anchor 局部特征签名的“同输入异标签”近似波动分析

说明：
- “same_input” 分析使用 anchor 级局部签名，不编码绝对 macro_pc，也不编码完整
  128 长上下文窗口，因此结论偏保守，但足以用于训练前体检。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import signal
import sys
from dataclasses import dataclass
from typing import Dict, Iterable, List

import numpy as np
import pyarrow.dataset as ds
import pyarrow.parquet as pq

if hasattr(signal, "SIGPIPE"):
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)


REQUIRED_COLS = [
    "fetch_latency",
    "execution_latency",
    "mispredicted",
    "is_fetch_group_head",
    "macro_pc",
    "micro_pc",
    "is_microop",
    "is_last_microop",
    "core_id",
    "thread_id",
    "pos_in_thread",
]


SAME_INPUT_COLS = [
    "is_load",
    "is_store",
    "is_atomic",
    "is_branch",
    "is_branch_cond",
    "is_branch_indirect",
    "is_call",
    "is_return",
    "is_int",
    "is_fp",
    "is_simd",
    "is_serialize",
    "is_microop",
    "is_last_microop",
    "n_src",
    "n_dst",
    "size",
    "micro_pc",
    "mesi_before",
    "coh_oracle",
    "sharer_bucket",
    "owner_dist",
    "dirty_owner",
    "path_class",
    "inval_fanout",
    "same_line_recent",
    "oracle_source",
    "i_path_class",
    "i_coh_oracle",
    "i_mesi_before",
    "i_oracle_source",
    "d_mshr_depth",
    "dtlb_hit",
    "d_walker_levels",
    "d_walker_dram_misses",
    "d_bank_id",
    "i_mshr_depth",
    "itlb_hit",
    "i_walker_levels",
    "i_walker_dram_misses",
    "i_bank_id",
    "d_llc_set_residency",
    "d_llc_set_lru_pos",
    "i_llc_set_residency",
    "i_llc_set_lru_pos",
    "d0",
    "pc0",
    "d1",
    "pc1",
    "d2",
    "pc2",
    "d3",
    "pc3",
    "mem_density_W64",
    "branch_density_W64",
    "unique_cl_W64",
    "cl_reuse_dist_log",
    "pc_freq_W64",
    "time_since_last_branch_log",
    "bank_conflict_W64",
    "unique_cl_W256",
    "unique_cl_W1024",
    "dram_bank_id",
    "dram_bank_freq_W256",
    "dram_row_freq_W256",
    "vaddr",
    "paddr",
    "cacheline_addr",
    "cacheline_paddr",
    "pos_in_thread",
    "fetch_latency",
    "execution_latency",
    "is_fetch_group_head",
    "mispredicted",
    "workload",
]


PRIMES = [
    np.uint64(11400714819323198485),
    np.uint64(14029467366897019727),
    np.uint64(1609587929392839161),
]


@dataclass
class GroupStats:
    count: int
    fetch_min: int
    fetch_max: int
    exec_min: int
    exec_max: int
    head_min: int
    head_max: int
    mis_min: int
    mis_max: int

    @classmethod
    def init(cls, fetch_v: int, exec_v: int, head_v: int, mis_v: int) -> "GroupStats":
        return cls(
            count=1,
            fetch_min=fetch_v,
            fetch_max=fetch_v,
            exec_min=exec_v,
            exec_max=exec_v,
            head_min=head_v,
            head_max=head_v,
            mis_min=mis_v,
            mis_max=mis_v,
        )

    def update(self, fetch_v: int, exec_v: int, head_v: int, mis_v: int) -> None:
        self.count += 1
        self.fetch_min = min(self.fetch_min, fetch_v)
        self.fetch_max = max(self.fetch_max, fetch_v)
        self.exec_min = min(self.exec_min, exec_v)
        self.exec_max = max(self.exec_max, exec_v)
        self.head_min = min(self.head_min, head_v)
        self.head_max = max(self.head_max, head_v)
        self.mis_min = min(self.mis_min, mis_v)
        self.mis_max = max(self.mis_max, mis_v)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="Hive 分区 parquet 根目录")
    ap.add_argument("--sample-mod", type=int, default=16,
                    help="same_input 近似分析的抽样步长；默认每 16 条抽 1 条")
    ap.add_argument("--batch-size", type=int, default=200000)
    ap.add_argument("--out", default="",
                    help="可选：输出完整 JSON 报告到指定路径")
    ap.add_argument("--json-only", action="store_true",
                    help="仅打印 JSON，不打印摘要")
    return ap.parse_args()


def hash_addr_bucket(arr: np.ndarray, n_bucket: int = 16) -> np.ndarray:
    a = (arr.astype(np.uint64) >> np.uint64(6))
    a = a ^ (a >> np.uint64(30))
    a = (a * np.uint64(0xBF58476D1CE4E5B9)) & np.uint64(0xFFFFFFFFFFFFFFFF)
    a = a ^ (a >> np.uint64(27))
    a = (a * np.uint64(0x94D049BB133111EB)) & np.uint64(0xFFFFFFFFFFFFFFFF)
    a = a ^ (a >> np.uint64(31))
    return (a % np.uint64(n_bucket)).astype(np.uint64)


def quantiles(arr: np.ndarray, probs: Iterable[float]) -> Dict[str, float]:
    if len(arr) == 0:
        return {}
    vals = np.quantile(arr.astype(np.float64), list(probs))
    return {str(p): float(v) for p, v in zip(probs, vals)}


def spread_quantiles(vals: List[int]) -> Dict[str, float]:
    if not vals:
        return {}
    arr = np.asarray(vals, dtype=np.float64)
    return {
        "p50": float(np.quantile(arr, 0.5)),
        "p90": float(np.quantile(arr, 0.9)),
        "p99": float(np.quantile(arr, 0.99)),
        "max": float(arr.max()),
    }


def find_parquet_parts(data_root: str) -> List[str]:
    files = sorted(glob.glob(os.path.join(data_root, "workload=*/*.parquet")))
    if not files:
        raise FileNotFoundError(f"未找到 parquet 分片: {data_root}")
    return files


def check_schema(files: List[str]) -> Dict[str, object]:
    schema = pq.read_schema(files[0])
    names = schema.names
    missing = [c for c in REQUIRED_COLS if c not in names]
    return {
        "schema_cols": names,
        "missing_required_cols": missing,
    }


def workload_name(path: str) -> str:
    return os.path.basename(os.path.dirname(path)).split("=", 1)[1]


def analyze_dataset(data_root: str, sample_mod: int, batch_size: int) -> Dict[str, object]:
    files = find_parquet_parts(data_root)
    schema_info = check_schema(files)
    if schema_info["missing_required_cols"]:
        raise RuntimeError(f"缺少关键列: {schema_info['missing_required_cols']}")

    workload_stats: Dict[str, Dict[str, object]] = {}
    null_stats: Dict[str, Dict[str, int]] = {}
    sample_groups: Dict[int, GroupStats] = {}
    sample_rows = 0

    for f in files:
        wl = workload_name(f)
        dset = ds.dataset(f, format="parquet")
        scanner = dset.scanner(
            columns=list(dict.fromkeys(REQUIRED_COLS + SAME_INPUT_COLS)),
            batch_size=batch_size,
        )

        w_rows = 0
        w_head = 0
        w_fetch_pos = 0
        w_fetch_pos_head = 0
        w_fetch_pos_nonhead = 0
        w_misp = 0
        fetch_vals: List[np.ndarray] = []
        fetch_head_vals: List[np.ndarray] = []
        exec_vals: List[np.ndarray] = []
        exec_head_vals: List[np.ndarray] = []
        null_counts = {c: 0 for c in REQUIRED_COLS}

        for rb in scanner.to_batches():
            cols = rb.schema.names
            arr = {c: rb.column(cols.index(c)).to_numpy(zero_copy_only=False) for c in cols}
            n = len(arr["fetch_latency"])
            head = arr["is_fetch_group_head"].astype(np.int64)
            fetch = arr["fetch_latency"].astype(np.float64)
            exe = arr["execution_latency"].astype(np.float64)
            mis = arr["mispredicted"].astype(np.int64)

            w_rows += n
            w_head += int(head.sum())
            w_fetch_pos += int((fetch > 0).sum())
            w_fetch_pos_head += int(((fetch > 0) & (head == 1)).sum())
            w_fetch_pos_nonhead += int(((fetch > 0) & (head == 0)).sum())
            w_misp += int(mis.sum())
            fetch_vals.append(fetch)
            exec_vals.append(exe)
            if (head == 1).any():
                fetch_head_vals.append(fetch[head == 1])
                exec_head_vals.append(exe[head == 1])

            for c in REQUIRED_COLS:
                col = rb.column(cols.index(c))
                null_counts[c] += col.null_count

            mask = (arr["pos_in_thread"].astype(np.int64) % sample_mod) == 0
            m = int(mask.sum())
            if not m:
                continue
            sample_rows += m

            sig = np.zeros(m, dtype=np.uint64)
            for i, k in enumerate(SAME_INPUT_COLS):
                if k in ("fetch_latency", "execution_latency", "is_fetch_group_head",
                         "mispredicted", "workload", "vaddr", "paddr",
                         "cacheline_addr", "cacheline_paddr", "pos_in_thread"):
                    continue
                v = arr[k][mask].astype(np.uint64, copy=False)
                sig = (sig * PRIMES[i % len(PRIMES)] + v + np.uint64(i + 1)) & np.uint64(0xFFFFFFFFFFFFFFFF)

            for i, k in enumerate(("vaddr", "paddr", "cacheline_addr", "cacheline_paddr")):
                v = hash_addr_bucket(arr[k][mask])
                sig = (sig * PRIMES[(i + 1) % len(PRIMES)] + v + np.uint64(101 + i)) & np.uint64(0xFFFFFFFFFFFFFFFF)

            fetch_s = fetch[mask]
            exec_s = exe[mask]
            head_s = head[mask]
            mis_s = mis[mask]
            for s, fv, ev, hv, mv in zip(sig.tolist(), fetch_s.tolist(), exec_s.tolist(),
                                         head_s.tolist(), mis_s.tolist()):
                g = sample_groups.get(s)
                if g is None:
                    sample_groups[s] = GroupStats.init(fv, ev, hv, mv)
                else:
                    g.update(fv, ev, hv, mv)

        fetch_all = np.concatenate(fetch_vals) if fetch_vals else np.array([], dtype=np.int64)
        exec_all = np.concatenate(exec_vals) if exec_vals else np.array([], dtype=np.int64)
        fetch_head = np.concatenate(fetch_head_vals) if fetch_head_vals else np.array([], dtype=np.int64)
        exec_head = np.concatenate(exec_head_vals) if exec_head_vals else np.array([], dtype=np.int64)

        workload_stats[wl] = {
            "rows": w_rows,
            "head_rate": w_head / w_rows if w_rows else 0.0,
            "head_count": w_head,
            "misp_rate": w_misp / w_rows if w_rows else 0.0,
            "fetch_pos_rate": w_fetch_pos / w_rows if w_rows else 0.0,
            "fetch_pos_given_head": w_fetch_pos_head / w_head if w_head else 0.0,
            "fetch_pos_nonhead_rate": w_fetch_pos_nonhead / max(w_rows - w_head, 1),
            "fetch_q": quantiles(fetch_all, [0.5, 0.9, 0.99, 0.999]),
            "fetch_head_q": quantiles(fetch_head, [0.5, 0.9, 0.99, 0.999]),
            "exec_q": quantiles(exec_all, [0.5, 0.9, 0.99, 0.999]),
            "exec_head_q": quantiles(exec_head, [0.5, 0.9, 0.99, 0.999]),
        }
        null_stats[wl] = null_counts

    total_rows = sum(v["rows"] for v in workload_stats.values())
    total_head = sum(v["head_count"] for v in workload_stats.values())

    repeat_groups = 0
    repeat_rows = 0
    fetch_var_groups = 0
    exec_var_groups = 0
    head_var_groups = 0
    mis_var_groups = 0
    fetch_spreads: List[int] = []
    exec_spreads: List[int] = []
    for g in sample_groups.values():
        if g.count < 2:
            continue
        repeat_groups += 1
        repeat_rows += g.count
        if g.fetch_min != g.fetch_max:
            fetch_var_groups += 1
            fetch_spreads.append(g.fetch_max - g.fetch_min)
        if g.exec_min != g.exec_max:
            exec_var_groups += 1
            exec_spreads.append(g.exec_max - g.exec_min)
        if g.head_min != g.head_max:
            head_var_groups += 1
        if g.mis_min != g.mis_max:
            mis_var_groups += 1

    return {
        "dataset": data_root,
        "schema": schema_info,
        "overall": {
            "rows": total_rows,
            "head_rate": total_head / total_rows if total_rows else 0.0,
            "head_count": total_head,
        },
        "by_workload": workload_stats,
        "null_stats": null_stats,
        "same_local_input_sample": {
            "sample_mod": sample_mod,
            "sample_rows": sample_rows,
            "num_groups": len(sample_groups),
            "repeat_groups": repeat_groups,
            "repeat_group_ratio": repeat_groups / max(len(sample_groups), 1),
            "repeat_rows": repeat_rows,
            "repeat_row_ratio": repeat_rows / max(sample_rows, 1),
            "fetch_var_groups": fetch_var_groups,
            "fetch_var_group_ratio_among_repeat": fetch_var_groups / max(repeat_groups, 1),
            "exec_var_groups": exec_var_groups,
            "exec_var_group_ratio_among_repeat": exec_var_groups / max(repeat_groups, 1),
            "head_var_groups": head_var_groups,
            "head_var_group_ratio_among_repeat": head_var_groups / max(repeat_groups, 1),
            "mis_var_groups": mis_var_groups,
            "mis_var_group_ratio_among_repeat": mis_var_groups / max(repeat_groups, 1),
            "fetch_spread": spread_quantiles(fetch_spreads),
            "exec_spread": spread_quantiles(exec_spreads),
            "note": "same_input 使用 anchor 级局部特征近似签名，不包含完整 128 长上下文窗口，因此是保守估计。",
        },
    }


def print_summary(report: Dict[str, object]) -> None:
    overall = report["overall"]
    print("== Dataset Health Summary ==")
    print(f"dataset      : {report['dataset']}")
    print(f"rows         : {overall['rows']}")
    print(f"head_rate    : {overall['head_rate']:.4%} ({overall['head_count']})")
    print("")
    print("== By Workload ==")
    for wl, stats in report["by_workload"].items():
        print(
            f"{wl}: rows={stats['rows']} "
            f"head={stats['head_rate']:.4%} "
            f"misp={stats['misp_rate']:.4%} "
            f"fetch>0|head={stats['fetch_pos_given_head']:.4f} "
            f"fetch>0|nonhead={stats['fetch_pos_nonhead_rate']:.4f}"
        )
    print("")
    same = report["same_local_input_sample"]
    print("== Same Local Input Sample ==")
    print(
        f"sample_rows={same['sample_rows']} groups={same['num_groups']} "
        f"repeat_group_ratio={same['repeat_group_ratio']:.4%} "
        f"repeat_row_ratio={same['repeat_row_ratio']:.4%}"
    )
    print(
        f"fetch_var_ratio={same['fetch_var_group_ratio_among_repeat']:.4%} "
        f"exec_var_ratio={same['exec_var_group_ratio_among_repeat']:.4%} "
        f"head_var_ratio={same['head_var_group_ratio_among_repeat']:.4%}"
    )
    if report["schema"]["missing_required_cols"]:
        print("")
        print("missing_required_cols:", report["schema"]["missing_required_cols"])


def main() -> None:
    args = parse_args()
    report = analyze_dataset(args.data, sample_mod=args.sample_mod, batch_size=args.batch_size)
    try:
        if not args.json_only:
            print_summary(report)
            print("")
        text = json.dumps(report, ensure_ascii=False, indent=2)
        print(text)
        if args.out:
            os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(text + "\n")
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except OSError:
            pass


if __name__ == "__main__":
    main()
