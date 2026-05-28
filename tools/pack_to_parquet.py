#!/usr/bin/env python3
"""把 samples_3m.jsonl 转换为列式 parquet 数据集。

设计要点（与 ml/dataset.py 协议绑定）：
  1) Hive 分区：workload=<NAME>/part-000.parquet，每 workload 一个文件。
  2) row_group_size = 65536，配合 zstd-3 + dict encoding，开销最小。
  3) 严格保持原 jsonl 内 (core_id, thread_id, micro_seq) 升序，行内 pos_in_thread
     字段为 thread 内的 0-based 行号；训练时取上下文窗口直接按 pos 计算。
  4) producer_dists / producer_classes 展平为 d0..d3 / pc0..pc3 8 个 int32 列。
  5) macro_pc / micro_pc / vaddr / paddr / cacheline_addr 保留为 uint64 用于
     位移特征；额外预生成 macro_pc_id 整数 token（小词表，<2^20），训练用。
  6) 同步生成 vocab.json + meta.json，训练侧直接消费。
"""
import argparse
import json
import os
import sys
from collections import defaultdict, OrderedDict
import pyarrow as pa
import pyarrow.parquet as pq


SCALAR_BOOL = (
    'is_load', 'is_store', 'is_atomic', 'is_branch', 'is_branch_cond',
    'is_branch_indirect', 'is_call', 'is_return', 'is_int', 'is_fp',
    'is_simd', 'is_serialize', 'is_microop', 'is_last_microop',
)
SCALAR_SMALL_INT = (
    'n_src', 'n_dst', 'size',
    'mesi_before', 'coh_oracle', 'sharer_bucket', 'owner_dist',
    'dirty_owner', 'path_class', 'inval_fanout', 'same_line_recent',
    'oracle_source',
    'i_path_class', 'i_coh_oracle', 'i_mesi_before', 'i_oracle_source',
)
SCALAR_U64 = ('macro_pc', 'micro_pc', 'vaddr', 'paddr', 'cacheline_addr')


