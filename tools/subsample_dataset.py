"""从 3M parquet 子采样到 ~144K 的 mini 数据集，单 epoch 控制在约 1 小时
（按 bs=128、bf16、samples/s≈38～50 实测口径估算：38.6 × 3600 ≈ 139k → 取 144,000）。

策略：
  - 按 workload 等比缩放（保持 W1/W2/W3/W4 分布一致）；
  - 每个 partition 内按 (core_id, thread_id) 分组，组内 stride 采样；
  - 保留 pos_in_thread 顺序，使 ParquetWindowDataset 仍能切连续历史窗口。

输出：与 3M 集相同的目录结构（hive 分区 + vocab.json + meta.json）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq


def stride_pick(n: int, k: int) -> np.ndarray:
    if k >= n:
        return np.arange(n, dtype=np.int64)
    step = n / k
    return np.array([int(i * step) for i in range(k)], dtype=np.int64)


def subsample_partition(in_path: Path, out_path: Path, target: int) -> int:
    tbl = pq.read_table(in_path)
    n = tbl.num_rows
    if target >= n:
        pq.write_table(tbl, out_path,
                       compression='zstd', compression_level=3,
                       row_group_size=65536, use_dictionary=True,
                       data_page_size=1 << 20)
        return n
    core = tbl['core_id'].to_numpy()
    thr = tbl['thread_id'].to_numpy()
    pos = tbl['pos_in_thread'].to_numpy()
    # 先按 (core, thread) group，组内按 pos 排序
    keys = (core.astype(np.int64) << 20) | thr.astype(np.int64)
    order = np.lexsort((pos, keys))
    keys_s = keys[order]
    # 找各组边界
    breaks = np.flatnonzero(np.diff(keys_s)) + 1
    starts = np.concatenate([[0], breaks])
    ends = np.concatenate([breaks, [n]])
    sizes = ends - starts
    # 每组按 size 比例分配 quota
    total = sizes.sum()
    quotas = np.maximum(1, np.round(sizes * target / total).astype(np.int64))
    # 调整总数
    diff = target - quotas.sum()
    if diff != 0:
        idxs = np.argsort(-sizes)
        i = 0
        while diff != 0 and i < len(idxs):
            j = idxs[i]
            adj = 1 if diff > 0 else -1
            new_q = quotas[j] + adj
            if 1 <= new_q <= sizes[j]:
                quotas[j] = new_q
                diff -= adj
            i += 1
            if i == len(idxs):
                i = 0
    selected = []
    for s, e, q in zip(starts, ends, quotas):
        local = stride_pick(int(e - s), int(q))
        selected.append(order[s + local])
    sel = np.sort(np.concatenate(selected))
    new_tbl = tbl.take(pa.array(sel))
    pq.write_table(new_tbl, out_path,
                   compression='zstd', compression_level=3,
                   row_group_size=65536, use_dictionary=True,
                   data_page_size=1 << 20)
    return new_tbl.num_rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--in-dir', default='/data00/yinhaolang/simulators/tmp/dataset_3m_pq')
    ap.add_argument('--out-dir', default='/data00/yinhaolang/simulators/tmp/dataset_144k_pq')
    ap.add_argument('--target', type=int, default=144000)
    args = ap.parse_args()

    in_dir = Path(args.in_dir)
    out_dir = Path(args.out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    parts = sorted(in_dir.glob('workload=*/part-*.parquet'))
    sizes = {}
    for p in parts:
        n = pq.read_metadata(p).num_rows
        w = p.parent.name.split('=')[1]
        sizes[w] = (p, n)
    total = sum(n for _, n in sizes.values())
    quotas = {w: int(n * args.target / total) for w, (_, n) in sizes.items()}
    diff = args.target - sum(quotas.values())
    if diff != 0:
        # 把差值塞给最大的 partition
        big = max(quotas, key=quotas.get)
        quotas[big] += diff
    print(f'target={args.target:,}; per-workload quotas:')
    for w, q in quotas.items():
        print(f'  {w}: {q:,}  (from {sizes[w][1]:,})')

    out_total = 0
    for w, (src, _) in sizes.items():
        dst_dir = out_dir / f'workload={w}'
        dst_dir.mkdir()
        dst = dst_dir / 'part-000.parquet'
        n_out = subsample_partition(src, dst, quotas[w])
        out_total += n_out
        print(f'  written {dst}  rows={n_out:,}')
    # vocab + meta
    for fn in ('vocab.json', 'meta.json'):
        sp = in_dir / fn
        if sp.exists():
            shutil.copy(sp, out_dir / fn)
    # 更新 meta.total_rows
    mp = out_dir / 'meta.json'
    if mp.exists():
        m = json.loads(mp.read_text())
        m['total_rows'] = out_total
        m['source_dataset'] = str(in_dir)
        m['subsample_target'] = args.target
        mp.write_text(json.dumps(m, indent=2))
    print(f'[done] total {out_total:,} rows -> {out_dir}')


if __name__ == '__main__':
    main()
