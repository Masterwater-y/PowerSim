#!/usr/bin/env python3
"""build_inference_input.py — V9.5 hold-out 验证用推理输入构造器。

数据源：
  --detailed-dir : gem5+ruby 跑出的 records.micro.jsonl（4 个 core 各一份）
                   仅取 *静态* 字段（macro_pc, micro_pc, is_load/store/...,
                   producer_dists, ...），uarch oracle 字段被丢弃。
  --pred-jsonl   : ref_sim 重放 mem_events 后输出的 pred.jsonl
                   commit 行携带模型推理用的 8 字段：mesi_before / coh_oracle /
                   sharer_bucket / owner_dist / dirty_owner / path_class /
                   inval_fanout / same_line_recent (oracle_source=1 fallback)

输出 jsonl 与 build_micro_dataset.py 同 schema (input + uarch_context)，
但 *不含 labels*（推理流），并保留 meta.{workload,core_id,thread_id,micro_seq}。

对齐规则：
  对每个 (core_id, thread_id)，把 records.micro 中 mem-touching 的行按出现顺序
  与 pred.jsonl 中 commit 行按出现顺序 1:1 对应（口径与 gem5 探针 emit
  顺序一致：commit-tick 全序内每核每线程独立递增）。
  - 非 mem_touching 行：uarch_context 全 0，oracle_source=1。

用法：
  build_inference_input.py --detailed-dir <gem5_outdir/tao_trace> \
                           --pred-jsonl   <ref_sim_pred.jsonl> \
                           --workload     <name> \
                           --out          <out.jsonl>
"""
import argparse
import glob
import json
import os
import re
from collections import defaultdict


def core_id_of(name):
    m = re.search(r'cores(\d+)', name)
    return int(m.group(1)) if m else -1


# 静态 input 字段集（与 build_micro_dataset.py 同）
INPUT_FIELDS = [
    'macro_pc', 'micro_pc', 'vaddr', 'size',
    'is_load', 'is_store', 'is_atomic',
    'is_branch', 'is_branch_cond', 'is_branch_indirect',
    'is_call', 'is_return', 'is_int', 'is_fp', 'is_simd',
    'is_serialize', 'is_microop', 'is_last_microop',
    'n_src', 'n_dst', 'producer_dists', 'producer_classes',
]

UCTX_FIELDS = [
    'mesi_before', 'coh_oracle', 'sharer_bucket', 'owner_dist',
    'dirty_owner', 'path_class', 'inval_fanout', 'same_line_recent',
    'oracle_source',
]

UCTX_FIELDS_FROM_PRED = [
    'mesi_before', 'coh_oracle', 'sharer_bucket', 'owner_dist',
    'dirty_owner', 'path_class', 'inval_fanout', 'same_line_recent',
]


def load_pred_commit(pred_path):
    """读取 ref_sim pred.jsonl 的 commit 行；按 (core_id, thread_id) 分组保存。
    每组按出现顺序的 list。"""
    bins = defaultdict(list)
    with open(pred_path) as f:
        for ln in f:
            s = ln.strip()
            if not s.startswith('{'):
                continue
            r = json.loads(s)
            if r.get('event_type', 'commit') != 'commit':
                continue
            key = (int(r.get('core_id', 0)), int(r.get('thread_id', 0)))
            bins[key].append(r)
    return bins


def build_one_core(workload, core_id, records_path, pred_bins, out_fp):
    """records.micro mem-touching 行 ↔ pred.jsonl commit 行 1:1。"""
    n_emit = 0
    n_mt = 0
    n_non_mt = 0
    cursor = defaultdict(int)  # (core,tid) -> 当前消费到 pred_bins 的下标
    with open(records_path) as fr:
        for ln in fr:
            jr = json.loads(ln)
            tid = int(jr['thread_id'])
            mseq = int(jr['micro_seq'])
            mem_touching = bool(jr.get('is_load', 0) or
                                jr.get('is_store', 0) or
                                jr.get('is_atomic', 0))
            uctx = {k: 0 for k in UCTX_FIELDS}
            uctx['oracle_source'] = 1
            uctx['seq_num'] = jr.get('seq_num', 0)
            uctx['paddr'] = jr.get('paddr', 0)
            uctx['cacheline_addr'] = jr.get('cacheline_addr', 0)

            if mem_touching:
                key = (core_id, tid)
                idx = cursor[key]
                bins = pred_bins.get(key, [])
                if idx < len(bins):
                    p = bins[idx]
                    for f in UCTX_FIELDS_FROM_PRED:
                        uctx[f] = int(p.get(f, 0))
                    cursor[key] = idx + 1
                n_mt += 1
            else:
                n_non_mt += 1

            sample = {
                "meta": {
                    "workload": workload,
                    "core_id": core_id,
                    "thread_id": tid,
                    "micro_seq": mseq,
                },
                "input": {f: jr[f] for f in INPUT_FIELDS},
                "uarch_context": uctx,
            }
            out_fp.write(json.dumps(sample, separators=(',', ':')))
            out_fp.write('\n')
            n_emit += 1

    # 检查 cursor 是否消耗完 pred_bins
    leftover = {}
    for key, bins in pred_bins.items():
        if key[0] != core_id:
            continue
        if cursor[key] != len(bins):
            leftover[key] = (cursor[key], len(bins))
    return n_emit, n_mt, n_non_mt, leftover


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--detailed-dir', required=True,
                    help='gem5+ruby outdir 下的 tao_trace 子目录')
    ap.add_argument('--pred-jsonl', required=True,
                    help='ref_sim 重放后输出的 pred.jsonl')
    ap.add_argument('--workload', required=True)
    ap.add_argument('--out', required=True)
    args = ap.parse_args()

    records_files = sorted(
        glob.glob(os.path.join(args.detailed_dir,
                               '*.records.micro.jsonl')),
        key=lambda p: core_id_of(os.path.basename(p)))
    assert records_files, \
        f"no records.micro.jsonl found under {args.detailed_dir}"

    pred_bins = load_pred_commit(args.pred_jsonl)

    total = total_mt = total_non = 0
    with open(args.out, 'w') as out_fp:
        for rf in records_files:
            cid = core_id_of(os.path.basename(rf))
            n, mt, non_mt, leftover = build_one_core(
                args.workload, cid, rf, pred_bins, out_fp)
            print(f"core{cid}: emit={n} mem_touching={mt} non_mt={non_mt}"
                  f" leftover={leftover}")
            total += n
            total_mt += mt
            total_non += non_mt
    print(f"\n=== {args.workload}: total_emit={total} mt={total_mt}"
          f" non_mt={total_non} -> {args.out} ===")


if __name__ == '__main__':
    main()
