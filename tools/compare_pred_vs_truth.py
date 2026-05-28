#!/usr/bin/env python3
"""compare_pred_vs_truth.py — 把 ml/infer.py 输出与 gem5 探针真值对照。

输入：
  --pred-jsonl   : ml/infer.py 输出（fetch_lat / exec_lat / mispred_prob / mispred_hard）
  --records-glob : detailed-dir/*.records.micro.jsonl 通配（用于对齐 thread_id）
  --labels-glob  : detailed-dir/*.labels.micro.jsonl  通配（提供真值 ticks）
                   labels 含 fetch_tick / ready_tick / commit_tick / mispredicted。

口径（对齐 build_micro_dataset.py V9.5）：
  fetch_lat_truth = fetch_tick_i - fetch_tick_{i-1}   (首条=0)
  exec_lat_truth  = ready_tick_i - fetch_tick_i

按 (core_id, thread_id, micro_seq) inner-join；
仅在 pred 行集合内做对比（推理只跑了前 N 条）。

输出：
  - 全局 MAE / RMSE / median(|err|) for fetch_lat & exec_lat
  - mispred 准确率 / 召回 / 阳性数（|truth>0| / |pred==1|）
  - 段内 sum/sum CPI 同 gem5 真值对比
"""
import argparse
import glob
import json
import math
import re
import os
from collections import defaultdict


def core_id_of(name):
    m = re.search(r'cores(\d+)', name)
    return int(m.group(1)) if m else -1


