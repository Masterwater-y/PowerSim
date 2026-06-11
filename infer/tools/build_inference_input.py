#!/usr/bin/env python3
"""build_inference_input.py — V9.5 hold-out 验证用推理输入构造器。

数据源：
  --detailed-dir : gem5+ruby 跑出的 records.micro.jsonl / labels.micro.jsonl
                   （4 个 core 各一份）
                   仅取 *静态* 字段（macro_pc, micro_pc, is_load/store/...,
                   producer_dists, ...），uarch oracle 字段被丢弃。
  --mem-events-jsonl : derive_mem_events.py 输出；其中 synthetic ifetch 行带
                   fetch_tick + i-side oracle 键，用于与 pred.ifetch 对齐。
  --pred-jsonl   : ref_sim 重放 mem_events 后输出的 pred.jsonl
                   commit 行携带 d-side 字段；ifetch 行携带 i-side 字段。

输出 jsonl 与 build_micro_dataset.py 同 schema (input + uarch_context)，
但 *不含 labels*（推理流），并保留 meta.{workload,core_id,thread_id,micro_seq}。

对齐规则：
  对每个 (core_id, thread_id)，把 records.micro 中 mem-touching 的行按出现顺序
  与 pred.jsonl 中 commit 行按出现顺序 1:1 对应（口径与 gem5 探针 emit
  顺序一致：commit-tick 全序内每核每线程独立递增）。
  - 非 mem_touching 行：uarch_context 全 0，oracle_source=1。

用法：
  build_inference_input.py --detailed-dir <gem5_outdir/tao_trace> \
                           --mem-events-jsonl <all_mem_events.merged.jsonl> \
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
    # P0-A / V10.3 A：ref_sim commit 行可直接提供，推理输入应透传
    'd_mshr_depth', 'dtlb_hit', 'd_walker_levels',
    'd_walker_dram_misses', 'd_bank_id',
    'd_llc_set_residency', 'd_llc_set_lru_pos',
]

UCTX_FIELDS_FROM_PRED = [
    'mesi_before', 'coh_oracle', 'sharer_bucket', 'owner_dist',
    'dirty_owner', 'path_class', 'inval_fanout', 'same_line_recent',
    'd_mshr_depth', 'dtlb_hit', 'd_walker_levels',
    'd_walker_dram_misses', 'd_bank_id',
    'd_llc_set_residency', 'd_llc_set_lru_pos',
]

IFETCH_FIELDS_FROM_PRED = [
    'i_path_class', 'i_coh_oracle', 'i_mesi_before',
    'i_mshr_depth', 'itlb_hit', 'i_walker_levels',
    'i_walker_dram_misses', 'i_bank_id',
    'i_llc_set_residency', 'i_llc_set_lru_pos',
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


def load_pred_ifetch(pred_path, mem_events_path):
    """把 mem_events.ifetch 与 pred.ifetch 按 (seq, core_id) 合并，返回：
    bins[core_id] = [{'fetch_tick', 'i_cl', ...pred fields...}, ...] 按 fetch_tick 排序。"""
    preds = {}
    with open(pred_path) as f:
        for ln in f:
            s = ln.strip()
            if not s.startswith('{'):
                continue
            r = json.loads(s)
            if r.get('event_type') != 'ifetch':
                continue
            preds[(int(r['seq']), int(r.get('core_id', 0)))] = r

    bins = defaultdict(list)
    with open(mem_events_path) as f:
        for ln in f:
            s = ln.strip()
            if not s.startswith('{'):
                continue
            r = json.loads(s)
            if r.get('event_type') != 'ifetch':
                continue
            key = (int(r['seq']), int(r.get('core_id', 0)))
            p = preds.get(key)
            if p is None:
                continue
            rec = {
                'fetch_tick': int(r.get('fetch_tick', r.get('commit_tick', 0))),
                'i_cl': int(r.get('cacheline_addr_v', r.get('cacheline_addr', 0))),
            }
            for fld in IFETCH_FIELDS_FROM_PRED:
                rec[fld] = int(p.get(fld, 0))
            bins[int(r.get('core_id', 0))].append(rec)
    for cid in bins:
        bins[cid].sort(key=lambda x: (x['fetch_tick'], x['i_cl']))
    return bins


def build_one_core(workload, core_id, records_path, labels_path,
                   pred_bins, ifetch_bins, out_fp):
    """records.micro mem-touching 行 ↔ pred.jsonl commit 行 1:1。"""
    n_emit = 0
    n_mt = 0
    n_non_mt = 0
    cursor = defaultdict(int)  # (core,tid) -> 当前消费到 pred_bins 的下标
    i_events = ifetch_bins.get(core_id, [])
    i_cursor = 0
    last_i_attr_by_cl = {}
    with open(records_path) as fr, open(labels_path) as fl:
        for ln, ll in zip(fr, fl):
            jr = json.loads(ln)
            jl = json.loads(ll)
            tid = int(jr['thread_id'])
            mseq = int(jr['micro_seq'])
            fetch_tick = int(jl['fetch_tick'])
            mem_touching = bool(jr.get('is_load', 0) or
                                jr.get('is_store', 0) or
                                jr.get('is_atomic', 0))
            uctx = {k: 0 for k in UCTX_FIELDS}
            uctx['oracle_source'] = 1
            uctx['i_oracle_source'] = 1
            uctx['seq_num'] = jr.get('seq_num', 0)
            uctx['paddr'] = jr.get('paddr', 0)
            uctx['cacheline_addr'] = jr.get('cacheline_addr', 0)
            uctx['cacheline_paddr'] = int(jr.get('paddr', 0)) & ~0x3F

            i_cl = int(jr.get('macro_pc', 0)) & ~63
            while i_cursor < len(i_events) and i_events[i_cursor]['fetch_tick'] <= fetch_tick:
                ev = i_events[i_cursor]
                last_i_attr_by_cl[ev['i_cl']] = ev
                i_cursor += 1
            if i_cl in last_i_attr_by_cl:
                i_ev = last_i_attr_by_cl[i_cl]
                for f in IFETCH_FIELDS_FROM_PRED:
                    uctx[f] = int(i_ev.get(f, 0))
                uctx['i_oracle_source'] = 0

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
    ap.add_argument('--mem-events-jsonl', required=True,
                    help='derive_mem_events.py 输出（提供 ifetch 时间/line 键）')
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
    pred_ifetch_bins = load_pred_ifetch(args.pred_jsonl, args.mem_events_jsonl)

    total = total_mt = total_non = 0
    with open(args.out, 'w') as out_fp:
        for rf in records_files:
            lf = rf.replace('.records.micro.jsonl', '.labels.micro.jsonl')
            cid = core_id_of(os.path.basename(rf))
            n, mt, non_mt, leftover = build_one_core(
                args.workload, cid, rf, lf, pred_bins, pred_ifetch_bins, out_fp)
            print(f"core{cid}: emit={n} mem_touching={mt} non_mt={non_mt}"
                  f" leftover={leftover}")
            total += n
            total_mt += mt
            total_non += non_mt
    print(f"\n=== {args.workload}: total_emit={total} mt={total_mt}"
          f" non_mt={total_non} -> {args.out} ===")


if __name__ == '__main__':
    main()
