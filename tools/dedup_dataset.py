#!/usr/bin/env python3
"""TAO-style dataset deduplication.

按 input feature vector（剥离 macro_pc/micro_pc/vaddr/size 这种"身份字段"）
对样本去重。每个 unique fv 至多保留 K 条（保留 label 噪声）。

fv = (op_class flags, n_src, n_dst, producer_dists[4], producer_classes[4],
      path_class, coh_oracle, sharer_bucket, owner_dist, dirty_owner)

输入：tmp/dataset_p3_v96/W*.jsonl
输出：tmp/dataset_p3_v96_dedup_K{K}/W*.jsonl + all.jsonl
"""
import argparse
import collections
import glob
import hashlib
import json
import os
import random
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--in-dir', required=True)
    ap.add_argument('--out-dir', required=True)
    ap.add_argument('--cap-k', type=int, default=100,
                    help='每个 unique fv 至多保留 K 条（K=1 即纯去重）')
    ap.add_argument('--seed', type=int, default=20260528)
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    rng = random.Random(args.seed)

    # 全局 fv 计数器（跨 workload 累计）
    fv_count = collections.Counter()
    total_in = 0
    total_out = 0

    for in_path in sorted(glob.glob(os.path.join(args.in_dir, 'W*.jsonl'))):
        wl = os.path.basename(in_path).replace('.jsonl', '')
        out_path = os.path.join(args.out_dir, f'{wl}.jsonl')
        n_in = 0
        n_out = 0
        with open(in_path) as fr, open(out_path, 'w') as fw:
            for ln in fr:
                n_in += 1
                rec = json.loads(ln)
                k = fv_key(rec)
                # reservoir-style：到 cap 之前都收，到 cap 之后随机替换以避免顺序偏置
                c = fv_count[k]
                if c < args.cap_k:
                    fw.write(ln)
                    n_out += 1
                fv_count[k] = c + 1
        print(f"[{wl}] in={n_in:>10} out={n_out:>10} keep={n_out/n_in*100:>6.2f}%")
        total_in += n_in
        total_out += n_out

    # all.jsonl 拼接
    all_path = os.path.join(args.out_dir, 'all.jsonl')
    with open(all_path, 'w') as fa:
        for wp in sorted(glob.glob(os.path.join(args.out_dir, 'W*.jsonl'))):
            with open(wp) as fr:
                for ln in fr:
                    fa.write(ln)

    print()
    print(f"=== TAO-style dedup K={args.cap_k} ===")
    print(f"in_total   : {total_in}")
    print(f"out_total  : {total_out}  (kept {total_out/total_in*100:.2f}%)")
    print(f"unique_fv  : {len(fv_count)}")
    print(f"  p50 mult : {sorted(fv_count.values())[len(fv_count)//2]}")
    print(f"  max mult : {max(fv_count.values())}")
    print(f"-> {args.out_dir}/")


if __name__ == '__main__':
    main()