def load_pred(path):
    out = {}
    with open(path) as f:
        for ln in f:
            s = ln.strip()
            if not s.startswith('{'):
                continue
            r = json.loads(s)
            key = (int(r['core_id']), int(r['thread_id']),
                   int(r['micro_seq']))
            out[key] = r
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pred-jsonl', required=True)
    ap.add_argument('--detailed-dir', required=True,
                    help='gem5 outdir/tao_trace 子目录')
    args = ap.parse_args()

    pred = load_pred(args.pred_jsonl)
    print(f'[cmp] pred rows = {len(pred)}')

    records_files = sorted(
        glob.glob(os.path.join(args.detailed_dir, '*.records.micro.jsonl')),
        key=lambda p: core_id_of(os.path.basename(p)))
    labels_files = sorted(
        glob.glob(os.path.join(args.detailed_dir, '*.labels.micro.jsonl')),
        key=lambda p: core_id_of(os.path.basename(p)))

    # 累加真值 fetch_lat / exec_lat / mispred 与 pred 对比
    n_pred_used = 0
    abs_fl = []
    abs_el = []
    truth_mp = 0
    pred_mp_pos = 0
    tp = fp = fn = tn = 0

    # 段内 per-thread sum cycles & n_macro
    pred_cycles = defaultdict(float)
    pred_macro = defaultdict(int)
    truth_cycles = defaultdict(float)
    truth_macro = defaultdict(int)

    for rf, lf in zip(records_files, labels_files):
        cid = core_id_of(os.path.basename(rf))
        prev_fetch = {}
        # ready-clock 累加（与 synthesize_cpi.py 一致）
        fc_pred = defaultdict(float)
        rc_pred = defaultdict(float)
        fc_tr = defaultdict(float)
        rc_tr = defaultdict(float)

        with open(rf) as fr, open(lf) as fl_:
            for lr_, ll_ in zip(fr, fl_):
                jr = json.loads(lr_)
                jl = json.loads(ll_)
                tid = int(jr['thread_id'])
                mseq = int(jr['micro_seq'])
                key = (cid, tid, mseq)
                if key not in pred:
                    continue  # pred 子集外，跳过

                ft = int(jl['fetch_tick'])
                rt = int(jl['ready_tick'])
                if (cid, tid) not in prev_fetch:
                    fl_truth = 0
                else:
                    fl_truth = ft - prev_fetch[(cid, tid)]
                prev_fetch[(cid, tid)] = ft
                el_truth = rt - ft
                mp_truth = int(jl.get('mispredicted', 0))

                p = pred[key]
                fl_p = float(p['fetch_lat'])
                el_p = float(p['exec_lat'])
                mp_p = int(p['mispred_hard'])

                abs_fl.append(abs(fl_p - fl_truth))
                abs_el.append(abs(el_p - el_truth))
                truth_mp += mp_truth
                pred_mp_pos += mp_p
                if mp_truth and mp_p:
                    tp += 1
                elif mp_truth and not mp_p:
                    fn += 1
                elif (not mp_truth) and mp_p:
                    fp += 1
                else:
                    tn += 1

                # ready-clock 累加（pred & truth 同口径）
                kt = (cid, tid)
                fc_pred[kt] += fl_p
                rc_pred[kt] = max(rc_pred[kt], fc_pred[kt] + el_p)
                fc_tr[kt] += fl_truth
                rc_tr[kt] = max(rc_tr[kt], fc_tr[kt] + el_truth)
                if int(jr.get('is_last_microop', 0)):
                    pred_macro[kt] += 1
                    truth_macro[kt] += 1

                n_pred_used += 1

        for kt in rc_pred:
            pred_cycles[kt] = rc_pred[kt]
            truth_cycles[kt] = rc_tr[kt]

    if n_pred_used == 0:
        print('[cmp] no overlap rows; abort')
        return
    abs_fl.sort(); abs_el.sort()
    def stats(a):
        n = len(a)
        mae = sum(a) / n
        rmse = math.sqrt(sum(x * x for x in a) / n)
        med = a[n // 2]
        p90 = a[int(n * 0.9)]
        return mae, rmse, med, p90
    mae_fl, rmse_fl, med_fl, p90_fl = stats(abs_fl)
    mae_el, rmse_el, med_el, p90_el = stats(abs_el)

    print()
    print('=' * 70)
    print(f'[cmp] aligned rows: {n_pred_used}')
    print('=' * 70)
    print('  fetch_lat (cycles):')
    print(f'    MAE   = {mae_fl:.3f}')
    print(f'    RMSE  = {rmse_fl:.3f}')
    print(f'    median|err| = {med_fl:.3f}')
    print(f'    p90|err|    = {p90_fl:.3f}')
    print('  exec_lat (cycles):')
    print(f'    MAE   = {mae_el:.3f}')
    print(f'    RMSE  = {rmse_el:.3f}')
    print(f'    median|err| = {med_el:.3f}')
    print(f'    p90|err|    = {p90_el:.3f}')
    print()
    print(f'  mispred truth-positives = {truth_mp}')
    print(f'  mispred pred-positives  = {pred_mp_pos}')
    print(f'  TP={tp} FP={fp} FN={fn} TN={tn}')
    if tp + fn > 0:
        recall = tp / (tp + fn)
        print(f'  recall (TP/(TP+FN)) = {recall:.4f}')
    if tp + fp > 0:
        precision = tp / (tp + fp)
        print(f'  precision (TP/(TP+FP)) = {precision:.4f}')
    print()
    print('--- segment-level CPI (sum/sum, aligned subset) ---')
    sc_p = sum(pred_cycles.values())
    sm_p = sum(pred_macro.values())
    sc_t = sum(truth_cycles.values())
    sm_t = sum(truth_macro.values())
    cpi_p = sc_p / sm_p if sm_p else float('nan')
    cpi_t = sc_t / sm_t if sm_t else float('nan')
    err = (cpi_p - cpi_t) / cpi_t * 100.0 if cpi_t else float('nan')
    print(f'  cycles_pred={sc_p:.0f} n_macro_pred={sm_p}')
    print(f'  cycles_truth={sc_t:.0f} n_macro_truth={sm_t}')
    print(f'  CPI_pred={cpi_p:.4f}  CPI_truth={cpi_t:.4f}  err={err:+.2f}%')
    print()
    print(f'  {"core":>4} {"tid":>3} {"cyc_pred":>12} {"cyc_truth":>12} '
          f'{"cpi_pred":>10} {"cpi_truth":>10} {"err%":>8}')
    for kt in sorted(pred_cycles.keys()):
        cp = pred_cycles[kt]; ct = truth_cycles[kt]
        np_ = pred_macro[kt]; nt_ = truth_macro[kt]
        cp_cpi = cp / np_ if np_ else float('nan')
        ct_cpi = ct / nt_ if nt_ else float('nan')
        e = (cp_cpi - ct_cpi) / ct_cpi * 100.0 if ct_cpi else float('nan')
        print(f'  {kt[0]:>4} {kt[1]:>3} {cp:>12.0f} {ct:>12.0f} '
              f'{cp_cpi:>10.4f} {ct_cpi:>10.4f} {e:>+8.2f}')


if __name__ == '__main__':
    main()
