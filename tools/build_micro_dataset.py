#!/usr/bin/env python3
"""V9.5 训练样本构建器：单源 detailed 投影。

数据源：仅 detailed 一份 trace。
  records.micro.jsonl（features，含 atomic_func 全部静态字段超集 + uarch oracle）
  labels.micro.jsonl  （labels，fetch/issue/ready/commit tick + mispredicted）
  按 (core_id, thread_id, micro_seq) 行级一一对应 join。

V9.4 -> V9.5 关键修订：废弃 atomic 双跑对齐。
  原因：atomic 与 detailed 是两台不同物理特性的 CPU；多线程下任何共享内存读写
  都可能因 timing 不同导致控制流分叉（已实测 core0 ~14k µop mismatch）。
  详见 v2_to_v5_alignment_log.md V9.5 章节。
  records.micro 已是 atomic_func.jsonl 的字段超集，单跑 detailed 即可同时
  产出 features 和 labels，根除对齐难题。

标签语义保持 V9.4 不变（ready-clock 方案）：
  fetch_latency_i     = fetch_tick_i  - fetch_tick_{i-1}     # 首条 = 0；恒 >= 0
  execution_latency_i = ready_tick_i  - fetch_tick_i         # 恒 >= 0

设计取舍：
  - 标签都 >= 0，物理含义清晰，模型回归头都用 ReLU
  - 同一条 micro 的标签只看自身 fetch/ready，不依赖前条 -> 训练稳定
  - sum 不守恒（OoO 重叠下 sum > total），由推理 max 公式自动恢复 total

推理公式（reference）：
  fetch_clock_i = fetch_clock_{i-1} + fetch_lat_i        # 累加
  ready_clock_i = max(ready_clock_{i-1}, fetch_clock_i + execution_latency_i)
  total_cycles  = ready_clock_last - ready_clock_first   # = max(ready_tick) - first_ready

自检：用预测标签的 max 公式累加得到 ready_clock_last，应严格等于真值 max(ready_tick)。
"""
import argparse
import glob
import json
import os
import re
import sys
from collections import defaultdict


SHARED_FIELDS = (  # 已不再用于比对，保留作为 records.micro 的"atomic-like"投影字段集
    'macro_pc', 'micro_pc',
    'is_load', 'is_store', 'is_atomic',
    'is_branch', 'is_branch_cond', 'is_branch_indirect',
    'is_call', 'is_return',
    'is_int', 'is_fp', 'is_simd', 'is_serialize',
    'is_microop', 'is_last_microop',
    'n_src', 'n_dst',
)


def core_id_of(name):
    m = re.search(r'cores(\d+)', name)
    return int(m.group(1)) if m else -1


