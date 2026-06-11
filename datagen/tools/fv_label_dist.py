#!/usr/bin/env python3
"""统计 fv 上的 label 分布：每个 fv 桶里 fL/eL/mispred 的方差和分位数。

判断"同 fv 不同 label"是否值得保留多份。
"""
import collections
import glob
import json
import os
import statistics
import sys

KEYS = (
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
    parts = [inp.get(k, 0) for k in KEYS]
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


def op_class_name(inp):
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


def quantiles(vs):
    if not vs:
        return None
    vs = sorted(vs)
    n = len(vs)
    return {
        'min': vs[0],
        'p25': vs[n * 25 // 100],
        'p50': vs[n // 2],
        'p75': vs[n * 75 // 100],
        'p90': vs[n * 90 // 100],
        'p99': vs[min(n - 1, n * 99 // 100)],
        'max': vs[-1],
        'mean': sum(vs) / n,
    }


def analyze(in_dir, top_k=10):
    # 全局: fv -> list of (fL, eL, mispred, op_class)
    fv_data = collections.defaultdict(list)
    fv_op = {}
    for in_path in sorted(glob.glob(os.path.join(in_dir, 'W*.jsonl'))):
        wl = os.path.basename(in_path).replace('.jsonl', '')
        n = 0
        with open(in_path) as fr:
            for ln in fr:
                rec = json.loads(ln)
                k = fv_key(rec)
                lab = rec.get('labels', {})
                fL = lab.get('fetch_latency', 0)
                eL = lab.get('execution_latency', 0)
                mp = lab.get('mispredicted', 0)
                fv_data[k].append((fL, eL, mp, wl))
                if k not in fv_op:
                    fv_op[k] = op_class_name(rec['input'])
                n += 1
        print(f'[scan] {wl}: {n} rows')

    print(f'\nTotal unique fv: {len(fv_data)}')
    by_mult = sorted(fv_data.items(), key=lambda kv: -len(kv[1]))

    # ----- TOP K 高频 fv 的 label 分布 -----
    print(f'\n=== Top {top_k} fv by multiplicity ===')
    print(f'{"#":>3}  {"mult":>8}  {"op":>9}  '
          f'{"fL_p50":>6} {"fL_p99":>7}  '
          f'{"eL_p50":>7} {"eL_p99":>7}  '
          f'{"mp%":>5}  workloads')
    for i, (k, rows) in enumerate(by_mult[:top_k]):
        fLs = [r[0] for r in rows]
        eLs = [r[1] for r in rows]
        mps = [r[2] for r in rows]
        wls = collections.Counter(r[3] for r in rows)
        fLq = quantiles(fLs)
        eLq = quantiles(eLs)
        wstr = ','.join(f'{w}:{c}' for w, c in wls.most_common(3))
        print(f'{i:>3}  {len(rows):>8}  {fv_op[k]:>9}  '
              f'{fLq["p50"]:>6} {fLq["p99"]:>7}  '
              f'{eLq["p50"]:>7} {eLq["p99"]:>7}  '
              f'{sum(mps)/len(mps)*100:>5.1f}  {wstr}')

    # ----- 整体: 同一 fv 内 label 的方差 -----
    print('\n=== Label spread within same fv (mult>=10) ===')
    el_cv_list = []
    fl_cv_list = []
    mp_var_list = []
    el_distinct_list = []
    fl_distinct_list = []
    for k, rows in fv_data.items():
        if len(rows) < 10:
            continue
        eLs = [r[1] for r in rows]
        fLs = [r[0] for r in rows]
        mps = [r[2] for r in rows]
        if statistics.mean(eLs) > 0:
            el_cv_list.append(statistics.pstdev(eLs) / statistics.mean(eLs))
        if statistics.mean(fLs) > 0:
            fl_cv_list.append(statistics.pstdev(fLs) / statistics.mean(fLs))
        mp_var_list.append(sum(mps) / len(mps))
        el_distinct_list.append(len(set(eLs)))
        fl_distinct_list.append(len(set(fLs)))

    def show(name, vs):
        if not vs:
            print(f'  {name}: <empty>')
            return
        q = quantiles(vs)
        print(f'  {name}: mean={q["mean"]:.3f} p50={q["p50"]:.3f} '
              f'p90={q["p90"]:.3f} p99={q["p99"]:.3f} max={q["max"]:.3f}')

    print(f'  fv buckets with mult>=10: {len(el_cv_list)}')
    show('eL CV (std/mean)', el_cv_list)
    show('fL CV (std/mean)', fl_cv_list)
    show('mispred rate', mp_var_list)
    show('eL distinct values per fv', el_distinct_list)
    show('fL distinct values per fv', fl_distinct_list)

    # ----- 如果只保留每 fv 1 条，丢失多少 label 信息 -----
    print('\n=== Coverage loss from K=1 ===')
    el_unique_global = set()
    fl_unique_global = set()
    mp_split_fv = 0
    for k, rows in fv_data.items():
        eLs = set(r[1] for r in rows)
        fLs = set(r[0] for r in rows)
        mps = set(r[2] for r in rows)
        el_unique_global.update((k, e) for e in eLs)
        fl_unique_global.update((k, f) for f in fLs)
        if len(mps) > 1:
            mp_split_fv += 1
    n_fv = len(fv_data)
    print(f'  unique (fv, eL) pairs: {len(el_unique_global)}  (vs {n_fv} fv → {len(el_unique_global)/n_fv:.2f}x)')
    print(f'  unique (fv, fL) pairs: {len(fl_unique_global)}  (vs {n_fv} fv → {len(fl_unique_global)/n_fv:.2f}x)')
    print(f'  fv with both mp=0 and mp=1: {mp_split_fv} ({mp_split_fv/n_fv*100:.2f}%)')


if __name__ == '__main__':
    analyze(sys.argv[1] if len(sys.argv) > 1 else 'tmp/dataset_p3_v96',
            top_k=int(sys.argv[2]) if len(sys.argv) > 2 else 12)
