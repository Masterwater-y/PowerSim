"""按 TAO 论文 §4 的 C-DUP 思路对 parquet 数据集做上下文指纹去重。

判定：两条样本互为重复 ⇔ 它们的「上下文窗口指纹 + 锚点标签桶」一致。

特征指纹 H[i]（per-row，uint64）覆盖以下桶化字段，**故意丢弃**：
  - macro_pc_id（identifier 而非行为）
  - vaddr / paddr / cacheline_addr 的精确值（保留 16 桶 hash）
  - core_id / thread_id / micro_seq / pos_in_thread（标识列）

被纳入 H[i] 的字段（取自 ml/dataset.py 的 SCALAR_BOOL/SMALL_INT + 桶化结果）：
  - 14 个 SCALAR_BOOL 打包成 14 bit
  - mesi/coh/path/sharer/owner/dirty/inval/same_line/oracle_source 一族小整数
  - i_path_class / i_coh_oracle / i_mesi_before / i_oracle_source（取指 oracle）
  - n_src/n_dst（clamp 0..15）, size（log2 桶）
  - d0..d3：bucketize_dist → 9 bins
  - pc0..pc3：sentinel 255 → 7，clamp 0..7
  - vaddr/paddr/cacheline_addr 的 hash_addr_bucket（16 bins）

锚点指纹 fp[t]（per-anchor，uint64）= 多项式滚动哈希(H[t-N+1..t])
  ⊕ latency 桶 ⊕ mispredicted 位 ⊕ is_fetch_group_head 位

逐 (core, thread) 段独立计算，O(L) 向量化（np.cumprod + np.cumsum，模 2^64 自动 wrap）。

可选 --protect-positives（默认开）：mispredicted=1 的锚点不参与去重，
保护稀有正样本（约 0.35%）。

输出：与输入相同结构的 parquet 数据集，meta.json 追加 dedup_* 字段。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq


# 与 ml/dataset.py 保持一致
SCALAR_BOOL = (
    'is_load', 'is_store', 'is_atomic', 'is_branch', 'is_branch_cond',
    'is_branch_indirect', 'is_call', 'is_return', 'is_int', 'is_fp',
    'is_simd', 'is_serialize', 'is_microop', 'is_last_microop',
)
# 这些字段会进入 fingerprint（注意：故意排除 macro_pc_id 等 identifier 列）
SMALL_INT_FOR_FP = (
    'n_src', 'n_dst', 'size',
    'mesi_before', 'coh_oracle', 'sharer_bucket', 'owner_dist',
    'dirty_owner', 'path_class', 'inval_fanout', 'same_line_recent',
    'oracle_source',
    'i_path_class', 'i_coh_oracle', 'i_mesi_before', 'i_oracle_source',
)


def hash_addr_bucket(arr: np.ndarray, n_bucket: int = 16) -> np.ndarray:
    """与 ml/dataset.py.hash_addr_bucket 等价，保留 cacheline 粒度。"""
    a = (arr.astype(np.uint64) >> np.uint64(6))
    a = a ^ (a >> np.uint64(30))
    a = (a * np.uint64(0xbf58476d1ce4e5b9)) & np.uint64(0xffffffffffffffff)
    a = a ^ (a >> np.uint64(27))
    a = (a * np.uint64(0x94d049bb133111eb)) & np.uint64(0xffffffffffffffff)
    a = a ^ (a >> np.uint64(31))
    return (a % np.uint64(n_bucket)).astype(np.uint64)


def bucketize_dist(arr: np.ndarray) -> np.ndarray:
    out = np.zeros_like(arr, dtype=np.int64)
    a = arr.astype(np.int64)
    out[(a >= 0) & (a <= 3)] = a[(a >= 0) & (a <= 3)]
    out[(a >= 4) & (a <= 7)] = 4
    out[(a >= 8) & (a <= 15)] = 5
    out[(a >= 16) & (a <= 31)] = 6
    out[(a >= 32) & (a <= 63)] = 7
    out[a >= 64] = 8
    out[a < 0] = 0
    return out.astype(np.uint64)


def splitmix64(x: np.ndarray) -> np.ndarray:
    """对 uint64 数组做 splitmix64 终混。"""
    x = (x ^ (x >> np.uint64(30))) * np.uint64(0xbf58476d1ce4e5b9)
    x = (x ^ (x >> np.uint64(27))) * np.uint64(0x94d049bb133111eb)
    x = x ^ (x >> np.uint64(31))
    return x


def per_row_hash(tbl: pa.Table) -> np.ndarray:
    """对每行 µop 计算 64-bit 行哈希，桶化、丢弃 identifier 列。"""
    n = tbl.num_rows
    h = np.zeros(n, dtype=np.uint64)
    # 14 个 bool 打包到低 14 bit
    bool_pack = np.zeros(n, dtype=np.uint64)
    for i, k in enumerate(SCALAR_BOOL):
        v = tbl[k].to_numpy().astype(np.uint64)
        bool_pack |= (v & np.uint64(1)) << np.uint64(i)
    h ^= bool_pack * np.uint64(0x100000001b3)

    # 小整数族
    for j, k in enumerate(SMALL_INT_FOR_FP):
        v = tbl[k].to_numpy().astype(np.int64)
        v = np.clip(v, 0, 31).astype(np.uint64)
        # 每个字段一个独立"槽位"：通过乘以独立质数 + 旋转保留位置区分度
        mult = np.uint64(0x9e3779b97f4a7c15) ^ np.uint64((j + 1) * 0x100000001b3)
        h ^= v * mult

    # producer dist 桶化
    for i in range(4):
        v = bucketize_dist(tbl[f'd{i}'].to_numpy())
        mult = np.uint64(0xc6a4a7935bd1e995) ^ np.uint64((i + 1) * 0x9e3779b1)
        h ^= v * mult

    # producer class（sentinel 255 -> 7）
    for i in range(4):
        v = tbl[f'pc{i}'].to_numpy().astype(np.int64)
        v = np.where(v == 255, 7, v)
        v = np.clip(v, 0, 7).astype(np.uint64)
        mult = np.uint64(0xff51afd7ed558ccd) ^ np.uint64((i + 1) * 0xc2b2ae35)
        h ^= v * mult

    # 16 桶地址哈希
    # V10 方案 B：cacheline_paddr 是 paddr-line 真值（与 cacheline_addr 的
    # vaddr-line 不同维度），纳入指纹后，"vaddr-line 同 / paddr-line 不同"
    # 的 microop（如别名 / 共享内存 / 进程间映射）不再被误判 dup。
    # COMPAT-OLD-50M: 旧 50M parquet 不含 cacheline_paddr 列，回退到
    # cacheline_addr，等同于 V10 之前的旧指纹（不增加去重粒度）。
    # 全 V10+ 重采后该 fallback 可删，并要求列必存在。
    if 'cacheline_paddr' in tbl.column_names:
        cline_paddr_arr = tbl['cacheline_paddr'].to_numpy()
    else:
        cline_paddr_arr = tbl['cacheline_addr'].to_numpy()
    addr_inputs = (
        ('vaddr', np.uint64(0xd6e8feb86659fd93), tbl['vaddr'].to_numpy()),
        ('paddr', np.uint64(0x94d049bb133111eb), tbl['paddr'].to_numpy()),
        ('cacheline_addr', np.uint64(0xbf58476d1ce4e5b9),
         tbl['cacheline_addr'].to_numpy()),
        ('cacheline_paddr', np.uint64(0xa5a5f00ddeadbeef),
         cline_paddr_arr),
    )
    for _name, mult, raw in addr_inputs:
        v = hash_addr_bucket(raw)
        h ^= v * mult

    return splitmix64(h)


def latency_bucket(arr: np.ndarray, n_bins: int = 8) -> np.ndarray:
    """log1p 后均匀分箱（capped to 4096 cycles）。"""
    a = np.maximum(arr.astype(np.int64), 0).astype(np.float64)
    a = np.log1p(a)
    cap = np.log1p(4096.0)
    a = np.minimum(a, cap)
    return np.clip((a / cap * n_bins).astype(np.int64), 0, n_bins - 1).astype(np.uint64)


def pow_mod_2_64(base: int, exp: int) -> int:
    return pow(base, exp, 1 << 64)


def window_fingerprint_per_segment(H: np.ndarray, N: int, mul: int) -> np.ndarray:
    """对一个 (core, thread) 段计算每行的窗口指纹。

    fp[t] = sum_{k=0..min(t,N-1)} H[t-k] * mul^k    (mod 2^64)

    向量化技巧：令 W[i] = H[i] * mul^i，C[i] = cumsum(W[0..i])，则
        fp[t] = (C[t] - C[t-N]) * inv(mul)^(t-N+1)   (mod 2^64)
    其中 t < N 时 C[t-N] = 0，且乘以 inv(mul)^(t-N+1) 仍然定义良好。
    """
    L = H.shape[0]
    if L == 0:
        return np.empty(0, dtype=np.uint64)
    mul_u = np.uint64(mul)
    inv_mul = pow_mod_2_64(mul, -1)  # mul 为奇数才存在
    inv_mul_u = np.uint64(inv_mul)

    # mul^i 累乘
    base_arr = np.full(L, mul_u, dtype=np.uint64)
    base_arr[0] = np.uint64(1)
    pow_mul = np.multiply.accumulate(base_arr)  # uint64 wraps mod 2^64

    # inv(mul)^i
    inv_arr = np.full(L + 1, inv_mul_u, dtype=np.uint64)
    inv_arr[0] = np.uint64(1)
    pow_inv = np.multiply.accumulate(inv_arr)  # 长度 L+1，对应 i=0..L

    # W[i] = H[i] * mul^i
    W = H * pow_mul  # uint64 wraps
    # cumsum 在 uint64 下 wrap mod 2^64
    C = np.cumsum(W, dtype=np.uint64)

    # 构造 C_offset[t] = C[t-N]，t<N 时为 0
    C_offset = np.zeros(L, dtype=np.uint64)
    if L > N:
        C_offset[N:] = C[:L - N]

    diff = C - C_offset  # uint64 wraps
    # 乘以 inv(mul)^(t-N+1)
    # 注意：t < N-1 时 t-N+1 为负，意味着窗口长度 < N，左侧不足。我们仍想要
    # "位置无关"指纹：把 sum_{k=0..t} H[t-k]*mul^k 反卷积成 sum_{j=0..t} H[j]*mul^(t-j)
    # 要除以 mul^t 还原；为统一起见，所有 t 都除以 mul^max(0, t-N+1)，这样
    # 长度 < N 的前缀窗口与长度 = N 的窗口互不相同（因为 H 序列前缀里没有 left-pad 0）。
    # 这一行为与 ml/dataset.py 的 left-pad mask 吻合（前缀窗口的 fp 自然不同于完整窗口）。
    shift = np.maximum(np.arange(L) - (N - 1), 0).astype(np.int64)
    inv_factor = pow_inv[shift]  # 长度 L
    fp = diff * inv_factor  # uint64 wraps

    return splitmix64(fp)


def dedup_partition(in_path: Path, out_path: Path, ctx_len: int,
                    protect_positives: bool, log) -> tuple[int, int, int]:
    """返回 (n_in, n_out, n_protected)。"""
    tbl = pq.read_table(in_path)
    n = tbl.num_rows
    log(f'    rows = {n:,}')

    cid = tbl['core_id'].to_numpy()
    tid = tbl['thread_id'].to_numpy()
    pos = tbl['pos_in_thread'].to_numpy()
    # 输入 parquet 已按 (core, thread, micro_seq) 升序，pos_in_thread 是
    # thread 内 0-based 行号；此处直接信任顺序，不再排序。
    # 段边界
    new_seg = np.empty(n, dtype=bool)
    new_seg[0] = True
    new_seg[1:] = (cid[1:] != cid[:-1]) | (tid[1:] != tid[:-1])
    starts = np.where(new_seg)[0]
    ends = np.concatenate([starts[1:], np.array([n], dtype=np.int64)])

    # 每行特征哈希
    H = per_row_hash(tbl)

    # 逐段窗口指纹
    fp = np.empty(n, dtype=np.uint64)
    MUL = 0x100000001b3  # 64-bit FNV prime（odd → 在 2^64 下可逆）
    for s, e in zip(starts, ends):
        fp[s:e] = window_fingerprint_per_segment(H[s:e], ctx_len, MUL)

    # 折入锚点 label 桶（避免"行为同但延迟/前端边界显著不同"被合并）
    f_lat = latency_bucket(tbl['fetch_latency'].to_numpy(), n_bins=8)
    e_lat = latency_bucket(tbl['execution_latency'].to_numpy(), n_bins=8)
    mis = tbl['mispredicted'].to_numpy().astype(np.uint64) & np.uint64(1)
    if 'is_fetch_group_head' in tbl.column_names:
        head = tbl['is_fetch_group_head'].to_numpy().astype(np.uint64) & np.uint64(1)
    else:
        head = np.zeros(n, dtype=np.uint64)

    label_hash = (
        f_lat * np.uint64(0xa5a5a5a5a5a5a5a5)
        ^ e_lat * np.uint64(0x5a5a5a5a5a5a5a5a)
        ^ mis * np.uint64(0xff51afd7ed558ccd)
        ^ head * np.uint64(0xc4ceb9fe1a85ec53)
    )
    fp = fp ^ splitmix64(label_hash)

    # 去重：稳定保留首次出现
    if protect_positives:
        # mispred=1 的锚点全部保留，mispred=0 的做去重
        neg_mask = (mis == 0)
        keep = np.zeros(n, dtype=bool)
        keep[~neg_mask] = True   # 全部正样本保留
        # 仅在负样本里去重
        neg_idx = np.where(neg_mask)[0]
        if neg_idx.size > 0:
            _, first_in_neg = np.unique(fp[neg_idx], return_index=True)
            keep[neg_idx[first_in_neg]] = True
        n_protected = int((mis == 1).sum())
    else:
        _, first = np.unique(fp, return_index=True)
        keep = np.zeros(n, dtype=bool)
        keep[first] = True
        n_protected = 0

    sel = np.where(keep)[0]
    sel.sort()  # 保留原顺序（unique 不保证）
    new_tbl = tbl.take(pa.array(sel))
    pq.write_table(
        new_tbl, out_path,
        compression='zstd', compression_level=3,
        row_group_size=65536, use_dictionary=True,
        data_page_size=1 << 20,
    )
    return n, new_tbl.num_rows, n_protected


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--in-dir', required=True,
                    help='输入 parquet 数据集目录（hive 分区 workload=*/part-*.parquet）')
    ap.add_argument('--out-dir', required=True)
    ap.add_argument('--context-len', type=int, default=128)
    ap.add_argument('--no-protect-positives', action='store_true',
                    help='对 mispredicted=1 的锚点也参与去重（默认保护这些稀有正样本）')
    args = ap.parse_args()

    in_dir = Path(args.in_dir)
    out_dir = Path(args.out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    parts = sorted(in_dir.glob('workload=*/part-*.parquet'))
    if not parts:
        print(f'[err] no parquet found under {in_dir}', file=sys.stderr)
        sys.exit(2)

    def log(msg):
        print(msg, flush=True)

    t0 = time.time()
    summary = {}
    total_in = total_out = total_prot = 0
    for src in parts:
        w = src.parent.name.split('=', 1)[1]
        dst_dir = out_dir / f'workload={w}'
        dst_dir.mkdir()
        dst = dst_dir / 'part-000.parquet'
        log(f'[dedup] {w}')
        n_in, n_out, n_prot = dedup_partition(
            src, dst, args.context_len,
            protect_positives=not args.no_protect_positives, log=log)
        ratio = n_out / max(n_in, 1)
        log(f'    kept = {n_out:,} / {n_in:,}  ({ratio:.3%}; '
            f'protected positives = {n_prot:,})')
        summary[w] = {'in': n_in, 'out': n_out, 'protected_pos': n_prot}
        total_in += n_in
        total_out += n_out
        total_prot += n_prot

    # 复制 vocab.json + 更新 meta.json
    if (in_dir / 'vocab.json').exists():
        shutil.copy(in_dir / 'vocab.json', out_dir / 'vocab.json')
    meta_src = in_dir / 'meta.json'
    if meta_src.exists():
        m = json.loads(meta_src.read_text())
    else:
        m = {}
    m['source_dataset'] = str(in_dir)
    m['n_total'] = total_out
    m['workload_rows'] = {w: s['out'] for w, s in summary.items()}
    m['dedup_method'] = 'c_dup_window_polyhash'
    m['dedup_context_len'] = args.context_len
    m['dedup_protect_positives'] = not args.no_protect_positives
    m['dedup_keep_ratio'] = total_out / max(total_in, 1)
    m['dedup_summary'] = summary
    (out_dir / 'meta.json').write_text(json.dumps(m, indent=2))

    elapsed = time.time() - t0
    log(f'\n[done] {total_in:,} -> {total_out:,}  '
        f'({total_out / max(total_in,1):.3%}) in {elapsed:.1f}s')
    log(f'       protected positives = {total_prot:,}')
    log(f'       output @ {out_dir}')


if __name__ == '__main__':
    main()
