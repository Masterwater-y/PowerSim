#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Tuple

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq


AUDIT_BOOL_COLS: Tuple[str, ...] = (
    'is_load',
    'is_store',
    'is_branch',
    'is_branch_cond',
    'is_call',
    'is_return',
    'is_int',
    'is_fp',
    'is_simd',
    'is_microop',
    'is_last_microop',
)
AUDIT_QUANTILES: Tuple[float, ...] = (0.0, 0.5, 0.9, 0.95, 0.99, 1.0)
MASK64 = np.uint64(0xFFFFFFFFFFFFFFFF)


@dataclass
class AuditStats:
    rows: int = 0
    head_pos: int = 0
    mispred_pos: int = 0
    fetch_pos: int = 0
    nonhead_fetch_pos: int = 0
    bool_pos: Dict[str, int] = field(
        default_factory=lambda: {k: 0 for k in AUDIT_BOOL_COLS}
    )
    fetch_samples: List[np.ndarray] = field(default_factory=list)
    exec_samples: List[np.ndarray] = field(default_factory=list)


def log(msg: str) -> None:
    ts = time.strftime('%H:%M:%S')
    print(f'[{ts}] {msg}', file=sys.stderr, flush=True)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description='按稳定 chunk-hash 把 parquet 数据集切成 train/val，并输出分布体检报告。'
    )
    ap.add_argument(
        '--input-root',
        default='${TAO_DATAGEN_ROOT:-${TAO_ROOT}/datagen}/tmp/06031920/final_balanced_50000000_pq',
        help='输入 parquet 根目录',
    )
    ap.add_argument(
        '--out-root',
        required=True,
        help='输出根目录；脚本会创建 train/ / val/ / split_report.*',
    )
    ap.add_argument(
        '--workloads',
        nargs='*',
        default=None,
        help='只处理指定 workload；默认使用 meta.json 中全部 workload',
    )
    ap.add_argument('--val-ratio', type=float, default=0.05, help='验证集比例，默认 0.05')
    ap.add_argument(
        '--chunk-size',
        type=int,
        default=262144,
        help='thread 内连续块大小，默认 262144',
    )
    ap.add_argument(
        '--guard-band',
        type=int,
        default=1024,
        help='相邻 chunk 跨 split 时，边界两侧各丢弃的行数，默认 1024',
    )
    ap.add_argument(
        '--batch-size',
        type=int,
        default=262144,
        help='按批流式读取 parquet 的 batch size，默认 262144',
    )
    ap.add_argument(
        '--report-sample-mod',
        type=int,
        default=512,
        help='报告采样模数，越小样本越多；默认每 512 行采 1 行',
    )
    ap.add_argument(
        '--hash-salt',
        default='tao_train_train_val_split_v1',
        help='稳定 hash 的 salt',
    )
    ap.add_argument(
        '--compression',
        default='zstd',
        help='输出 parquet 压缩算法，默认 zstd',
    )
    ap.add_argument(
        '--force',
        action='store_true',
        help='若 out-root 已存在则先删除再重建',
    )
    return ap.parse_args()


