"""V10.1 smoke 数据子集构造：从大 parquet 流式抽取前 K row-groups
拼成一个小 parquet，并写入 hive 分区 + meta.json + vocab.json。

源目录布局支持两种：
  A) 多 workload 同根：<src>/workload=<W>/part-000.parquet (+ meta.json + vocab.json)
  B) per-workload 子目录：<src>/<W>/workload=<W>/part-000.parquet (+ meta.json)

为什么不复用 tools/subsample_dataset.py：
  - 旧脚本一次性 pq.read_table 整个 17 GB parquet，会 OOM；
  - smoke 只需 ~30 k 行，按 row_group_size=65536 取 1 个 row group 即可。

输出：
  <out_dir>/workload=<W>/part-000.parquet
  <out_dir>/meta.json
  <out_dir>/vocab.json (从源拷贝)
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


def stream_first_n(in_path: Path, target: int) -> pa.Table:
    pf = pq.ParquetFile(in_path)
    chunks = []
    got = 0
    for i in range(pf.num_row_groups):
        if got >= target:
            break
        rg = pf.read_row_group(i)
        if got + rg.num_rows > target:
            rg = rg.slice(0, target - got)
        chunks.append(rg)
        got += rg.num_rows
    return pa.concat_tables(chunks)


def find_workload_part(src: Path, w: str) -> Path:
    """支持两种布局：A) src/workload=W/part-*.parquet B) src/W/workload=W/part-*.parquet。"""
    cand_a = src / f'workload={w}' / 'part-000.parquet'
    if cand_a.exists():
        return cand_a
    cand_b = src / w / f'workload={w}' / 'part-000.parquet'
    if cand_b.exists():
        return cand_b
    raise FileNotFoundError(
        f'cannot find part-000.parquet for {w} under {src} '
        f'(tried {cand_a} and {cand_b})')


def find_meta(src: Path, w: str) -> Path | None:
    for cand in (src / 'meta.json', src / w / 'meta.json'):
        if cand.exists():
            return cand
    return None


def find_vocab(src: Path, w: str) -> Path | None:
    for cand in (src / 'vocab.json', src / w / 'vocab.json'):
        if cand.exists():
            return cand
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', required=True,
                    help='源根目录（支持 A: <src>/workload=<W>/...  '
                         'B: <src>/<W>/workload=<W>/...）')
    ap.add_argument('--workloads', nargs='+', required=True)
    ap.add_argument('--per-workload', type=int, default=20000)
    ap.add_argument('--out', required=True)
    args = ap.parse_args()

    src = Path(args.src)
    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    out_total = 0
    for w in args.workloads:
        wsrc = find_workload_part(src, w)
        tbl = stream_first_n(wsrc, args.per_workload)
        wdir = out / f'workload={w}'
        wdir.mkdir(parents=True)
        dst = wdir / 'part-000.parquet'
        pq.write_table(tbl, dst,
                       compression='zstd', compression_level=3,
                       row_group_size=65536, use_dictionary=True,
                       data_page_size=1 << 20)
        out_total += tbl.num_rows
        print(f'  {w}: {tbl.num_rows:,} rows -> {dst}')

    # 复用第一个 workload 的 meta 作为骨架
    base_meta = None
    for w in args.workloads:
        cand = find_meta(src, w)
        if cand:
            base_meta = json.loads(cand.read_text())
            break
    if base_meta is None:
        base_meta = {'schema_version': 'v9_5_pq_v1'}

    workload_rows = {w: args.per_workload for w in args.workloads}
    base_meta.update({
        'n_total': out_total,
        'workloads': list(args.workloads),
        'workload_rows': workload_rows,
        'context_len_recommended': 128,
        'source': 'smoke_subset_v10_1',
        'source_dataset': str(src),
        'subsample_target': args.per_workload * len(args.workloads),
    })
    (out / 'meta.json').write_text(json.dumps(base_meta, indent=2))

    for w in args.workloads:
        v = find_vocab(src, w)
        if v:
            shutil.copy(v, out / 'vocab.json')
            break

    print(f'[done] wrote {out_total:,} rows to {out}')


if __name__ == '__main__':
    main()

