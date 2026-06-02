#!/usr/bin/env python3
"""跨 workload 稳定期均衡采样器（V9.5 schema v2，方案 B 列式管线）。

输入：每个 workload 的 gem5 run 目录，目录下含
  tao_trace/board.processor.coresN.core.tao_trace.tao_trace.records.micro.jsonl
  tao_trace/board.processor.coresN.core.tao_trace.tao_trace.labels.micro.jsonl
records / labels 行级 1:1，与 build_micro_dataset.py 同源。

采样规则：
  - 历史上曾跳过 core0（旧版 V9.x 之前 ROI 边界不严，core0 含 ~30k
    初始化行）。**V10 之后** `m5_work_begin/end` 严格限定 ROI，core0
    在 ROI 内是 main thread 的真实业务计算（pthread_harness 让 main
    thread 直接以 tid=0 运行 worker），不再剔除。默认 business_cores
    = (0, 1, 2, 3)；如需复现旧行为可通过 --exclude-cores 0 指定。
  - 每条 (core, thread) 的稳定窗 = 行序号在
    [max(head_skip, context_warmup_skip), 1 - tail_skip] 之间（默认 5% / 5%）。
  - 每 workload 目标行数 = total_target / n_workloads；若稳定窗容量不足
    则取尽，缺口按其他 workload 稳定窗容量比例分摊。
  - 每 workload 内目标行数按 (core, thread) 稳定窗容量比例分配；窗内
    使用等距 stride 索引，保持原 trace 时序。

V10 方案 B（列式管线）：
  - 用 pyarrow.json.read_json 多线程读 records/labels jsonl
  - 在 pa.Table / numpy 上做分组、稳定窗 mask、stride 索引选样
  - **直接输出 parquet**（每 workload 一个 part-000.parquet）
  - --legacy-jsonl-out 选项可回退到旧 nested-json jsonl emit（仅 debug）
  - fetch_latency 仍取相对原 trace 内 prev_fetch 的差值（按 thread 出现顺序差分）
  - exec_latency = ready_tick - fetch_tick
  - is_fetch_group_head：fetch_lat>0 -> 1，每 thread 第一条强制为 1
"""
import argparse
import glob
import json
import os
import re
import sys
from collections import defaultdict

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.json as paj
import pyarrow.parquet as pq


# 一次性 warning 标志：COMPAT-OLD-50M 路径仅 print 一次
_WARNED = {'cacheline_paddr': False, 'i_side': False}


# ============================================================ helpers
def core_id_of(name: str) -> int:
    m = re.search(r'cores(\d+)', name)
    return int(m.group(1)) if m else -1


def detect_n_cores(run_dir: str) -> int:
    """从 run 目录推断当前实验的 core 数：
    扫 tao_trace/board.processor.coresN.* 文件名取 max(N)+1；
    扫不到（异常情况）回退到 4。
    注：曾尝试用 uarch_profile.json 的 cache.l1d 数量做 fallback，但该字段
    可能含 L1I+L1D+L2+L3 多 level 而被高估，已废弃。"""
    rec_files = glob.glob(os.path.join(
        run_dir, 'tao_trace',
        'board.processor.cores*.core.tao_trace.tao_trace.records.micro.jsonl'))
    cores = set()
    for f in rec_files:
        cid = core_id_of(os.path.basename(f))
        if cid >= 0:
            cores.add(cid)
    if cores:
        return max(cores) + 1
    return 4