def load_meta(root: str) -> dict:
    meta_path = os.path.join(root, 'meta.json')
    with open(meta_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def ensure_out_root(path: str, force: bool) -> None:
    if os.path.exists(path):
        if not force:
            raise FileExistsError(f'输出目录已存在：{path}；如需覆盖请加 --force')
        shutil.rmtree(path)
    os.makedirs(path, exist_ok=True)


def salt_to_u64(text: str) -> int:
    return int.from_bytes(hashlib.blake2b(text.encode('utf-8'), digest_size=8).digest(), 'little')


def splitmix64(x: np.ndarray) -> np.ndarray:
    x = (x + np.uint64(0x9E3779B97F4A7C15)) & MASK64
    x = ((x ^ (x >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)) & MASK64
    x = ((x ^ (x >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)) & MASK64
    x = (x ^ (x >> np.uint64(31))) & MASK64
    return x


def pack_thread_key(core_id: np.ndarray, thread_id: np.ndarray) -> np.ndarray:
    core_u = core_id.astype(np.uint64, copy=False) & np.uint64(0xFFFFFFFF)
    tid_u = thread_id.astype(np.uint64, copy=False) & np.uint64(0xFFFFFFFF)
    return ((core_u << np.uint64(32)) | tid_u).astype(np.uint64, copy=False)


def hash_mask_for_chunks(
    workload_code: int,
    core_id: np.ndarray,
    thread_id: np.ndarray,
    chunk_id: np.ndarray,
    salt_u64: int,
    val_ratio: float,
) -> np.ndarray:
    if val_ratio <= 0.0:
        return np.zeros_like(chunk_id, dtype=bool)
    if val_ratio >= 1.0:
        return np.ones_like(chunk_id, dtype=bool)
    threshold = int(val_ratio * (1 << 64))
    x = np.full(chunk_id.shape, np.uint64(salt_u64), dtype=np.uint64)
    x ^= (np.uint64(workload_code + 1) * np.uint64(0x9E3779B185EBCA87)) & MASK64
    x ^= ((core_id.astype(np.uint64, copy=False) + np.uint64(1)) *
          np.uint64(0xC2B2AE3D27D4EB4F)) & MASK64
    x ^= ((thread_id.astype(np.uint64, copy=False) + np.uint64(1)) *
          np.uint64(0x165667B19E3779F9)) & MASK64
    x ^= ((chunk_id.astype(np.uint64, copy=False) + np.uint64(1)) *
          np.uint64(0x85EBCA77C2B2AE63)) & MASK64
    h = splitmix64(x)
    return h < np.uint64(threshold)


def sample_mask_for_report(
    core_id: np.ndarray,
    thread_id: np.ndarray,
    pos_in_thread: np.ndarray,
    salt_u64: int,
    sample_mod: int,
) -> np.ndarray:
    if sample_mod <= 1:
        return np.ones_like(pos_in_thread, dtype=bool)
    x = np.full(pos_in_thread.shape, np.uint64(salt_u64 ^ 0xD6E8FEB86659FD93), dtype=np.uint64)
    x ^= ((core_id.astype(np.uint64, copy=False) + np.uint64(1)) *
          np.uint64(0x94D049BB133111EB)) & MASK64
    x ^= ((thread_id.astype(np.uint64, copy=False) + np.uint64(1)) *
          np.uint64(0x369DEA0F31A53F85)) & MASK64
    x ^= ((pos_in_thread.astype(np.uint64, copy=False) + np.uint64(1)) *
          np.uint64(0xDB4F0B9175AE2165)) & MASK64
    h = splitmix64(x)
    return (h % np.uint64(sample_mod)) == np.uint64(0)


def detect_thread_max_chunk(path: str, chunk_size: int, batch_size: int) -> Dict[int, int]:
    pf = pq.ParquetFile(path)
    thread_max_chunk: Dict[int, int] = {}
    for batch in pf.iter_batches(
        batch_size=batch_size,
        columns=['core_id', 'thread_id', 'pos_in_thread'],
        use_threads=True,
    ):
        core = batch.column(0).to_numpy(zero_copy_only=False)
        tid = batch.column(1).to_numpy(zero_copy_only=False)
        pos = batch.column(2).to_numpy(zero_copy_only=False)
        if len(pos) == 0:
            continue
        keys = pack_thread_key(core, tid)
        starts = np.empty(len(pos), dtype=bool)
        starts[0] = True
        starts[1:] = keys[1:] != keys[:-1]
        seg_starts = np.flatnonzero(starts)
        seg_ends = np.concatenate([seg_starts[1:], np.array([len(pos)], dtype=np.int64)])
        for s, e in zip(seg_starts.tolist(), seg_ends.tolist()):
            key = int(keys[s])
            max_chunk = int(pos[e - 1] // chunk_size)
            thread_max_chunk[key] = max_chunk
    return thread_max_chunk


def init_audit_tree(workloads: Iterable[str]) -> Dict[str, Dict[str, AuditStats]]:
    return {
        'train': {w: AuditStats() for w in workloads},
        'val': {w: AuditStats() for w in workloads},
    }


def update_audit_stats(
    stats: AuditStats,
    arrays: Dict[str, np.ndarray],
    keep_mask: np.ndarray,
    sample_mask: np.ndarray,
) -> None:
    n = int(keep_mask.sum())
    if n == 0:
        return
    stats.rows += n
    fetch = arrays['fetch_latency'][keep_mask]
    exec_lat = arrays['execution_latency'][keep_mask]
    head = arrays['is_fetch_group_head'][keep_mask] > 0
    mispred = arrays['mispredicted'][keep_mask] > 0
    fetch_pos = fetch > 0
    stats.head_pos += int(head.sum())
    stats.mispred_pos += int(mispred.sum())
    stats.fetch_pos += int(fetch_pos.sum())
    stats.nonhead_fetch_pos += int((fetch_pos & (~head)).sum())
    for col in AUDIT_BOOL_COLS:
        stats.bool_pos[col] += int(arrays[col][keep_mask].sum())
    local_sample = sample_mask[keep_mask]
    if np.any(local_sample):
        stats.fetch_samples.append(fetch[local_sample].astype(np.int64, copy=False))
        stats.exec_samples.append(exec_lat[local_sample].astype(np.int64, copy=False))


def combine_stats(items: Iterable[AuditStats]) -> AuditStats:
    out = AuditStats()
    for s in items:
        out.rows += s.rows
        out.head_pos += s.head_pos
        out.mispred_pos += s.mispred_pos
        out.fetch_pos += s.fetch_pos
        out.nonhead_fetch_pos += s.nonhead_fetch_pos
        for col in AUDIT_BOOL_COLS:
            out.bool_pos[col] += s.bool_pos[col]
        if s.fetch_samples:
            out.fetch_samples.extend(s.fetch_samples)
        if s.exec_samples:
            out.exec_samples.extend(s.exec_samples)
    return out


def safe_rate(num: int, den: int) -> float:
    return float(num) / float(den) if den else 0.0


def sample_quantiles(samples: List[np.ndarray]) -> Dict[str, float]:
    if not samples:
        return {}
    arr = np.concatenate(samples, axis=0)
    if arr.size == 0:
        return {}
    qv = np.quantile(arr, AUDIT_QUANTILES)
    return {
        'min': float(qv[0]),
        'p50': float(qv[1]),
        'p90': float(qv[2]),
        'p95': float(qv[3]),
        'p99': float(qv[4]),
        'max': float(qv[5]),
        'sample_size': int(arr.size),
    }


def stats_to_report(stats: AuditStats) -> Dict[str, object]:
    bool_rates = {col: safe_rate(stats.bool_pos[col], stats.rows) for col in AUDIT_BOOL_COLS}
    return {
        'rows': int(stats.rows),
        'head_rate': safe_rate(stats.head_pos, stats.rows),
        'mispred_rate': safe_rate(stats.mispred_pos, stats.rows),
        'fetch_positive_rate': safe_rate(stats.fetch_pos, stats.rows),
        'fetch_positive_given_head': safe_rate(stats.fetch_pos, stats.head_pos),
        'fetch_positive_nonhead_rate': safe_rate(stats.nonhead_fetch_pos, max(stats.rows - stats.head_pos, 0)),
        'bool_rates': bool_rates,
        'fetch_latency_quantiles_sampled': sample_quantiles(stats.fetch_samples),
        'execution_latency_quantiles_sampled': sample_quantiles(stats.exec_samples),
    }


def build_meta(
    split_role: str,
    workloads: List[str],
    by_workload_count: Dict[str, int],
    source_meta: dict,
    input_root: str,
    sibling_root: str,
    dropped_rows: int,
    args: argparse.Namespace,
) -> dict:
    meta = dict(source_meta)
    meta.update({
        'total_rows': int(sum(by_workload_count.values())),
        'workloads': workloads,
        'by_workload_count': by_workload_count,
        'source_dataset': input_root,
        'paired_split_root': sibling_root,
        'split_role': split_role,
        'split_method': 'stable_chunk_hash',
        'split_config': {
            'val_ratio': args.val_ratio,
            'chunk_size': args.chunk_size,
            'guard_band': args.guard_band,
            'hash_salt': args.hash_salt,
        },
        'dropped_rows_due_to_guard_band': int(dropped_rows),
    })
    return meta


def write_json(path: str, payload: dict) -> None:
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def format_pct(x: float) -> str:
    return f'{100.0 * x:.3f}%'


def format_quantiles(q: Dict[str, float]) -> str:
    if not q:
        return 'n/a'
    return (
        f"min={q['min']:.1f} p50={q['p50']:.1f} p90={q['p90']:.1f} "
        f"p95={q['p95']:.1f} p99={q['p99']:.1f} max={q['max']:.1f} "
        f"(sample={int(q['sample_size'])})"
    )


def build_markdown_report(report: dict) -> str:
    lines: List[str] = []
    cfg = report['config']
    totals = report['totals']
    lines.append('# Train/Val Split Report')
    lines.append('')
    lines.append('## Config')
    lines.append(f"- input_root: `{cfg['input_root']}`")
    lines.append(f"- train_root: `{cfg['train_root']}`")
    lines.append(f"- val_root: `{cfg['val_root']}`")
    lines.append(f"- val_ratio: `{cfg['val_ratio']}`")
    lines.append(f"- chunk_size: `{cfg['chunk_size']}`")
    lines.append(f"- guard_band: `{cfg['guard_band']}`")
    lines.append(f"- hash_salt: `{cfg['hash_salt']}`")
    lines.append(f"- report_sample_mod: `{cfg['report_sample_mod']}`")
    lines.append('')
    lines.append('## Totals')
    lines.append(f"- input_rows: `{totals['input_rows']}`")
    lines.append(f"- train_rows: `{totals['train_rows']}`")
    lines.append(f"- val_rows: `{totals['val_rows']}`")
    lines.append(f"- dropped_rows: `{totals['dropped_rows']}`")
    lines.append(f"- effective_val_ratio_on_kept: `{format_pct(totals['effective_val_ratio_on_kept'])}`")
    lines.append(f"- dropped_ratio_on_input: `{format_pct(totals['dropped_ratio_on_input'])}`")
    lines.append('')
    lines.append('## Overall Distribution')
    for split_name in ('train', 'val'):
        s = report['overall'][split_name]
        lines.append(f"### {split_name}")
        lines.append(f"- rows: `{s['rows']}`")
        lines.append(f"- head_rate: `{format_pct(s['head_rate'])}`")
        lines.append(f"- mispred_rate: `{format_pct(s['mispred_rate'])}`")
        lines.append(f"- fetch_positive_rate: `{format_pct(s['fetch_positive_rate'])}`")
        lines.append(f"- fetch_positive_given_head: `{format_pct(s['fetch_positive_given_head'])}`")
        lines.append(f"- fetch_positive_nonhead_rate: `{format_pct(s['fetch_positive_nonhead_rate'])}`")
        lines.append(f"- fetch_latency(sampled): `{format_quantiles(s['fetch_latency_quantiles_sampled'])}`")
        lines.append(f"- execution_latency(sampled): `{format_quantiles(s['execution_latency_quantiles_sampled'])}`")
        bool_summary = ', '.join(
            f"{k}={format_pct(v)}" for k, v in s['bool_rates'].items()
        )
        lines.append(f"- bool_rates: `{bool_summary}`")
    lines.append('')
    lines.append('## By Workload')
    for wl, item in report['by_workload'].items():
        lines.append(f"### {wl}")
        lines.append(f"- input_rows: `{item['input_rows']}`")
        lines.append(f"- train_rows: `{item['train']['rows']}`")
        lines.append(f"- val_rows: `{item['val']['rows']}`")
        lines.append(f"- dropped_rows: `{item['dropped_rows']}`")
        lines.append(f"- effective_val_ratio_on_kept: `{format_pct(item['effective_val_ratio_on_kept'])}`")
        lines.append(
            f"- head_rate(train/val/delta): `{format_pct(item['train']['head_rate'])}` / "
            f"`{format_pct(item['val']['head_rate'])}` / "
            f"`{format_pct(item['drift']['head_rate_abs_delta'])}`"
        )
        lines.append(
            f"- mispred_rate(train/val/delta): `{format_pct(item['train']['mispred_rate'])}` / "
            f"`{format_pct(item['val']['mispred_rate'])}` / "
            f"`{format_pct(item['drift']['mispred_rate_abs_delta'])}`"
        )
        lines.append(
            f"- fetch_positive_rate(train/val/delta): `{format_pct(item['train']['fetch_positive_rate'])}` / "
            f"`{format_pct(item['val']['fetch_positive_rate'])}` / "
            f"`{format_pct(item['drift']['fetch_positive_rate_abs_delta'])}`"
        )
        lines.append(
            f"- fetch_latency(train sampled): `{format_quantiles(item['train']['fetch_latency_quantiles_sampled'])}`"
        )
        lines.append(
            f"- fetch_latency(val sampled): `{format_quantiles(item['val']['fetch_latency_quantiles_sampled'])}`"
        )
        lines.append(
            f"- execution_latency(train sampled): `{format_quantiles(item['train']['execution_latency_quantiles_sampled'])}`"
        )
        lines.append(
            f"- execution_latency(val sampled): `{format_quantiles(item['val']['execution_latency_quantiles_sampled'])}`"
        )
    lines.append('')
    lines.append('## Notes')
    lines.append('- `fetch_latency` / `execution_latency` 分位数来自 hash 抽样，不是全量精确分位数。')
    lines.append('- guard band 仅在相邻 chunk 被分到不同 split 时生效，用于削弱上下文窗口泄漏。')
    return '\n'.join(lines) + '\n'


def process_one_workload(
    workload: str,
    workload_code: int,
    input_root: str,
    train_root: str,
    val_root: str,
    args: argparse.Namespace,
    salt_u64: int,
    audit: Dict[str, Dict[str, AuditStats]],
) -> Dict[str, int]:
    src_path = os.path.join(input_root, f'workload={workload}', 'part-000.parquet')
    log(f'{workload}: pre-scan thread/chunk boundaries')
    thread_max_chunk = detect_thread_max_chunk(src_path, args.chunk_size, args.batch_size)
    log(f'{workload}: found {len(thread_max_chunk)} thread segments')

    os.makedirs(os.path.join(train_root, f'workload={workload}'), exist_ok=True)
    os.makedirs(os.path.join(val_root, f'workload={workload}'), exist_ok=True)
    train_path = os.path.join(train_root, f'workload={workload}', 'part-000.parquet')
    val_path = os.path.join(val_root, f'workload={workload}', 'part-000.parquet')

    pf = pq.ParquetFile(src_path)
    train_writer = pq.ParquetWriter(train_path, pf.schema_arrow, compression=args.compression)
    val_writer = pq.ParquetWriter(val_path, pf.schema_arrow, compression=args.compression)

    input_rows = 0
    train_rows = 0
    val_rows = 0
    dropped_rows = 0

    try:
        for batch_idx, batch in enumerate(
            pf.iter_batches(batch_size=args.batch_size, use_threads=True)
        ):
            table = pa.Table.from_batches([batch], schema=pf.schema_arrow)
            core = batch.column(batch.schema.get_field_index('core_id')).to_numpy(zero_copy_only=False)
            tid = batch.column(batch.schema.get_field_index('thread_id')).to_numpy(zero_copy_only=False)
            pos = batch.column(batch.schema.get_field_index('pos_in_thread')).to_numpy(zero_copy_only=False)
            n = len(pos)
            if n == 0:
                continue
            input_rows += n

            keys = pack_thread_key(core, tid)
            seg_start_flags = np.empty(n, dtype=bool)
            seg_start_flags[0] = True
            seg_start_flags[1:] = keys[1:] != keys[:-1]
            seg_starts = np.flatnonzero(seg_start_flags)
            seg_ends = np.concatenate([seg_starts[1:], np.array([n], dtype=np.int64)])
            max_chunk = np.empty(n, dtype=np.int64)
            for s, e in zip(seg_starts.tolist(), seg_ends.tolist()):
                max_chunk[s:e] = thread_max_chunk[int(keys[s])]

            chunk_id = pos.astype(np.int64, copy=False) // int(args.chunk_size)
            offset_in_chunk = pos.astype(np.int64, copy=False) - chunk_id * int(args.chunk_size)
            tail_to_boundary = ((chunk_id + 1) * int(args.chunk_size) - 1) - pos.astype(np.int64, copy=False)

            cur_is_val = hash_mask_for_chunks(workload_code, core, tid, chunk_id, salt_u64, args.val_ratio)
            prev_exists = chunk_id > 0
            next_exists = chunk_id < max_chunk
            prev_is_val = hash_mask_for_chunks(
                workload_code, core, tid, np.maximum(chunk_id - 1, 0), salt_u64, args.val_ratio
            )
            next_is_val = hash_mask_for_chunks(
                workload_code, core, tid, chunk_id + 1, salt_u64, args.val_ratio
            )
            drop_left = prev_exists & (cur_is_val != prev_is_val) & (offset_in_chunk < int(args.guard_band))
            drop_right = next_exists & (cur_is_val != next_is_val) & (tail_to_boundary < int(args.guard_band))
            keep_mask = ~(drop_left | drop_right)
            train_mask = keep_mask & (~cur_is_val)
            val_mask = keep_mask & cur_is_val

            drop_n = int((~keep_mask).sum())
            train_n = int(train_mask.sum())
            val_n = int(val_mask.sum())
            dropped_rows += drop_n
            train_rows += train_n
            val_rows += val_n

            if train_n > 0:
                train_writer.write_table(table.filter(pa.array(train_mask)))
            if val_n > 0:
                val_writer.write_table(table.filter(pa.array(val_mask)))

            arrays: Dict[str, np.ndarray] = {
                'fetch_latency': batch.column(batch.schema.get_field_index('fetch_latency')).to_numpy(zero_copy_only=False),
                'execution_latency': batch.column(batch.schema.get_field_index('execution_latency')).to_numpy(zero_copy_only=False),
                'mispredicted': batch.column(batch.schema.get_field_index('mispredicted')).to_numpy(zero_copy_only=False),
                'is_fetch_group_head': batch.column(batch.schema.get_field_index('is_fetch_group_head')).to_numpy(zero_copy_only=False),
            }
            for col in AUDIT_BOOL_COLS:
                arrays[col] = batch.column(batch.schema.get_field_index(col)).to_numpy(zero_copy_only=False)
            sampled = sample_mask_for_report(core, tid, pos, salt_u64, args.report_sample_mod)
            update_audit_stats(audit['train'][workload], arrays, train_mask, sampled)
            update_audit_stats(audit['val'][workload], arrays, val_mask, sampled)

            if (batch_idx + 1) % 16 == 0:
                log(
                    f'{workload}: batches={batch_idx + 1} rows={input_rows} '
                    f'train={train_rows} val={val_rows} dropped={dropped_rows}'
                )
    finally:
        train_writer.close()
        val_writer.close()

    return {
        'input_rows': int(input_rows),
        'train_rows': int(train_rows),
        'val_rows': int(val_rows),
        'dropped_rows': int(dropped_rows),
    }


def main() -> None:
    args = parse_args()
    if not (0.0 <= args.val_ratio <= 1.0):
        raise ValueError('--val-ratio 必须在 [0, 1] 范围内')
    if args.chunk_size <= 0:
        raise ValueError('--chunk-size 必须 > 0')
    if args.guard_band < 0:
        raise ValueError('--guard-band 必须 >= 0')
    if args.guard_band * 2 >= args.chunk_size:
        raise ValueError('--guard-band 过大：必须满足 2 * guard_band < chunk_size')
    if args.batch_size <= 0:
        raise ValueError('--batch-size 必须 > 0')
    if args.report_sample_mod <= 0:
        raise ValueError('--report-sample-mod 必须 > 0')

    source_meta = load_meta(args.input_root)
    workloads = args.workloads or source_meta['workloads']
    out_root = os.path.abspath(args.out_root)
    train_root = os.path.join(out_root, 'train')
    val_root = os.path.join(out_root, 'val')

    ensure_out_root(out_root, args.force)
    os.makedirs(train_root, exist_ok=True)
    os.makedirs(val_root, exist_ok=True)

    salt_u64 = salt_to_u64(args.hash_salt)
    audit = init_audit_tree(workloads)
    workload_counts: Dict[str, Dict[str, int]] = {}

    vocab_path = os.path.join(args.input_root, 'vocab.json')
    if os.path.exists(vocab_path):
        shutil.copy2(vocab_path, os.path.join(train_root, 'vocab.json'))
        shutil.copy2(vocab_path, os.path.join(val_root, 'vocab.json'))

    for idx, workload in enumerate(workloads):
        stats = process_one_workload(
            workload=workload,
            workload_code=idx,
            input_root=args.input_root,
            train_root=train_root,
            val_root=val_root,
            args=args,
            salt_u64=salt_u64,
            audit=audit,
        )
        workload_counts[workload] = stats
        log(
            f"{workload}: done input={stats['input_rows']} "
            f"train={stats['train_rows']} val={stats['val_rows']} dropped={stats['dropped_rows']}"
        )

    train_by_workload = {w: int(workload_counts[w]['train_rows']) for w in workloads}
    val_by_workload = {w: int(workload_counts[w]['val_rows']) for w in workloads}
    dropped_rows_total = int(sum(workload_counts[w]['dropped_rows'] for w in workloads))
    input_rows_total = int(sum(workload_counts[w]['input_rows'] for w in workloads))
    train_rows_total = int(sum(train_by_workload.values()))
    val_rows_total = int(sum(val_by_workload.values()))
    kept_rows_total = train_rows_total + val_rows_total

    train_meta = build_meta(
        split_role='train',
        workloads=workloads,
        by_workload_count=train_by_workload,
        source_meta=source_meta,
        input_root=args.input_root,
        sibling_root=val_root,
        dropped_rows=dropped_rows_total,
        args=args,
    )
    val_meta = build_meta(
        split_role='val',
        workloads=workloads,
        by_workload_count=val_by_workload,
        source_meta=source_meta,
        input_root=args.input_root,
        sibling_root=train_root,
        dropped_rows=dropped_rows_total,
        args=args,
    )
    write_json(os.path.join(train_root, 'meta.json'), train_meta)
    write_json(os.path.join(val_root, 'meta.json'), val_meta)

    overall_train = combine_stats(audit['train'].values())
    overall_val = combine_stats(audit['val'].values())
    by_workload_report: Dict[str, dict] = {}
    for workload in workloads:
        train_report = stats_to_report(audit['train'][workload])
        val_report = stats_to_report(audit['val'][workload])
        by_workload_report[workload] = {
            'input_rows': int(workload_counts[workload]['input_rows']),
            'dropped_rows': int(workload_counts[workload]['dropped_rows']),
            'effective_val_ratio_on_kept': safe_rate(
                workload_counts[workload]['val_rows'],
                workload_counts[workload]['train_rows'] + workload_counts[workload]['val_rows'],
            ),
            'train': train_report,
            'val': val_report,
            'drift': {
                'head_rate_abs_delta': abs(train_report['head_rate'] - val_report['head_rate']),
                'mispred_rate_abs_delta': abs(train_report['mispred_rate'] - val_report['mispred_rate']),
                'fetch_positive_rate_abs_delta': abs(
                    train_report['fetch_positive_rate'] - val_report['fetch_positive_rate']
                ),
            },
        }

    report = {
        'config': {
            'input_root': os.path.abspath(args.input_root),
            'out_root': out_root,
            'train_root': train_root,
            'val_root': val_root,
            'workloads': workloads,
            'val_ratio': args.val_ratio,
            'chunk_size': args.chunk_size,
            'guard_band': args.guard_band,
            'batch_size': args.batch_size,
            'report_sample_mod': args.report_sample_mod,
            'hash_salt': args.hash_salt,
            'compression': args.compression,
        },
        'totals': {
            'input_rows': input_rows_total,
            'train_rows': train_rows_total,
            'val_rows': val_rows_total,
            'dropped_rows': dropped_rows_total,
            'effective_val_ratio_on_kept': safe_rate(val_rows_total, kept_rows_total),
            'dropped_ratio_on_input': safe_rate(dropped_rows_total, input_rows_total),
        },
        'overall': {
            'train': stats_to_report(overall_train),
            'val': stats_to_report(overall_val),
        },
        'by_workload': by_workload_report,
    }
    write_json(os.path.join(out_root, 'split_report.json'), report)
    with open(os.path.join(out_root, 'split_report.md'), 'w', encoding='utf-8') as f:
        f.write(build_markdown_report(report))

    log(
        f'split done: train={train_rows_total} val={val_rows_total} '
        f'dropped={dropped_rows_total} report={os.path.join(out_root, "split_report.md")}'
    )


if __name__ == '__main__':
    main()
