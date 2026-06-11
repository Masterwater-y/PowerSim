#!/usr/bin/env python3
"""V9.7 数据多样性体检：单 workload + 跨 workload + 去重前后对比。

回答四个问题：
1) 各特征族（op_class, oracle, producer-dep, vaddr_bucket, fL/eL, head）分布是否平衡？
2) 不同 K 去重档下 fv 多样性回收率（unique_fv / total）是多少？
3) 重复主要由哪些 op_class / oracle bucket 贡献？
4) 哪些 cell（op_class × path_class）几乎为空 → 需要构造负载补足？
"""
import argparse
import collections
import glob
import json
import math
import os
import sys


# 与 dedup_dataset.py 完全一致的 fv 定义
FV_FLAGS = (
    'is_load', 'is_store', 'is_atomic',
    'is_branch', 'is_branch_cond', 'is_branch_indirect',
    'is_call', 'is_return',
    'is_int', 'is_fp', 'is_simd', 'is_serialize',
    'is_microop', 'is_last_microop',
    'n_src', 'n_dst',
)


def fv_key(rec):
    inp = rec['input']
    ux = rec.get('uarch_context', {})
    parts = [inp.get(k, 0) for k in FV_FLAGS]
    parts += list(inp.get('producer_dists', [0] * 4))
    parts += list(inp.get('producer_classes', [0] * 4))
    parts += [
        ux.get('path_class', 0),
        ux.get('coh_oracle', 0),
        ux.get('sharer_bucket', 0),
        ux.get('owner_dist', 0),
        ux.get('dirty_owner', 0),
    ]
    return tuple(parts)


def op_class(inp):
    if inp.get('is_atomic'): return 'atomic'
    if inp.get('is_branch'):
        if inp.get('is_branch_indirect'): return 'br_indir'
        if inp.get('is_branch_cond'): return 'br_cond'
        return 'br_uncond'
    if inp.get('is_call'): return 'call'
    if inp.get('is_return'): return 'ret'
    if inp.get('is_load'): return 'load'
    if inp.get('is_store'): return 'store'
    if inp.get('is_simd'): return 'simd'
    if inp.get('is_fp'): return 'fp'
    if inp.get('is_int'): return 'int'
    if inp.get('is_serialize'): return 'serial'
    return 'other'


def shannon_entropy(counter):
    total = sum(counter.values())
    if total == 0:
        return 0.0
    H = 0.0
    for v in counter.values():
        if v > 0:
            p = v / total
            H -= p * math.log2(p)
    return H


def gini(counter):
    """衡量一个分布的不平衡程度。0=完全均匀, 接近 1=极端集中。"""
    vs = sorted(counter.values())
    n = len(vs)
    if n == 0:
        return 0.0
    total = sum(vs)
    if total == 0:
        return 0.0
    cum = 0.0
    for i, v in enumerate(vs, 1):
        cum += i * v
    return (2 * cum) / (n * total) - (n + 1) / n