# ============================================================ 列式核心
def read_core_tables(rec_path: str, lab_path: str):
    """读单 core 的 records / labels jsonl 为 pa.Table。
    pyarrow.json.read_json 内部多线程解析，远快于 python json。
    """
    rec_t = paj.read_json(rec_path)
    lab_t = paj.read_json(lab_path)

    # COMPAT-OLD-50M：旧 raw 不含 cacheline_paddr，回退 cacheline_addr
    if 'cacheline_paddr' not in rec_t.schema.names:
        if not _WARNED['cacheline_paddr']:
            print("[sample][COMPAT-OLD-50M] cacheline_paddr 缺失 -> "
                  "fallback cacheline_addr (vaddr-line)", file=sys.stderr)
            _WARNED['cacheline_paddr'] = True
        rec_t = rec_t.append_column(
            'cacheline_paddr', rec_t.column('cacheline_addr'))

    # COMPAT-OLD-50M：i-side 4 字段缺失时填 -1（schema v1 -> v2 升级）
    for col in ('i_path_class', 'i_coh_oracle', 'i_mesi_before',
                'i_oracle_source'):
        if col not in rec_t.schema.names:
            if not _WARNED['i_side']:
                print("[sample][COMPAT-OLD-50M] i-side 字段缺失 -> -1",
                      file=sys.stderr)
                _WARNED['i_side'] = True
            n = rec_t.num_rows
            rec_t = rec_t.append_column(
                col, pa.array(np.full(n, -1, dtype=np.int64)))

    # COMPAT-P0A：d-side / i-side 各 5 字段缺失时填 0（schema v2 -> v3 升级）。
    #   gem5 端 emitMicroRecord 已恒定输出，缺失只发生在 V10.1 之前的旧 raw。
    p0a_cols = ('d_mshr_depth', 'dtlb_hit', 'd_walker_levels',
                'd_walker_dram_misses', 'd_bank_id',
                'i_mshr_depth', 'itlb_hit', 'i_walker_levels',
                'i_walker_dram_misses', 'i_bank_id')
    for col in p0a_cols:
        if col not in rec_t.schema.names:
            if not _WARNED.get('p0a'):
                print("[sample][COMPAT-OLD-50M] P0-A 字段缺失 -> 0",
                      file=sys.stderr)
                _WARNED['p0a'] = True
            n = rec_t.num_rows
            rec_t = rec_t.append_column(
                col, pa.array(np.zeros(n, dtype=np.int64)))

    # COMPAT-V10.3：A 字段（LLC set residency / lru_pos） 4 列缺失时填 0。
    #   gem5 端 emitMicroRecord 已恒定输出；缺失只发生在 V10.2 之前的旧 raw。
    v10_3_a_cols = ('d_llc_set_residency', 'd_llc_set_lru_pos',
                    'i_llc_set_residency', 'i_llc_set_lru_pos')
    for col in v10_3_a_cols:
        if col not in rec_t.schema.names:
            if not _WARNED.get('v10_3_a'):
                print("[sample][COMPAT-V10.3] A 字段缺失 -> 0",
                      file=sys.stderr)
                _WARNED['v10_3_a'] = True
            n = rec_t.num_rows
            rec_t = rec_t.append_column(
                col, pa.array(np.zeros(n, dtype=np.int64)))

    return rec_t, lab_t


def derive_thread_signals(rec_t: pa.Table, lab_t: pa.Table):
    """对一个 core 的 records/labels 算:
       - fetch_lat (int64, 全行)
       - exec_lat  (int64, 全行)
       - is_head   (int8, 全行)
       - window_per_tid: dict[tid] -> np.array(全局 row idx, 该 thread 出现顺序)
    """
    tid_arr = rec_t.column('thread_id').to_numpy(zero_copy_only=False)
    ft_arr = lab_t.column('fetch_tick').to_numpy(zero_copy_only=False).astype(np.int64)
    rt_arr = lab_t.column('ready_tick').to_numpy(zero_copy_only=False).astype(np.int64)
    n = len(tid_arr)
    fetch_lat = np.zeros(n, dtype=np.int64)
    is_head = np.zeros(n, dtype=np.int8)
    exec_lat = (rt_arr - ft_arr).astype(np.int64)

    # 按 thread 出现顺序差分（与原 prev_fetch 累积语义一致）
    unique_tids, inv = np.unique(tid_arr, return_inverse=True)
    window_per_tid = {}
    for k, tid in enumerate(unique_tids):
        idx = np.where(inv == k)[0]      # 全局行号，原 trace 出现顺序升序
        nt = len(idx)
        ft_t = ft_arr[idx]
        diff = np.empty(nt, dtype=np.int64)
        diff[0] = 0
        if nt > 1:
            diff[1:] = ft_t[1:] - ft_t[:-1]
        fetch_lat[idx] = diff
        head = np.zeros(nt, dtype=np.int8)
        head[0] = 1
        if nt > 1:
            head[1:] = (diff[1:] > 0).astype(np.int8)
        is_head[idx] = head
        window_per_tid[int(tid)] = idx

    return fetch_lat, exec_lat, is_head, window_per_tid


