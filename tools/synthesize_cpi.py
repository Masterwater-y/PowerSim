#!/usr/bin/env python3
"""V9.5 hold-out CPI 合成器（口径锁定为 sum/sum 全局聚合）。

对每个 (core_id, thread_id) 用模型预测的 fetch_latency / execution_latency 按
方案 §5.2 锁死的 ready-clock 公式累加：

    fetch_clock[i] = fetch_clock[i-1] + fetch_lat[i]                  # 累加
    ready_clock[i] = max(ready_clock[i-1], fetch_clock[i]+exec_lat[i])
    cycles_pred(c,t) = ready_clock_last  (first_ready=0 起算)

宏指令数：
    N_macro(c,t) = sum( is_last_microop == 1 or is_microop == 0 )

全局 CPI（用户口径）：
    CPI = sum_{c,t} cycles_pred / sum_{c,t} N_macro

同时输出：
  - per-core / per-thread diagnostic（仅展示，不参与全局 CPI 计算）
  - 与 gem5 stats.txt 真值（numCycles, commitStats0.numInsts, cpi）对比

输入：
  --pred-jsonl  : ml/infer.py 输出（每行含 fetch_lat / exec_lat / mispred*）
  --input-jsonl : build_inference_input.py 输出（带 is_microop / is_last_microop）
                  按 (core_id, thread_id, micro_seq) 1:1 对齐
  --gem5-stats  : gem5 stats.txt 路径
  --out-json    : 写入综合报告 JSON

对齐：input_jsonl 行与 pred_jsonl 行同源（pred 是逐行预测），按行序 1:1。
"""
import argparse
import json
import os
import re
import sys
from collections import defaultdict


def load_input(path):
    rows = []
    with open(path) as f:
        for ln in f:
            s = ln.strip()
            if not s.startswith('{'):
                continue
            r = json.loads(s)
            rows.append(r)
    return rows


def load_pred(path):
    rows = []
    with open(path) as f:
        for ln in f:
            s = ln.strip()
            if not s.startswith('{'):
                continue
            rows.append(json.loads(s))
    return rows