def quantiles(vs):
    if not vs:
        return None
    vs = sorted(vs)
    n = len(vs)
    return {
        'min': vs[0],
        'p50': vs[n // 2],
        'p90': vs[min(n - 1, n * 90 // 100)],
        'p99': vs[min(n - 1, n * 99 // 100)],
        'max': vs[-1],
        'mean': round(sum(vs) / n, 2),
    }


def analyze_one(path):
    op_cnt = collections.Counter()
    path_cnt = collections.Counter()
    coh_cnt = collections.Counter()
    fv_mult = collections.Counter()
    fL_zero = 0
    eL_zero = 0
    mp_one = 0
    head_one = 0
    n_src_dist = collections.Counter()
    pd_bucket = collections.Counter()
    op_path_cell = collections.Counter()
    total = 0
    fL_vals, eL_vals = [], []

    with open(path) as fr:
        for line in fr:
            rec = json.loads(line)
            inp = rec['input']
            ux = rec.get('uarch_context', {})
            lbl = rec.get('labels', {})
            oc = op_class(inp)
            pc = ux.get('path_class', 0)
            ch = ux.get('coh_oracle', 0)
            op_cnt[oc] += 1
            path_cnt[pc] += 1
            coh_cnt[ch] += 1
            op_path_cell[(oc, pc)] += 1
            n_src_dist[inp.get('n_src', 0)] += 1
            for d in inp.get('producer_dists', []):
                pd_bucket[int(d)] += 1
            fL = lbl.get('fetch_latency', 0)
            eL = lbl.get('execution_latency', 0)
            mp = lbl.get('mispredicted', 0)
            hd = lbl.get('is_fetch_group_head', 0)
            if fL == 0:
                fL_zero += 1
            if eL == 0:
                eL_zero += 1
            if mp:
                mp_one += 1
            if hd:
                head_one += 1
            fL_vals.append(fL)
            eL_vals.append(eL)
            fv_mult[fv_key(rec)] += 1
            total += 1

    n_unique_fv = len(fv_mult)
    mult_vals = list(fv_mult.values())
    return {
        'total': total,
        'unique_fv': n_unique_fv,
        'fv_recovery_rate': round(n_unique_fv / total, 4) if total else 0,
        'fv_mult_p50_p90_p99_max': (
            quantiles(mult_vals) if mult_vals else None
        ),
        'top10_fv_share': round(
            sum(sorted(mult_vals, reverse=True)[:10]) / total, 3
        ) if total else 0,
        'op_class': dict(op_cnt.most_common()),
        'path_class': dict(path_cnt.most_common()),
        'coh_oracle': dict(coh_cnt.most_common()),
        'op_class_entropy_bits': round(shannon_entropy(op_cnt), 3),
        'op_class_gini': round(gini(op_cnt), 3),
        'path_class_entropy_bits': round(shannon_entropy(path_cnt), 3),
        'fL=0_pct': round(fL_zero / total * 100, 2) if total else 0,
        'eL=0_pct': round(eL_zero / total * 100, 2) if total else 0,
        'mispred_pct': round(mp_one / total * 100, 2) if total else 0,
        'head_pct': round(head_one / total * 100, 2) if total else 0,
        'fL_quantiles': quantiles(fL_vals),
        'eL_quantiles': quantiles(eL_vals),
        'producer_dist_buckets': dict(sorted(pd_bucket.items())[:12]),
        'op_path_top10': [
            {'op': k[0], 'path': k[1], 'n': v}
            for k, v in sorted(op_path_cell.items(), key=lambda kv: -kv[1])[:10]
        ],
        'op_path_empty_cells': [
            {'op': oc, 'path': pc} for oc in op_cnt
            for pc in path_cnt
            if op_path_cell.get((oc, pc), 0) == 0
        ][:20],
    }


def cross_workload(in_dir):
    results = {}
    g = collections.Counter()
    g_path = collections.Counter()
    g_coh = collections.Counter()
    g_fv = collections.Counter()
    g_total = 0
    for path in sorted(glob.glob(os.path.join(in_dir, 'W*.jsonl'))):
        if path.endswith('all.jsonl'):
            continue
        wl = os.path.basename(path).replace('.jsonl', '')
        sys.stderr.write(f'[{wl}] analyzing... ')
        sys.stderr.flush()
        r = analyze_one(path)
        results[wl] = r
        sys.stderr.write(
            f'total={r["total"]}, unique_fv={r["unique_fv"]} '
            f'({r["fv_recovery_rate"]*100:.1f}%)\n'
        )
        for k, v in r['op_class'].items():
            g[k] += v
        for k, v in r['path_class'].items():
            g_path[k] += v
        for k, v in r['coh_oracle'].items():
            g_coh[k] += v
        g_total += r['total']
    results['_global'] = {
        'total': g_total,
        'op_class_share': {
            k: round(v / g_total, 4) for k, v in g.most_common()
        } if g_total else {},
        'path_class_share': {
            k: round(v / g_total, 4) for k, v in g_path.most_common()
        } if g_total else {},
        'coh_oracle_share': {
            k: round(v / g_total, 4) for k, v in g_coh.most_common()
        } if g_total else {},
        'op_class_entropy_bits': round(shannon_entropy(g), 3),
        'op_class_gini': round(gini(g), 3),
    }
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--in-dir', required=True)
    ap.add_argument('--out', required=True)
    args = ap.parse_args()
    res = cross_workload(args.in_dir)
    with open(args.out, 'w') as fw:
        json.dump(res, fw, indent=2, ensure_ascii=False, default=str)
    sys.stderr.write(f'\nWrote {args.out}\n')


if __name__ == '__main__':
    main()