def stable_window_slice(thread_global_idx: np.ndarray,
                        head_skip: float, tail_skip: float,
                        ctx_warmup: int):
    """返回稳定窗对应的全局行号 np.array；不达条件返回 None。"""
    n = len(thread_global_idx)
    if n < 100:
        return None
    lo = max(int(n * head_skip), int(ctx_warmup))
    hi = int(n * (1.0 - tail_skip))
    if hi - lo < 50:
        return None
    return thread_global_idx[lo:hi]


def stride_pick_offsets(window_size: int, n_target: int) -> np.ndarray:
    """等距 stride 选 n_target 个窗口内偏移；保留原 stride_pick 的去重语义。"""
    if n_target <= 0 or window_size <= 0:
        return np.empty(0, dtype=np.int64)
    if n_target >= window_size:
        return np.arange(window_size, dtype=np.int64)
    step = window_size / n_target
    raw = (np.arange(n_target, dtype=np.float64) * step).astype(np.int64)
    # 与原版去重逻辑等价：偶发重复时按出现序去重
    if len(raw) > 1:
        keep = np.empty(len(raw), dtype=bool)
        keep[0] = True
        keep[1:] = raw[1:] != raw[:-1]
        raw = raw[keep]
    return raw


# ============================================================ 全 workload 收集
def collect_workload(run_dir: str, head_skip: float, tail_skip: float,
                     ctx_warmup: int, business_cores=(0, 1, 2, 3)):
    """返回:
        core_data: {cid: {'rec', 'lab', 'fl', 'el', 'ih'}}
        windows:   {(cid, tid): np.array(global idx in core)}
    """
    rec_files = sorted(glob.glob(os.path.join(
        run_dir, 'tao_trace',
        'board.processor.cores*.core.tao_trace.tao_trace.records.micro.jsonl')),
        key=lambda p: core_id_of(os.path.basename(p)))
    core_data = {}
    windows = {}
    for rf in rec_files:
        cid = core_id_of(os.path.basename(rf))
        if cid not in business_cores:
            continue
        lf = rf.replace('.records.micro.jsonl', '.labels.micro.jsonl')
        if not os.path.exists(lf):
            print(f"WARN: missing labels for core {cid}: {lf}", file=sys.stderr)
            continue
        rec_t, lab_t = read_core_tables(rf, lf)
        fl, el, ih, w_per_tid = derive_thread_signals(rec_t, lab_t)
        core_data[cid] = {'rec': rec_t, 'lab': lab_t, 'fl': fl, 'el': el, 'ih': ih}
        for tid, gidx in w_per_tid.items():
            wnd = stable_window_slice(gidx, head_skip, tail_skip, ctx_warmup)
            if wnd is None:
                continue
            windows[(cid, tid)] = wnd
    return core_data, windows