def parse_gem5_stats(path):
    """提取 numCycles/commitStats0.numInsts/cpi（per core）。"""
    pat_cycles = re.compile(
        r'^board\.processor\.cores(\d+)\.core\.numCycles\s+(\d+)')
    pat_insts = re.compile(
        r'^board\.processor\.cores(\d+)\.core\.commitStats0\.numInsts\s+(\d+)')
    pat_cpi = re.compile(
        r'^board\.processor\.cores(\d+)\.core\.cpi\s+([\d.]+)')
    cycles, insts, cpis = {}, {}, {}
    with open(path) as f:
        for ln in f:
            m = pat_cycles.match(ln)
            if m:
                cycles[int(m.group(1))] = int(m.group(2))
                continue
            m = pat_insts.match(ln)
            if m:
                insts[int(m.group(1))] = int(m.group(2))
                continue
            m = pat_cpi.match(ln)
            if m:
                cpis[int(m.group(1))] = float(m.group(2))
    return cycles, insts, cpis


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pred-jsonl', required=True)
    ap.add_argument('--input-jsonl', required=True)
    ap.add_argument('--gem5-stats', required=True)
    ap.add_argument('--out-json', required=True)
    ap.add_argument('--require-inst-match', action='store_true',
                    help='要求推理侧 macro 指令数与 gem5 commitStats0.numInsts '
                         '在全局和 per-core 上严格相等；否则报错退出')
    args = ap.parse_args()

    inp = load_input(args.input_jsonl)
    prd = load_pred(args.pred_jsonl)
    if len(inp) != len(prd):
        sys.exit(f'[ERROR] input ({len(inp)}) != pred ({len(prd)})')

    # 按 (core_id, thread_id) 分组、按 micro_seq 升序累加
    rows_by_key = defaultdict(list)
    for ri, rp in zip(inp, prd):
        m = ri['meta']
        key = (int(m['core_id']), int(m['thread_id']))
        rows_by_key[key].append({
            'mseq': int(m['micro_seq']),
            'fetch_lat': float(rp['fetch_lat']),
            'exec_lat': float(rp['exec_lat']),
            'is_microop': int(ri['input'].get('is_microop', 0)),
            'is_last_microop': int(ri['input'].get('is_last_microop', 0)),
            'mispred_hard': int(rp.get('mispred_hard', 0)),
        })
    for k in rows_by_key:
        rows_by_key[k].sort(key=lambda r: r['mseq'])

    per_thread = []
    sum_cycles = 0.0
    sum_macro = 0
    sum_mispred = 0
    for (cid, tid), rows in rows_by_key.items():
        fc = 0.0   # fetch_clock
        rc = 0.0   # ready_clock
        n_macro = 0
        n_mp = 0
        for r in rows:
            fc += r['fetch_lat']
            rc = max(rc, fc + r['exec_lat'])
            # 宏指令计数口径：
            # - 若该条是分解出的 micro-op，则仅最后一条记 1 次；
            # - 若该条本身不是 micro-op（is_microop==0），它就是单条宏指令，
            #   gem5 commitStats0.numInsts 会计 1，因此这里也必须计 1。
            if r['is_last_microop'] or not r['is_microop']:
                n_macro += 1
            n_mp += r['mispred_hard']
        cycles = rc
        cpi = (cycles / n_macro) if n_macro else float('nan')
        per_thread.append({
            'core_id': cid, 'thread_id': tid,
            'cycles_pred': cycles, 'n_macro': n_macro, 'n_micro': len(rows),
            'cpi_pred': cpi, 'mispred_pred_count': n_mp,
        })
        sum_cycles += cycles
        sum_macro += n_macro
        sum_mispred += n_mp

    # per-core 聚合（同一 core 不同 tid 合并）
    by_core = defaultdict(lambda: {'cycles_pred': 0.0, 'n_macro': 0,
                                   'n_micro': 0, 'mispred_pred_count': 0})
    for t in per_thread:
        c = by_core[t['core_id']]
        c['cycles_pred'] += t['cycles_pred']
        c['n_macro'] += t['n_macro']
        c['n_micro'] += t['n_micro']
        c['mispred_pred_count'] += t['mispred_pred_count']
    per_core = []
    for cid, c in sorted(by_core.items()):
        c['core_id'] = cid
        c['cpi_pred'] = (c['cycles_pred'] / c['n_macro']) if c['n_macro'] else float('nan')
        per_core.append(dict(c))

    # gem5 真值
    g_cyc, g_ins, g_cpi = parse_gem5_stats(args.gem5_stats)
    truth_cycles = sum(g_cyc.values())
    truth_insts = sum(g_ins.values())
    cpi_truth = (truth_cycles / truth_insts) if truth_insts else float('nan')
    cpi_pred_global = (sum_cycles / sum_macro) if sum_macro else float('nan')
    cpi_err_pct = ((cpi_pred_global - cpi_truth) / cpi_truth * 100.0) \
        if cpi_truth else float('nan')

    # per-core 误差对比
    per_core_diff = []
    inst_mismatch = []
    for c in per_core:
        cid = c['core_id']
        gc = g_cyc.get(cid, 0)
        gi = g_ins.get(cid, 0)
        gcpi = g_cpi.get(cid, float('nan'))
        cpi_err = ((c['cpi_pred'] - gcpi) / gcpi * 100.0) \
            if gcpi else float('nan')
        per_core_diff.append({
            'core_id': cid,
            'cycles_pred': c['cycles_pred'], 'cycles_truth': gc,
            'n_macro_pred': c['n_macro'], 'n_macro_truth': gi,
            'cpi_pred': c['cpi_pred'], 'cpi_truth': gcpi,
            'cpi_err_pct': cpi_err,
        })
        if c['n_macro'] != gi:
            inst_mismatch.append(
                f'core{cid}: pred={c["n_macro"]} truth={gi}'
            )

    global_inst_match = (sum_macro == truth_insts)
    if not global_inst_match:
        inst_mismatch.insert(
            0, f'global: pred={sum_macro} truth={truth_insts}'
        )

    report = {
        'global': {
            'cycles_pred_sum': sum_cycles,
            'cycles_truth_sum': truth_cycles,
            'n_macro_pred_sum': sum_macro,
            'n_macro_truth_sum': truth_insts,
            'cpi_pred_sumsum': cpi_pred_global,
            'cpi_truth_sumsum': cpi_truth,
            'cpi_err_pct': cpi_err_pct,
            'mispred_pred_total': sum_mispred,
        },
        'per_core': per_core_diff,
        'per_thread_diagnostic': per_thread,
    }
    with open(args.out_json, 'w') as f:
        json.dump(report, f, indent=2)

    # 打印简报
    print('=== CPI synthesis (sum/sum global) ===')
    print(f'cycles_pred = {sum_cycles:.0f}   cycles_truth = {truth_cycles}')
    print(f'n_macro_pred= {sum_macro}        n_macro_truth= {truth_insts}')
    print(f'CPI_pred (sum/sum) = {cpi_pred_global:.4f}')
    print(f'CPI_truth(sum/sum) = {cpi_truth:.4f}')
    print(f'CPI_err     = {cpi_err_pct:+.2f}%')
    print()
    print('--- per-core ---')
    print(f'{"core":>4} {"cyc_pred":>14} {"cyc_truth":>14} '
          f'{"cpi_pred":>10} {"cpi_truth":>10} {"err%":>8}')
    for d in per_core_diff:
        print(f'{d["core_id"]:>4} {d["cycles_pred"]:>14.0f} '
              f'{d["cycles_truth"]:>14} {d["cpi_pred"]:>10.4f} '
              f'{d["cpi_truth"]:>10.4f} {d["cpi_err_pct"]:>+8.2f}')
    print()
    print(f'mispred_pred_total = {sum_mispred}')
    print(f'\nreport -> {args.out_json}')

    if args.require_inst_match and inst_mismatch:
        sys.exit('[ERROR] instruction count mismatch: ' + '; '.join(inst_mismatch))


if __name__ == '__main__':
    main()
