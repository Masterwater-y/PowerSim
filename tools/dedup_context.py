"""按 TAO 论文 §4 的 C-DUP 思路对 parquet 数据集做上下文指纹去重。

判定：两条样本互为重复 ⇔ 它们的「上下文窗口指纹 + 锚点标签桶」一致。

V10.3 model-aware 口径：
特征指纹 H[i]（per-row，uint64）覆盖当前模型实际可见的非 identifier 特征，
**故意丢弃**：
  - macro_pc_id（identifier 而非行为）
  - macro_pc / micro_pc 精确值（macro_pc 仅派生 i_group_* 的低容量桶）
  - vaddr / paddr / cacheline_addr / cacheline_paddr 的精确值（保留 16 桶 hash）
  - core_id / thread_id / micro_seq / pos_in_thread（标识列）
  - workload

被纳入 H[i] 的字段：
  - 14 个 SCALAR_BOOL 打包成 14 bit
  - n_src/n_dst（clamp 0..15）, size（log2 桶）
  - D/I-side coherence、P0-A、V10.3 A 小整数（排除 i_oracle_source）
  - P1-C 上下文窗口字段
  - V10.3 B/C 长窗口 unique_cl + DRAM bank/row 字段
  - d0..d3：bucketize_dist → 9 bins
  - pc0..pc3：sentinel 255 → 7，clamp 0..7
  - vaddr/paddr/cacheline_addr/cacheline_paddr 的 hash_addr_bucket（16 bins）
  - i_group_head / i_group_pos / i_group_bkt（从 macro_pc 派生，和 ml.dataset.py 对齐）

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


# 与 ml/dataset.py 保持一致；只复制轻量常量，避免工具脚本依赖导入路径。
SCALAR_BOOL = (
    'is_load', 'is_store', 'is_atomic', 'is_branch', 'is_branch_cond',
    'is_branch_indirect', 'is_call', 'is_return', 'is_int', 'is_fp',
    'is_simd', 'is_serialize', 'is_microop', 'is_last_microop',
)
# 这些字段会进入 fingerprint（注意：故意排除 macro_pc_id / i_oracle_source 等）
SMALL_INT_FOR_FP = (
    'n_src', 'n_dst', 'size',
    'mesi_before', 'coh_oracle', 'sharer_bucket', 'owner_dist',
    'dirty_owner', 'path_class', 'inval_fanout', 'same_line_recent',
    'oracle_source',
    # P0-A / V10.3 A d-side
    'd_mshr_depth', 'dtlb_hit', 'd_walker_levels',
    'd_walker_dram_misses', 'd_bank_id',
    'd_llc_set_residency', 'd_llc_set_lru_pos',
    # i-side：模型不消费 i_oracle_source，故不纳入去重指纹
    'i_path_class', 'i_coh_oracle', 'i_mesi_before',
    'i_mshr_depth', 'itlb_hit', 'i_walker_levels',
    'i_walker_dram_misses', 'i_bank_id',
    'i_llc_set_residency', 'i_llc_set_lru_pos',
)
SCALAR_P1C_FOR_FP = (
    'mem_density_W64', 'branch_density_W64', 'unique_cl_W64',
    'cl_reuse_dist_log', 'pc_freq_W64', 'time_since_last_branch_log',
    'bank_conflict_W64',
)
SCALAR_V10_3_FOR_FP = (
    'unique_cl_W256', 'unique_cl_W1024',
    'dram_bank_id', 'dram_bank_freq_W256', 'dram_row_freq_W256',
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


def mix_field(h: np.ndarray, v: np.ndarray, slot: int) -> np.ndarray:
    """把一个离散字段混入行哈希。slot 区分字段位置，避免交换不变。"""
    v = np.asarray(v).astype(np.uint64)
    mult = np.uint64(0x9e3779b97f4a7c15) ^ np.uint64(
        ((slot + 1) * 0x100000001b3) & 0xffffffffffffffff)
    salt = np.uint64(((slot + 17) * 0xc2b2ae3d27d4eb4f) & 0xffffffffffffffff)
    return h ^ splitmix64(v * mult + salt)


def require_columns(tbl: pa.Table, cols: tuple[str, ...] | list[str]) -> None:
    missing = [c for c in cols if c not in tbl.column_names]
    if missing:
        raise KeyError(f'missing columns for V10.3 model-aware dedup: {missing}')


def per_row_hash(tbl: pa.Table) -> np.ndarray:
    """对每行 µop 计算 model-visible / non-identifier 的 64-bit 行哈希。"""
    require_columns(
        tbl,
        list(SCALAR_BOOL) + list(SMALL_INT_FOR_FP) + list(SCALAR_P1C_FOR_FP)
        + list(SCALAR_V10_3_FOR_FP)
        + [f'd{i}' for i in range(4)] + [f'pc{i}' for i in range(4)]
        + ['vaddr', 'paddr', 'cacheline_addr', 'macro_pc',
           'core_id', 'thread_id']
    )
    n = tbl.num_rows
    h = np.zeros(n, dtype=np.uint64)
    slot = 0

    # 14 个 bool 打包到低 14 bit 后整体混入。
    bool_pack = np.zeros(n, dtype=np.uint64)
    for i, k in enumerate(SCALAR_BOOL):
        v = tbl[k].to_numpy().astype(np.uint64)
        bool_pack |= (v & np.uint64(1)) << np.uint64(i)
    h = mix_field(h, bool_pack, slot)
    slot += 1

    # 小整数族：覆盖当前模型消费的 D/I/P0-A/V10.3A 字段，排除 i_oracle_source。
    for k in SMALL_INT_FOR_FP:
        v = tbl[k].to_numpy().astype(np.int64)
        v = np.clip(v, 0, 255).astype(np.uint64)
        h = mix_field(h, v, slot)
        slot += 1

    # P1-C / V10.3 B+C：这些已经是严格因果窗口派生或地址投影特征。
    for k in SCALAR_P1C_FOR_FP + SCALAR_V10_3_FOR_FP:
        v = tbl[k].to_numpy().astype(np.int64)
        v = np.clip(v, 0, 2047).astype(np.uint64)
        h = mix_field(h, v, slot)
        slot += 1

    # producer dist 桶化
    for i in range(4):
        v = bucketize_dist(tbl[f'd{i}'].to_numpy())
        h = mix_field(h, v, slot)
        slot += 1

    # producer class（sentinel 255 -> 7）
    for i in range(4):
        v = tbl[f'pc{i}'].to_numpy().astype(np.int64)
        v = np.where(v == 255, 7, v)
        v = np.clip(v, 0, 7).astype(np.uint64)
        h = mix_field(h, v, slot)
        slot += 1

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
    for _name, _mult, raw in addr_inputs:
        v = hash_addr_bucket(raw)
        h = mix_field(h, v, slot)
        slot += 1

    # i_group_*：与 ml.dataset.py 的派生语义对齐，但在整段上预计算。
    cid = tbl['core_id'].to_numpy()
    tid = tbl['thread_id'].to_numpy()
    macro_pc = tbl['macro_pc'].to_numpy().astype(np.uint64)
    macro_cl = (macro_pc >> np.uint64(6)).astype(np.int64)
    i_group_head = np.ones(n, dtype=np.uint64)
    if n > 1:
        i_group_head[1:] = (
            (macro_cl[1:] != macro_cl[:-1])
            | (cid[1:] != cid[:-1])
            | (tid[1:] != tid[:-1])
        ).astype(np.uint64)
    i_group_pos = np.zeros(n, dtype=np.uint64)
    running = 0
    for i in range(n):
        if i_group_head[i]:
            running = 0
        else:
            running = min(running + 1, 15)
        i_group_pos[i] = running
    i_group_bkt = hash_addr_bucket(macro_pc)

    for v in (i_group_head, i_group_pos, i_group_bkt):
        h = mix_field(h, v, slot)
        slot += 1

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


def segment_bounds(tbl: pa.Table) -> tuple[np.ndarray, np.ndarray]:
    """返回 parquet 顺序中的 (core, thread) 连续段边界。"""
    cid = tbl['core_id'].to_numpy()
    tid = tbl['thread_id'].to_numpy()
    n = tbl.num_rows
    new_seg = np.empty(n, dtype=bool)
    new_seg[0] = True
    new_seg[1:] = (cid[1:] != cid[:-1]) | (tid[1:] != tid[:-1])
    starts = np.where(new_seg)[0]
    ends = np.concatenate([starts[1:], np.array([n], dtype=np.int64)])
    return starts, ends


def input_window_fingerprint(tbl: pa.Table, ctx_len: int) -> np.ndarray:
    """当前模型可见非 identifier 特征的 128-window 输入指纹（不含 label）。"""
    starts, ends = segment_bounds(tbl)
    H = per_row_hash(tbl)
    fp = np.empty(tbl.num_rows, dtype=np.uint64)
    MUL = 0x100000001b3  # 64-bit FNV prime（odd → 在 2^64 下可逆）
    for s, e in zip(starts, ends):
        fp[s:e] = window_fingerprint_per_segment(H[s:e], ctx_len, MUL)
    return fp


def label_bucket_hash(tbl: pa.Table, latency_bins: int) -> tuple[np.ndarray, np.ndarray]:
    """返回 (label_hash, mispred_bit)。latency 默认 16-bin log bucket。"""
    f_lat = latency_bucket(tbl['fetch_latency'].to_numpy(), n_bins=latency_bins)
    e_lat = latency_bucket(tbl['execution_latency'].to_numpy(), n_bins=latency_bins)
    mis = tbl['mispredicted'].to_numpy().astype(np.uint64) & np.uint64(1)
    if 'is_fetch_group_head' in tbl.column_names:
        head = tbl['is_fetch_group_head'].to_numpy().astype(np.uint64) & np.uint64(1)
    else:
        head = np.zeros(tbl.num_rows, dtype=np.uint64)
    label_hash = (
        f_lat * np.uint64(0xa5a5a5a5a5a5a5a5)
        ^ e_lat * np.uint64(0x5a5a5a5a5a5a5a5a)
        ^ mis * np.uint64(0xff51afd7ed558ccd)
        ^ head * np.uint64(0xc4ceb9fe1a85ec53)
    )
    return splitmix64(label_hash), mis


def dedup_partition(in_path: Path, out_path: Path, ctx_len: int,
                    protect_positives: bool, latency_bins: int,
                    log) -> tuple[int, int, int]:
    """返回 (n_in, n_out, n_protected)。"""
    tbl = pq.read_table(in_path)
    n = tbl.num_rows
    log(f'    rows = {n:,}')

    # 输入 parquet 已按 (core, thread, micro_seq) 升序；此处直接信任顺序。
    fp = input_window_fingerprint(tbl, ctx_len)

    # 折入锚点 label 桶（避免"行为同但延迟/前端边界显著不同"被合并）
    lh, mis = label_bucket_hash(tbl, latency_bins)
    fp = fp ^ lh

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
    ap.add_argument('--latency-bins', type=int, default=16,
                    help='fetch/exec latency 的 log bucket 数；V10.3 默认 16')
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
            protect_positives=not args.no_protect_positives,
            latency_bins=args.latency_bins, log=log)
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
    m['dedup_method'] = 'v10_3_modelaware_c_dup_window_polyhash'
    m['dedup_context_len'] = args.context_len
    m['dedup_latency_bins'] = args.latency_bins
    m['dedup_hash_features'] = {
        'bool': list(SCALAR_BOOL),
        'small_int': list(SMALL_INT_FOR_FP),
        'p1c': list(SCALAR_P1C_FOR_FP),
        'v10_3': list(SCALAR_V10_3_FOR_FP),
        'producer': [f'd{i}/pc{i}' for i in range(4)],
        'addr_buckets': ['vaddr_bucket', 'paddr_bucket',
                         'cline_bucket', 'cline_p_bucket'],
        'derived_i_group': ['i_group_head', 'i_group_pos', 'i_group_bkt'],
        'excluded_identifiers': ['macro_pc_id', 'macro_pc', 'micro_pc',
                                 'raw vaddr/paddr/cacheline_addr/cacheline_paddr',
                                 'core_id', 'thread_id', 'pos_in_thread',
                                 'workload'],
    }
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