# ============================================================ flat parquet schema
# 输出 schema：直接 flat（meta + input + uarch_context + labels）
# 下游 pack_to_parquet --from-parquet 会再 cast / 重排。
_INPUT_BOOL = (
    'is_load', 'is_store', 'is_atomic', 'is_branch', 'is_branch_cond',
    'is_branch_indirect', 'is_call', 'is_return', 'is_int', 'is_fp',
    'is_simd', 'is_serialize', 'is_microop', 'is_last_microop',
)
_UARCH_SMALL = (
    'mesi_before', 'coh_oracle', 'sharer_bucket', 'owner_dist',
    'dirty_owner', 'path_class', 'inval_fanout', 'same_line_recent',
    'oracle_source',
    'i_path_class', 'i_coh_oracle', 'i_mesi_before', 'i_oracle_source',
    # P0-A：d-side / i-side 各 5 字段（与 packer SCALAR_SMALL_INT 对齐）
    'd_mshr_depth', 'dtlb_hit', 'd_walker_levels',
    'd_walker_dram_misses', 'd_bank_id',
    'i_mshr_depth', 'itlb_hit', 'i_walker_levels',
    'i_walker_dram_misses', 'i_bank_id',
    # V10.3 A：LLC set residency / lru_pos（d/i 各 2 列）
    'd_llc_set_residency', 'd_llc_set_lru_pos',
    'i_llc_set_residency', 'i_llc_set_lru_pos',
)
_INPUT_U64 = ('macro_pc', 'micro_pc', 'vaddr')
_UARCH_U64 = ('paddr', 'cacheline_addr', 'cacheline_paddr')


def make_flat_schema():
    fields = [
        ('workload', pa.string()),
        ('core_id', pa.int32()),
        ('thread_id', pa.int32()),
        ('micro_seq', pa.uint64()),
        ('pick_idx', pa.int32()),
        # input
        ('macro_pc', pa.uint64()),
        ('micro_pc', pa.uint64()),
        ('vaddr', pa.uint64()),
        ('size', pa.int32()),
    ]
    for k in _INPUT_BOOL:
        fields.append((k, pa.int8()))
    fields += [
        ('n_src', pa.int16()),
        ('n_dst', pa.int16()),
        ('producer_dists', pa.list_(pa.int32())),
        ('producer_classes', pa.list_(pa.int32())),
        # uarch_context
        ('seq_num', pa.int64()),
        ('paddr', pa.uint64()),
        ('cacheline_addr', pa.uint64()),
        ('cacheline_paddr', pa.uint64()),
    ]
    for k in _UARCH_SMALL:
        fields.append((k, pa.int16()))
    fields += [
        # labels
        ('fetch_tick', pa.int64()),
        ('ready_tick', pa.int64()),
        ('commit_tick', pa.int64()),
        ('mispredicted', pa.int8()),
        ('fetch_latency', pa.int64()),
        ('execution_latency', pa.int64()),
        ('is_fetch_group_head', pa.int8()),
    ]
    return pa.schema([pa.field(n, t) for n, t in fields])


def _cast(arr, target):
    return pc.cast(arr, target)