def build_one_core(workload, core_id, records_path, labels_path,
                   out_fp, stats):
    """V9.5：单源 detailed 投影。
    records.micro 与 labels.micro 由 tao_trace 同一次 commit 写入，行级 1:1 对齐。
    """
    n_emitted = 0

    prev_fetch = {}
    first_fetch = {}
    first_ready = {}
    first_commit = {}
    last_commit = {}
    max_ready = {}
    fetch_clock_acc = {}
    ready_acc = {}
    sum_lat = defaultdict(lambda: [0, 0])
    total_count = defaultdict(int)

    with open(records_path) as fr, open(labels_path) as fl:
        for lr, ll in zip(fr, fl):
            jr = json.loads(lr)
            jl = json.loads(ll)

            tid = jr['thread_id']
            mseq = jr['micro_seq']

            if jl['thread_id'] != tid or jl['micro_seq'] != mseq:
                raise RuntimeError(
                    f"records/labels tid/seq mismatch: "
                    f"records=({tid},{mseq}) labels=("
                    f"{jl['thread_id']},{jl['micro_seq']})")

            ft = jl['fetch_tick']
            ct = jl['commit_tick']
            rt = jl['ready_tick']
            exec_lat = rt - ft   # ready - fetch，恒 >= 0
            assert exec_lat >= 0, \
                f"ready < fetch! tid={tid} mseq={mseq} ft={ft} rt={rt}"
            assert rt <= ct, \
                f"ready > commit! tid={tid} mseq={mseq} rt={rt} ct={ct}"

            key = (core_id, tid)
            if key not in prev_fetch:
                fetch_lat = 0
                first_fetch[key] = ft
                first_ready[key] = rt
                first_commit[key] = ct
                max_ready[key] = rt
                fetch_clock_acc[key] = ft
                ready_acc[key] = rt
            else:
                fetch_lat = ft - prev_fetch[key]
                assert fetch_lat >= 0, \
                    f"fetch_tick 回退! tid={tid} mseq={mseq} " \
                    f"prev_ft={prev_fetch[key]} ft={ft}"
                fetch_clock_acc[key] += fetch_lat
                ready_acc[key] = max(ready_acc[key],
                                     fetch_clock_acc[key] + exec_lat)
                if rt > max_ready[key]:
                    max_ready[key] = rt
            prev_fetch[key] = ft
            last_commit[key] = ct
            sum_lat[key][0] += fetch_lat
            sum_lat[key][1] += exec_lat
            total_count[key] += 1

            sample = {
                "meta": {
                    "workload": workload,
                    "core_id": core_id,
                    "thread_id": tid,
                    "micro_seq": mseq,
                },
                "input": {
                    # 静态 atomic-like features，从 records.micro 取
                    "macro_pc": jr['macro_pc'],
                    "micro_pc": jr['micro_pc'],
                    "vaddr": jr['vaddr'],
                    "size": jr['size'],
                    "is_load": jr['is_load'],
                    "is_store": jr['is_store'],
                    "is_atomic": jr['is_atomic'],
                    "is_branch": jr['is_branch'],
                    "is_branch_cond": jr['is_branch_cond'],
                    "is_branch_indirect": jr['is_branch_indirect'],
                    "is_call": jr['is_call'],
                    "is_return": jr['is_return'],
                    "is_int": jr['is_int'],
                    "is_fp": jr['is_fp'],
                    "is_simd": jr['is_simd'],
                    "is_serialize": jr['is_serialize'],
                    "is_microop": jr['is_microop'],
                    "is_last_microop": jr['is_last_microop'],
                    "n_src": jr['n_src'],
                    "n_dst": jr['n_dst'],
                    "producer_dists": jr['producer_dists'],
                    "producer_classes": jr['producer_classes'],
                },
                "uarch_context": {
                    "seq_num": jr['seq_num'],
                    "paddr": jr['paddr'],
                    "cacheline_addr": jr['cacheline_addr'],
                    "mesi_before": jr['mesi_before'],
                    "coh_oracle": jr['coh_oracle'],
                    "sharer_bucket": jr['sharer_bucket'],
                    "owner_dist": jr['owner_dist'],
                    "dirty_owner": jr['dirty_owner'],
                    "path_class": jr['path_class'],
                    "inval_fanout": jr['inval_fanout'],
                    "same_line_recent": jr['same_line_recent'],
                    "oracle_source": jr['oracle_source'],
                },
                "labels": {
                    "fetch_tick": ft,
                    "ready_tick": rt,
                    "commit_tick": ct,
                    "mispredicted": jl['mispredicted'],
                    "fetch_latency": fetch_lat,
                    "execution_latency": exec_lat,
                },
            }
            out_fp.write(json.dumps(sample, separators=(',', ':')))
            out_fp.write('\n')
            n_emitted += 1

    for key in total_count:
        sf, se = sum_lat[key]
        ff = first_fetch[key]
        fr_ = first_ready[key]
        fc = first_commit[key]
        lc = last_commit[key]
        mr = max_ready[key]
        ready_pred = ready_acc[key]
        truth = mr - fr_
        replay = ready_pred - fr_
        stats['per_thread'].append({
            'core': key[0], 'tid': key[1],
            'count': total_count[key],
            'first_fetch': ff, 'first_ready': fr_, 'first_commit': fc,
            'last_commit': lc, 'max_ready': mr,
            'truth_total': truth,
            'max_replay_total': replay,
            'replay_match': (replay == truth),
            'sum_fetch_lat': sf, 'sum_exec_lat': se,
            'sum_total': sf + se,
            'sum_overshoot': (sf + se) - truth,
        })

    return n_emitted


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--detailed-dir', required=True,
                    help='V9.5 单源：仅需 detailed records.micro + labels.micro')
    ap.add_argument('--workload', required=True)
    ap.add_argument('--out', required=True)
    args = ap.parse_args()

    records_files = sorted(
        glob.glob(os.path.join(args.detailed_dir, '*.records.micro.jsonl')),
        key=lambda p: core_id_of(os.path.basename(p)))
    labels_files = sorted(
        glob.glob(os.path.join(args.detailed_dir, '*.labels.micro.jsonl')),
        key=lambda p: core_id_of(os.path.basename(p)))

    assert len(records_files) == len(labels_files) > 0, \
        f"records/labels file counts mismatch: " \
        f"records={len(records_files)} labels={len(labels_files)}"

    stats = {'per_thread': []}
    total_emit = 0
    with open(args.out, 'w') as out_fp:
        for rf, lf in zip(records_files, labels_files):
            cid = core_id_of(os.path.basename(rf))
            n = build_one_core(args.workload, cid, rf, lf, out_fp, stats)
            print(f"core{cid}: emit={n}")
            total_emit += n

    print(f"\n=== {args.workload}: total_emit={total_emit} -> {args.out} ===")
    print(f"\n--- per-thread max-replay 自检 + sum 重叠度 ---")
    print(f"{'core':>4} {'tid':>3} {'count':>10} "
          f"{'truth':>14} {'max_replay':>14} {'ok':>3} "
          f"{'sum':>14} {'overshoot%':>11}")
    all_ok = True
    total_truth = 0
    total_sum = 0
    for s in stats['per_thread']:
        ok = 'OK' if s['replay_match'] else 'X'
        if not s['replay_match']:
            all_ok = False
        total_truth += s['truth_total']
        total_sum += s['sum_total']
        ov = s['sum_overshoot'] / s['truth_total'] * 100 if s['truth_total'] else 0.0
        print(f"{s['core']:>4} {s['tid']:>3} {s['count']:>10} "
              f"{s['truth_total']:>14} {s['max_replay_total']:>14} {ok:>3} "
              f"{s['sum_total']:>14} {ov:>10.2f}%")
    print(f"\nALL max-replay matches truth: {all_ok}")
    if total_truth:
        global_ov = (total_sum - total_truth) / total_truth * 100
        print(f"GLOBAL sum overshoot vs truth: "
              f"{total_sum-total_truth}/{total_truth} = {global_ov:.2f}% "
              f"(OoO 重叠正常 >0)")
    if not all_ok:
        sys.exit(2)


if __name__ == '__main__':
    main()