def make_schema():
    f = []
    f.append(('core_id', pa.int8()))
    f.append(('thread_id', pa.int16()))
    f.append(('micro_seq', pa.int64()))
    f.append(('pos_in_thread', pa.int32()))
    for k in SCALAR_BOOL:
        f.append((k, pa.int8()))
    for k in SCALAR_SMALL_INT:
        f.append((k, pa.int16()))
    for k in SCALAR_U64:
        f.append((k, pa.uint64()))
    for i in range(4):
        f.append((f'd{i}', pa.int32()))
        # pc 取值 0~6 + sentinel 255（"无生产者"），需要无符号或 int16
        f.append((f'pc{i}', pa.int16()))
    f.append(('macro_pc_id', pa.int32()))
    # labels
    f.append(('fetch_tick', pa.int64()))
    f.append(('ready_tick', pa.int64()))
    f.append(('commit_tick', pa.int64()))
    f.append(('mispredicted', pa.int8()))
    f.append(('fetch_latency', pa.int64()))
    f.append(('execution_latency', pa.int64()))
    return pa.schema([pa.field(n, t) for n, t in f])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--in-jsonl', required=True)
    ap.add_argument('--out-dir', required=True)
    ap.add_argument('--row-group-size', type=int, default=65536)
    ap.add_argument('--compression', default='zstd')
    ap.add_argument('--compression-level', type=int, default=3)
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)

    schema = make_schema()
    names = list(schema.names)
    # 第一遍：流式分组到 per-workload columnar buffer（list-of-list，避免 dict 开销）。
    print('[pass1] streaming jsonl -> per-workload columnar buffers ...', file=sys.stderr)
    by_w_cols = {}                       # workload -> dict[col_name -> list]
    macro_pc_vocab = OrderedDict()       # macro_pc -> id

    n_total = 0
    with open(args.in_jsonl) as f:
        for ln in f:
            s = json.loads(ln)
            m, inp, uc, lb = s['meta'], s['input'], s['uarch_context'], s['labels']
            w = m['workload']
            if w not in by_w_cols:
                by_w_cols[w] = {n: [] for n in names}
            cols = by_w_cols[w]

            mpc = inp['macro_pc']
            if mpc not in macro_pc_vocab:
                macro_pc_vocab[mpc] = len(macro_pc_vocab)
            mpc_id = macro_pc_vocab[mpc]

            cols['core_id'].append(int(m['core_id']))
            cols['thread_id'].append(int(m['thread_id']))
            cols['micro_seq'].append(int(m['micro_seq']))
            cols['pos_in_thread'].append(0)              # 占位，下一阶段重写
            cols['macro_pc_id'].append(mpc_id)
            for k in SCALAR_BOOL:
                cols[k].append(int(inp[k]))
            for k in SCALAR_SMALL_INT:
                v = inp.get(k, uc.get(k, 0))
                cols[k].append(int(v))
            for k in SCALAR_U64:
                v = inp.get(k, uc.get(k, 0))
                cols[k].append(int(v) & ((1 << 64) - 1))
            pds = inp['producer_dists']
            pcs = inp['producer_classes']
            for i in range(4):
                cols[f'd{i}'].append(int(pds[i]) if i < len(pds) else -1)
                cols[f'pc{i}'].append(int(pcs[i]) if i < len(pcs) else -1)
            cols['fetch_tick'].append(int(lb['fetch_tick']))
            cols['ready_tick'].append(int(lb['ready_tick']))
            cols['commit_tick'].append(int(lb['commit_tick']))
            cols['mispredicted'].append(int(lb['mispredicted']))
            cols['fetch_latency'].append(int(lb['fetch_latency']))
            cols['execution_latency'].append(int(lb['execution_latency']))

            n_total += 1
            if n_total % 500000 == 0:
                print(f'  read {n_total:,}', file=sys.stderr)

    print(f'[pass1] total = {n_total:,}, workloads = {len(by_w_cols)}', file=sys.stderr)
    print(f'[pass1] macro_pc unique = {len(macro_pc_vocab):,}', file=sys.stderr)

    # 写 parquet
    import numpy as np
    by_w_count = {}
    by_w_thr = {}
    for w, cols in by_w_cols.items():
        sub = os.path.join(args.out_dir, f'workload={w}')
        os.makedirs(sub, exist_ok=True)
        path = os.path.join(sub, 'part-000.parquet')

        # 1) 转成 numpy，按 (core, tid, micro_seq) 排序
        n = len(cols['core_id'])
        cid = np.asarray(cols['core_id'], dtype=np.int32)
        tid = np.asarray(cols['thread_id'], dtype=np.int32)
        seq = np.asarray(cols['micro_seq'], dtype=np.int64)
        order = np.lexsort((seq, tid, cid))   # 主键 cid > tid > seq
        # 2) pos_in_thread 重算（按 (cid, tid) 分组的 0-based 序号）
        sorted_cid = cid[order]
        sorted_tid = tid[order]
        # 边界：当 (cid,tid) 与前一个不同时重置
        new_thread = np.empty(n, dtype=bool)
        new_thread[0] = True
        new_thread[1:] = (sorted_cid[1:] != sorted_cid[:-1]) | (sorted_tid[1:] != sorted_tid[:-1])
        thread_id_run = np.cumsum(new_thread) - 1
        # group counter via np.diff trick
        pos = np.arange(n, dtype=np.int32)
        # 找每个 thread 的起始位置，pos -= start
        starts = np.where(new_thread)[0]
        thread_start_per_row = starts[thread_id_run]
        pos_in_thread = pos - thread_start_per_row

        n_thr = int(new_thread.sum())

        # 3) 按 order 重排所有列
        arrays = []
        type_map = {f.name: f.type for f in schema}
        for name in names:
            v = cols[name]
            if name == 'pos_in_thread':
                arr = pos_in_thread
            else:
                # 选合适的 numpy dtype，int8/int16 由 schema 决定
                t = type_map[name]
                if t == pa.uint64():
                    arr = np.asarray(v, dtype=np.uint64)[order]
                elif t == pa.int64():
                    arr = np.asarray(v, dtype=np.int64)[order]
                elif t == pa.int32():
                    arr = np.asarray(v, dtype=np.int32)[order]
                elif t == pa.int16():
                    arr = np.asarray(v, dtype=np.int16)[order]
                elif t == pa.int8():
                    arr = np.asarray(v, dtype=np.int8)[order]
                else:
                    arr = np.asarray(v)[order]
            arrays.append(pa.array(arr, type=type_map[name]))
            cols[name] = None  # 释放原 python list

        table = pa.Table.from_arrays(arrays, schema=schema)
        pq.write_table(
            table, path,
            compression=args.compression,
            compression_level=args.compression_level,
            row_group_size=args.row_group_size,
            use_dictionary=True,
            data_page_size=1 << 20,
            write_statistics=True,
        )
        size = os.path.getsize(path)
        by_w_count[w] = n
        by_w_thr[w] = n_thr
        print(f'  wrote {w:20s} rows={n:>10,} threads={n_thr:>3} '
              f'size={size/1e6:>7.1f} MB -> {path}', file=sys.stderr)
        del table, arrays

    # vocab
    vocab = {
        'macro_pc': {f'{k:#x}': v for k, v in macro_pc_vocab.items()},
    }
    with open(os.path.join(args.out_dir, 'vocab.json'), 'w') as f:
        json.dump(vocab, f, separators=(',', ':'))

    meta = {
        'schema_version': 'v9_5_pq_v1',
        'n_total': n_total,
        'row_group_size': args.row_group_size,
        'compression': args.compression,
        'compression_level': args.compression_level,
        'workloads': list(by_w_count.keys()),
        'workload_rows': by_w_count,
        'workload_threads': by_w_thr,
        'context_len_recommended': 128,
        'producer_arity': 4,
    }
    with open(os.path.join(args.out_dir, 'meta.json'), 'w') as f:
        json.dump(meta, f, indent=2)

    print(f'\n[done] dataset @ {args.out_dir}  rows={n_total:,}', file=sys.stderr)


if __name__ == '__main__':
    main()