def assemble_flat_table(workload: str, core_data, picks):
    """picks: list of (cid, tid, global_idx_arr, pick_idx_arr)
    返回一个完整 flat pa.Table，按 picks 顺序组装（保证 (core,tid) 内 stride 顺序）。
    """
    schema = make_flat_schema()
    if not picks:
        # 空表
        empty_arrs = [pa.array([], type=f.type) for f in schema]
        return pa.Table.from_arrays(empty_arrs, schema=schema)

    # 按 cid 聚合 take 以减少 take 次数
    by_cid = defaultdict(list)   # cid -> list of (tid, gidx, pidx) 按 picks 顺序
    cid_order = []
    for (cid, tid, gidx, pidx) in picks:
        if cid not in by_cid:
            cid_order.append(cid)
        by_cid[cid].append((tid, gidx, pidx))

    sub_rec_list, sub_lab_list = [], []
    fl_list, el_list, ih_list = [], [], []
    cid_list, tid_list, pick_list = [], [], []
    workload_n_total = 0
    for cid in cid_order:
        items = by_cid[cid]
        all_idx = np.concatenate([t[1] for t in items])
        all_tid = np.concatenate([np.full(len(t[1]), t[0], dtype=np.int32) for t in items])
        all_pidx = np.concatenate([t[2] for t in items])
        n = len(all_idx)
        idx_pa = pa.array(all_idx)
        sub_rec_list.append(core_data[cid]['rec'].take(idx_pa))
        sub_lab_list.append(core_data[cid]['lab'].take(idx_pa))
        fl_list.append(core_data[cid]['fl'][all_idx])
        el_list.append(core_data[cid]['el'][all_idx])
        ih_list.append(core_data[cid]['ih'][all_idx])
        cid_list.append(np.full(n, cid, dtype=np.int32))
        tid_list.append(all_tid)
        pick_list.append(all_pidx)
        workload_n_total += n

    sub_rec = pa.concat_tables(sub_rec_list)
    sub_lab = pa.concat_tables(sub_lab_list)
    fl_arr = np.concatenate(fl_list)
    el_arr = np.concatenate(el_list)
    ih_arr = np.concatenate(ih_list)
    cid_arr = np.concatenate(cid_list)
    tid_arr = np.concatenate(tid_list)
    pick_arr = np.concatenate(pick_list)
    n = workload_n_total

    # 组装列
    cols = {}
    cols['workload'] = pa.array([workload] * n, type=pa.string())
    cols['core_id'] = pa.array(cid_arr, type=pa.int32())
    cols['thread_id'] = pa.array(tid_arr, type=pa.int32())
    cols['micro_seq'] = _cast(sub_rec.column('micro_seq'), pa.uint64())
    cols['pick_idx'] = pa.array(pick_arr, type=pa.int32())

    cols['macro_pc'] = _cast(sub_rec.column('macro_pc'), pa.uint64())
    cols['micro_pc'] = _cast(sub_rec.column('micro_pc'), pa.uint64())
    cols['vaddr'] = _cast(sub_rec.column('vaddr'), pa.uint64())
    cols['size'] = _cast(sub_rec.column('size'), pa.int32())
    for k in _INPUT_BOOL:
        cols[k] = _cast(sub_rec.column(k), pa.int8())
    cols['n_src'] = _cast(sub_rec.column('n_src'), pa.int16())
    cols['n_dst'] = _cast(sub_rec.column('n_dst'), pa.int16())
    cols['producer_dists'] = _cast(sub_rec.column('producer_dists'),
                                   pa.list_(pa.int32()))
    cols['producer_classes'] = _cast(sub_rec.column('producer_classes'),
                                     pa.list_(pa.int32()))

    cols['seq_num'] = _cast(sub_rec.column('seq_num'), pa.int64())
    cols['paddr'] = _cast(sub_rec.column('paddr'), pa.uint64())
    cols['cacheline_addr'] = _cast(sub_rec.column('cacheline_addr'), pa.uint64())
    cols['cacheline_paddr'] = _cast(sub_rec.column('cacheline_paddr'), pa.uint64())
    for k in _UARCH_SMALL:
        cols[k] = _cast(sub_rec.column(k), pa.int16())

    cols['fetch_tick'] = _cast(sub_lab.column('fetch_tick'), pa.int64())
    cols['ready_tick'] = _cast(sub_lab.column('ready_tick'), pa.int64())
    cols['commit_tick'] = _cast(sub_lab.column('commit_tick'), pa.int64())
    cols['mispredicted'] = _cast(sub_lab.column('mispredicted'), pa.int8())
    cols['fetch_latency'] = pa.array(fl_arr, type=pa.int64())
    cols['execution_latency'] = pa.array(el_arr, type=pa.int64())
    cols['is_fetch_group_head'] = pa.array(ih_arr, type=pa.int8())

    schema = make_flat_schema()
    arrays = [cols[f.name] for f in schema]
    return pa.Table.from_arrays(arrays, schema=schema)


