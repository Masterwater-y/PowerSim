#!/usr/bin/env python3
"""从各 workload 的去重 parquet 中按 workload 均衡抽样，并重建全局 macro_pc_id。

输入目录布局：
  IN_ROOT/<workload>/workload=<workload>/part-000.parquet

输出目录布局：
  OUT_DIR/workload=<workload>/part-000.parquet
  OUT_DIR/vocab.json
  OUT_DIR/meta.json

抽样使用连续 block，避免二次稀疏 stride 把训练窗口打得过碎。
"""
from __future__ import annotations

import argparse
import json
import shutil
from collections import OrderedDict
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from latency_report import build_latency_report, print_latency_report


def find_parts(root: Path) -> dict[str, Path]:
    parts = {}
    for p in sorted(root.glob("*/workload=*/part-*.parquet")):
        workload = p.parent.name.split("=", 1)[1]
        parts[workload] = p
    if not parts:
        for p in sorted(root.glob("workload=*/part-*.parquet")):
            workload = p.parent.name.split("=", 1)[1]
            parts[workload] = p
    return parts


def block_pick_indices(tbl: pa.Table, target: int, block_size: int) -> np.ndarray:
    n = tbl.num_rows
    if target >= n:
        return np.arange(n, dtype=np.int64)

    core = tbl["core_id"].to_numpy(zero_copy_only=False).astype(np.int64)
    tid = tbl["thread_id"].to_numpy(zero_copy_only=False).astype(np.int64)
    keys = (core << 32) | tid
    breaks = np.flatnonzero(keys[1:] != keys[:-1]) + 1
    starts = np.concatenate([[0], breaks])
    ends = np.concatenate([breaks, [n]])
    sizes = ends - starts

    quotas = np.floor(sizes * target / sizes.sum()).astype(np.int64)
    quotas = np.minimum(quotas, sizes)
    diff = target - int(quotas.sum())
    order = np.argsort(-sizes)
    i = 0
    while diff > 0:
        j = order[i % len(order)]
        if quotas[j] < sizes[j]:
            quotas[j] += 1
            diff -= 1
        i += 1

    selected = []
    for s, e, q in zip(starts, ends, quotas):
        if q <= 0:
            continue
        length = int(e - s)
        if q >= length:
            selected.append(np.arange(s, e, dtype=np.int64))
            continue
        bs = min(block_size, int(q))
        n_blocks = max(1, int(np.ceil(q / bs)))
        if n_blocks == 1:
            block_starts = np.array([0], dtype=np.int64)
        else:
            max_start = max(0, length - bs)
            block_starts = np.linspace(0, max_start, n_blocks, dtype=np.int64)
        local = []
        remaining = int(q)
        for b in block_starts:
            take = min(bs, remaining)
            local.append(np.arange(s + b, s + b + take, dtype=np.int64))
            remaining -= take
            if remaining <= 0:
                break
        selected.append(np.concatenate(local))

    idx = np.unique(np.concatenate(selected))
    if idx.size > target:
        idx = idx[:target]
    if idx.size < target:
        missing = target - idx.size
        mask = np.ones(n, dtype=bool)
        mask[idx] = False
        extra = np.flatnonzero(mask)[:missing]
        idx = np.sort(np.concatenate([idx, extra]))
    return idx.astype(np.int64)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in-root", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--target-total", type=int, default=10_000_000)
    ap.add_argument("--block-size", type=int, default=4096)
    ap.add_argument("--allow-underfilled", action="store_true")
    args = ap.parse_args()

    in_root = Path(args.in_root)
    out_dir = Path(args.out_dir)
    parts = find_parts(in_root)
    if not parts:
        raise SystemExit(f"no parquet parts found under {in_root}")

    workloads = sorted(parts)
    per_w = args.target_total // len(workloads)
    remainder = args.target_total - per_w * len(workloads)

    selected: dict[str, tuple[Path, np.ndarray, int]] = {}
    total_selected = 0
    print(f"[plan] target_total={args.target_total:,} workloads={len(workloads)} per_workload={per_w:,}")
    for i, w in enumerate(workloads):
        quota = per_w + (1 if i < remainder else 0)
        tbl_meta = pq.read_metadata(parts[w])
        n = tbl_meta.num_rows
        if n < quota and not args.allow_underfilled:
            raise SystemExit(
                f"workload {w} has only {n:,} rows after dedup, quota={quota:,}; "
                "rerun collection or use --allow-underfilled")
        tbl = pq.read_table(parts[w], columns=["core_id", "thread_id"])
        idx = block_pick_indices(tbl, min(quota, n), args.block_size)
        selected[w] = (parts[w], idx, quota)
        total_selected += idx.size
        print(f"[select] {w:24s} dedup_rows={n:>12,} selected={idx.size:>12,}")

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    # Pass 1: global macro_pc vocab over selected rows.
    vocab: OrderedDict[str, int] = OrderedDict()
    for w, (path, idx, _) in selected.items():
        tbl = pq.read_table(path, columns=["macro_pc"])
        macro_pc = tbl["macro_pc"].to_numpy(zero_copy_only=False)[idx]
        for pc in macro_pc:
            key = str(int(pc))
            if key not in vocab:
                vocab[key] = len(vocab)
        print(f"[vocab] {w:24s} cumulative_macro_pc={len(vocab):,}")

    by_w_count = {}
    latency_inputs = {}
    for w, (path, idx, _) in selected.items():
        tbl = pq.read_table(path)
        sub = tbl.take(pa.array(idx))
        macro_pc = sub["macro_pc"].to_numpy(zero_copy_only=False)
        macro_pc_id = np.array([vocab[str(int(pc))] for pc in macro_pc], dtype=np.int32)
        col_idx = sub.schema.get_field_index("macro_pc_id")
        sub = sub.set_column(col_idx, "macro_pc_id", pa.array(macro_pc_id, type=pa.int32()))

        wdir = out_dir / f"workload={w}"
        wdir.mkdir(parents=True)
        pq.write_table(sub, wdir / "part-000.parquet",
                       compression="zstd", compression_level=3,
                       row_group_size=65536, use_dictionary=True,
                       data_page_size=1 << 20)
        by_w_count[w] = sub.num_rows
        latency_inputs[w] = {
            'fetch_latency': sub['fetch_latency'].to_numpy(zero_copy_only=False),
            'execution_latency': sub['execution_latency'].to_numpy(zero_copy_only=False),
        }
        print(f"[write] {w:24s} rows={sub.num_rows:>12,}")

    latency_report = build_latency_report(latency_inputs)
    print_latency_report(latency_report)

    (out_dir / "vocab.json").write_text(json.dumps({
        "macro_pc": vocab,
    }, indent=2))
    (out_dir / "meta.json").write_text(json.dumps({
        "total_rows": total_selected,
        "workloads": workloads,
        "by_workload_count": by_w_count,
        "n_macro_pc": len(vocab),
        "source_dataset": str(in_root),
        "balanced_sample_target": args.target_total,
        "balanced_sample_block_size": args.block_size,
        "latency_quantiles": latency_report,
    }, indent=2))
    print(f"[done] total_selected={total_selected:,} -> {out_dir}")


if __name__ == "__main__":
    main()