# ============================================================ legacy jsonl emit
def emit_legacy_jsonl(workload: str, core_data, picks, out_path: str):
    """旧格式 nested-json jsonl，仅供 --legacy-jsonl-out 调试。"""
    n_emitted = 0
    with open(out_path, 'w') as out_fp:
        # 与列式 emit 顺序一致：按 (cid, tid) 排序后逐条写
        for (cid, tid, gidx, pidx) in picks:
            rec_t = core_data[cid]['rec']
            lab_t = core_data[cid]['lab']
            fl = core_data[cid]['fl']
            el = core_data[cid]['el']
            ih = core_data[cid]['ih']
            sub_rec = rec_t.take(pa.array(gidx)).to_pylist()
            sub_lab = lab_t.take(pa.array(gidx)).to_pylist()
            for j, (r, l) in enumerate(zip(sub_rec, sub_lab)):
                gi = int(gidx[j])
                sample = {
                    "meta": {
                        "workload": workload,
                        "core_id": cid,
                        "thread_id": tid,
                        "micro_seq": r['micro_seq'],
                        "pick_idx": int(pidx[j]),
                    },
                    "input": {
                        "macro_pc": r['macro_pc'],
                        "micro_pc": r['micro_pc'],
                        "vaddr": r['vaddr'],
                        "size": r['size'],
                        "is_load": r['is_load'],
                        "is_store": r['is_store'],
                        "is_atomic": r['is_atomic'],
                        "is_branch": r['is_branch'],
                        "is_branch_cond": r['is_branch_cond'],
                        "is_branch_indirect": r['is_branch_indirect'],
                        "is_call": r['is_call'],
                        "is_return": r['is_return'],
                        "is_int": r['is_int'],
                        "is_fp": r['is_fp'],
                        "is_simd": r['is_simd'],
                        "is_serialize": r['is_serialize'],
                        "is_microop": r['is_microop'],
                        "is_last_microop": r['is_last_microop'],
                        "n_src": r['n_src'],
                        "n_dst": r['n_dst'],
                        "producer_dists": r['producer_dists'],
                        "producer_classes": r['producer_classes'],
                    },
                    "uarch_context": {
                        "seq_num": r['seq_num'],
                        "paddr": r['paddr'],
                        "cacheline_addr": r['cacheline_addr'],
                        # COMPAT-OLD-50M：见 read_core_tables 的 fallback
                        "cacheline_paddr": r['cacheline_paddr'],
                        "mesi_before": r['mesi_before'],
                        "coh_oracle": r['coh_oracle'],
                        "sharer_bucket": r['sharer_bucket'],
                        "owner_dist": r['owner_dist'],
                        "dirty_owner": r['dirty_owner'],
                        "path_class": r['path_class'],
                        "inval_fanout": r['inval_fanout'],
                        "same_line_recent": r['same_line_recent'],
                        "oracle_source": r['oracle_source'],
                        "i_path_class": r['i_path_class'],
                        "i_coh_oracle": r['i_coh_oracle'],
                        "i_mesi_before": r['i_mesi_before'],
                        "i_oracle_source": r['i_oracle_source'],
                        # P0-A
                        "d_mshr_depth": r['d_mshr_depth'],
                        "dtlb_hit": r['dtlb_hit'],
                        "d_walker_levels": r['d_walker_levels'],
                        "d_walker_dram_misses": r['d_walker_dram_misses'],
                        "d_bank_id": r['d_bank_id'],
                        "i_mshr_depth": r['i_mshr_depth'],
                        "itlb_hit": r['itlb_hit'],
                        "i_walker_levels": r['i_walker_levels'],
                        "i_walker_dram_misses": r['i_walker_dram_misses'],
                        "i_bank_id": r['i_bank_id'],
                        # V10.3 A：LLC set residency / lru_pos
                        "d_llc_set_residency": r['d_llc_set_residency'],
                        "d_llc_set_lru_pos":   r['d_llc_set_lru_pos'],
                        "i_llc_set_residency": r['i_llc_set_residency'],
                        "i_llc_set_lru_pos":   r['i_llc_set_lru_pos'],
                    },
                    "labels": {
                        "fetch_tick": l['fetch_tick'],
                        "ready_tick": l['ready_tick'],
                        "commit_tick": l['commit_tick'],
                        "mispredicted": l['mispredicted'],
                        "fetch_latency": int(fl[gi]),
                        "execution_latency": int(el[gi]),
                        "is_fetch_group_head": int(ih[gi]),
                    },
                }
                out_fp.write(json.dumps(sample, separators=(',', ':')))
                out_fp.write('\n')
                n_emitted += 1
    return n_emitted


# ============================================================ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', action='append', required=True,
                    metavar='NAME=DIR',
                    help='workload 名称与 gem5 run 目录映射，可多次指定')
    ap.add_argument('--target', type=int, required=True,
                    help='总目标样本数（如 3000000）')
    ap.add_argument('--out', required=True,
                    help='默认: parquet 文件路径（每 workload 一个 part-000.parquet '
                         '聚合到该文件）。当 --legacy-jsonl-out 启用时，按旧 '
                         'jsonl 路径解释。')
    ap.add_argument('--legacy-jsonl-out', action='store_true', default=False,
                    help='启用后额外按旧 nested-json jsonl 格式写入 --out（仅 debug）')
    ap.add_argument('--head-skip', type=float, default=0.05)
    ap.add_argument('--tail-skip', type=float, default=0.05)
    ap.add_argument('--context-warmup-skip', type=int, default=128,
                    help='每个 (core, thread) 稳定窗额外跳过的 ROI 内前 N 条 µop')
    ap.add_argument('--exclude-cores', default='',
                    help='逗号分隔的 core_id 列表，从采样池剔除（默认空，'
                         '即 core0..3 全部纳入）。'
                         '如旧 V9.x 行为可传 --exclude-cores 0。')
    ap.add_argument('--row-group-size', type=int, default=65536)
    ap.add_argument('--compression', default='zstd')
    ap.add_argument('--compression-level', type=int, default=3)
    args = ap.parse_args()

    runs = []
    for r in args.run:
        if '=' not in r:
            sys.exit(f"--run 必须形如 NAME=DIR，got: {r}")
        name, dir_ = r.split('=', 1)
        runs.append((name, dir_))

    # business_cores: 自动按 run_dir 实际 core 数 - --exclude-cores
    n_cores_runs = [detect_n_cores(d) for _, d in runs]
    n_cores_max = max(n_cores_runs) if n_cores_runs else 4
    excluded = set()
    if args.exclude_cores.strip():
        for tok in args.exclude_cores.split(','):
            tok = tok.strip()
            if tok:
                excluded.add(int(tok))
    business_cores = tuple(c for c in range(n_cores_max) if c not in excluded)
    print(f"[business_cores] n_cores_per_run={n_cores_runs} -> "
          f"universe=range({n_cores_max})  excluded={sorted(excluded)}  "
          f"business_cores={business_cores}", file=sys.stderr)

    # 1) 各 workload 收集稳定窗（pyarrow 列式读取）
    pools = {}        # name -> (core_data, windows)
    capacity = {}     # name -> int
    for name, d in runs:
        core_data, windows = collect_workload(
            d, args.head_skip, args.tail_skip,
            args.context_warmup_skip, business_cores=business_cores)
        cap = sum(len(v) for v in windows.values())
        pools[name] = (core_data, windows)
        capacity[name] = cap
        print(f"[scan] {name:20s} steady-cap = {cap:>12,}", file=sys.stderr)

    total_cap = sum(capacity.values())
    if total_cap < args.target:
        sys.exit(f"steady capacity {total_cap:,} < target {args.target:,}")

    # 2) 每 workload 配额：先均分，cap 不足则将 deficit 按比例分摊到其余
    n = len(runs)
    base = args.target // n
    quotas = {}
    deficit = 0
    rich = []
    for name, _ in runs:
        if capacity[name] <= base:
            quotas[name] = capacity[name]
            deficit += base - capacity[name]
        else:
            quotas[name] = base
            rich.append(name)
    rem = args.target - sum(quotas.values()) - deficit
    if rem > 0 and rich:
        quotas[rich[0]] += rem
    while deficit > 0 and rich:
        rich_left = {n_: capacity[n_] - quotas[n_] for n_ in rich}
        total_left = sum(rich_left.values())
        if total_left <= 0:
            break
        new_rich = []
        for n_ in rich:
            share = int(round(deficit * rich_left[n_] / total_left))
            give = min(share, rich_left[n_])
            quotas[n_] += give
            deficit -= give
            if capacity[n_] > quotas[n_]:
                new_rich.append(n_)
        rich = new_rich
    delta = args.target - sum(quotas.values())
    for n_ in (rich + [r[0] for r in runs]):
        if delta == 0:
            break
        if capacity[n_] - quotas[n_] >= delta:
            quotas[n_] += delta
            delta = 0
    print(file=sys.stderr)
    for name, _ in runs:
        print(f"[quota] {name:20s} {quotas[name]:>10,} / {capacity[name]:>12,}",
              file=sys.stderr)
    print(f"[quota] {'TOTAL':20s} {sum(quotas.values()):>10,} / {args.target:,}",
          file=sys.stderr)

    # 3) 每 workload 内按 (core, tid) 容量比例分摊配额，stride 选样
    coverage = defaultdict(int)
    n_emitted_total = 0

    if args.legacy_jsonl_out:
        # 单 .jsonl 聚合所有 workload
        os.makedirs(os.path.dirname(args.out) or '.', exist_ok=True)
        # 清空 / 重写
        with open(args.out, 'w'):
            pass
    else:
        # parquet：单文件路径 args.out
        os.makedirs(os.path.dirname(args.out) or '.', exist_ok=True)

    writer = None
    schema = make_flat_schema()
    if not args.legacy_jsonl_out:
        writer = pq.ParquetWriter(
            args.out, schema,
            compression=args.compression,
            compression_level=args.compression_level,
            use_dictionary=True,
            data_page_size=1 << 20,
            write_statistics=True,
        )

    try:
        for name, _ in runs:
            quota = quotas[name]
            if quota == 0:
                continue
            core_data, windows = pools[name]
            keys = sorted(windows.keys())
            sub_cap = {k: len(windows[k]) for k in keys}
            wcap = sum(sub_cap.values())
            sub_quota = {}
            assigned = 0
            for k in keys[:-1]:
                q = int(quota * sub_cap[k] / wcap) if wcap > 0 else 0
                q = min(q, sub_cap[k])
                sub_quota[k] = q
                assigned += q
            if keys:
                last = keys[-1]
                sub_quota[last] = min(quota - assigned, sub_cap[last])

            picks = []   # (cid, tid, global_idx_arr, pick_idx_arr)
            for k in keys:
                w = windows[k]
                offsets = stride_pick_offsets(len(w), sub_quota.get(k, 0))
                if len(offsets) == 0:
                    continue
                gidx = w[offsets]
                pidx = np.arange(len(gidx), dtype=np.int32)
                picks.append((k[0], k[1], gidx, pidx))
                coverage[name] += len(gidx)

            if args.legacy_jsonl_out:
                with open(args.out, 'a') as _:
                    pass
                n = emit_legacy_jsonl(name, core_data, picks, args.out + '.tmp')
                # 追加合并
                with open(args.out + '.tmp') as src, open(args.out, 'a') as dst:
                    for ln in src:
                        dst.write(ln)
                os.unlink(args.out + '.tmp')
                n_emitted_total += n
            else:
                table = assemble_flat_table(name, core_data, picks)
                writer.write_table(table, row_group_size=args.row_group_size)
                n_emitted_total += table.num_rows

            print(f"[emit] {name:20s} rows={coverage[name]:>10,}",
                  file=sys.stderr)

            # 单 workload 处理完后释放本 workload 的 core_data
            pools[name] = None

    finally:
        if writer is not None:
            writer.close()

    print(file=sys.stderr)
    print(f"=== emitted total = {n_emitted_total:,} -> {args.out} "
          f"({'jsonl' if args.legacy_jsonl_out else 'parquet'}) ===",
          file=sys.stderr)
    for name, _ in runs:
        print(f"  {name:20s} {coverage[name]:>10,}", file=sys.stderr)


if __name__ == '__main__':
    main()
