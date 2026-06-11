#!/usr/bin/env python3
"""把 samples_3m.jsonl 或 sample 阶段产出的 flat parquet 转为列式数据集。

设计要点（与 ml/dataset.py 协议绑定）：
  1) Hive 分区：workload=<NAME>/part-000.parquet，每 workload 一个文件。
  2) row_group_size = 65536，配合 zstd-3 + dict encoding，开销最小。
  3) 严格保持原 jsonl 内 (core_id, thread_id, micro_seq) 升序，行内 pos_in_thread
     字段为 thread 内的 0-based 行号；训练时取上下文窗口直接按 pos 计算。
  4) producer_dists / producer_classes 展平为 d0..d3 / pc0..pc3 8 个 int32 列。
  5) macro_pc / micro_pc / vaddr / paddr / cacheline_addr 保留为 uint64 用于
     位移特征；额外预生成 macro_pc_id 整数 token（小词表，<2^20），训练用。
  6) 同步生成 vocab.json + meta.json，训练侧直接消费。

入口 (二选一)：
  --in-jsonl PATH       旧入口：流式读旧 nested-json jsonl（兼容 50M 数据集）
  --from-parquet PATH   方案 B 新入口：直接读 sample_steady_balanced.py 产出的
                        flat parquet（跳过 json 解析）
"""
import argparse
import json
import os
import sys
from collections import defaultdict, OrderedDict

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from latency_report import build_latency_report, print_latency_report
from latency_units import latency_unit_metadata, load_uarch_profile


SCALAR_BOOL = (
    'is_load', 'is_store', 'is_atomic', 'is_branch', 'is_branch_cond',
    'is_branch_indirect', 'is_call', 'is_return', 'is_int', 'is_fp',
    'is_simd', 'is_serialize', 'is_microop', 'is_last_microop',
)
SCALAR_SMALL_INT = (
    'n_src', 'n_dst', 'size',
    'mesi_before', 'coh_oracle', 'sharer_bucket', 'owner_dist',
    'dirty_owner', 'path_class', 'inval_fanout', 'same_line_recent',
    'oracle_source',
    'i_path_class', 'i_coh_oracle', 'i_mesi_before', 'i_oracle_source',
    # P0-A：d-side / i-side 各 5 个 oracle bit-exact 镜像列
    #   缺失（旧 50M 数据）时 fallback 到 0；schema 列恒存在
    'd_mshr_depth', 'dtlb_hit', 'd_walker_levels',
    'd_walker_dram_misses', 'd_bank_id',
    'i_mshr_depth', 'itlb_hit', 'i_walker_levels',
    'i_walker_dram_misses', 'i_bank_id',
    # V10.3 A 字段：LLC set residency / lru_pos（d/i 各 2 列）。
    #   ref_sim 与 gem5 oracle 通过 BankedSetAssocLRU::peekSetState 同源 bit-exact。
    #   缺失（V10.2 数据）时 fallback 到 0；schema 列恒存在。
    'd_llc_set_residency', 'd_llc_set_lru_pos',
    'i_llc_set_residency', 'i_llc_set_lru_pos',
)
# P1-C 上下文窗口派生：纯离线、严格因果（仅看当前行之前的 W 行），
#   按 (core_id, thread_id) 分组聚合；当前行不计入窗口。
#   - 计数类（_W64）：clip 0..64，存 int16
#   - log 距离类（_log）：clip 0..15，存 int16（没有先例时填 15）
P1C_CTX = (
    'mem_density_W64',           # 窗口内 (is_load|is_store|is_atomic) 数
    'branch_density_W64',        # 窗口内 is_branch 数
    'unique_cl_W64',             # 窗口内 cacheline_paddr 唯一数
    'cl_reuse_dist_log',         # 距上次同 cacheline_paddr 的距离 log2
    'pc_freq_W64',               # 窗口内 macro_pc 等于当前的次数
    'time_since_last_branch_log',# 距上次 is_branch 的距离 log2
    'bank_conflict_W64',         # 窗口内 d_bank_id == 当前 且 mem-touching 的次数
)
P1C_WINDOW = 64

# V10.3 B 字段：长窗口 unique_cl 派生（解决 W13 graph_walk 工作集失效）。
#   - unique_cl_W256：W64 → W256，cap 在 int16 容量内（255 已足够区分稀/密）
#   - unique_cl_W1024：W64 → W1024，cap 2047（int16 容量内）
#   纯离线、严格因果，与 P1C_CTX 同时计算（共享 cl_count 滑窗）。
P1C_LONG_WINDOWS = (256, 1024)
P1C_LONG_CAPS = {256: 255, 1024: 2047}

# V10.3 C 字段：DRAM 简化派生（地址公式 + 滑窗 W256）。
#   - dram_bank_id：(paddr >> 6) & (banks_per_channel - 1)，cap 0..15
#   - dram_bank_freq_W256：W256 内 dram_bank_id == 当前 的次数，cap 255
#   - dram_row_freq_W256：W256 内 row_id == 当前 的次数，cap 255
#       row_id = paddr / row_size_b（默认 8KB）
#   bank/row 公式与 SingleChannelDDR4_2400 默认一致；profile.dram 段与
#   args.mem_size 同源。所有派生在 packer 离线计算，ref_sim/gem5 oracle 不参与。
P1C_DRAM = ('dram_bank_id', 'dram_bank_freq_W256', 'dram_row_freq_W256')
P1C_DRAM_WINDOW = 256
P1C_DRAM_CAP = 255
# V10 方案 B：cacheline_paddr 是 paddr-line 真值；旧 50M raw 不含该字段，
# 在 pass1 流式读取时通过 inp.get/uc.get 自动 fallback 到 cacheline_addr
# (vaddr-line) 或 0，schema 列始终存在但旧数据上等同于 cacheline_addr 的桶。
# COMPAT-OLD-50M: 全 V10+ 重采后无需特殊 fallback，但 schema 仍保留该列。
SCALAR_U64 = ('macro_pc', 'micro_pc', 'vaddr', 'paddr',
              'cacheline_addr', 'cacheline_paddr')
FETCH_AUX_LABELS = (
    ('fetch_base_latency', pa.float64()),
    ('fetch_after_mispred_latency', pa.float64()),
    ('fetch_residual_tail_latency', pa.float64()),
    ('fetch_after_mispred_k4', pa.int8()),
)


def make_schema():
    f = []
    f.append(('core_id', pa.int8()))
    f.append(('thread_id', pa.int16()))
    f.append(('micro_seq', pa.int64()))
    f.append(('pos_in_thread', pa.int32()))
    for k in SCALAR_BOOL:
        f.append((k, pa.int8()))
    for k in SCALAR_SMALL_INT:
        f.append((k, pa.int16()))
    for k in SCALAR_U64:
        f.append((k, pa.uint64()))
    for i in range(4):
        f.append((f'd{i}', pa.int32()))
        # pc 取值 0~6 + sentinel 255（"无生产者"），需要无符号或 int16
        f.append((f'pc{i}', pa.int16()))
    f.append(('macro_pc_id', pa.int32()))
    # P1-C：上下文窗口派生（packer 在 sort 之后计算，不依赖 ML/online context）
    for k in P1C_CTX:
        f.append((k, pa.int16()))
    # V10.3 B：长窗口 unique_cl（W256/W1024）
    for w in P1C_LONG_WINDOWS:
        f.append((f'unique_cl_W{w}', pa.int16()))
    # V10.3 C：DRAM 简化派生
    for k in P1C_DRAM:
        f.append((k, pa.int16()))
    # labels
    f.append(('fetch_tick', pa.int64()))
    f.append(('ready_tick', pa.int64()))
    f.append(('commit_tick', pa.int64()))
    f.append(('mispredicted', pa.int8()))
    f.append(('fetch_latency', pa.float64()))
    f.append(('execution_latency', pa.float64()))
    # V9.7 方案 B：fetch group head 辅助 label（detailed-only，推理时丢弃）
    f.append(('is_fetch_group_head', pa.int8()))
    # V10.5：functional trace 可复现的 fetch candidate group。
    # 非 candidate row 在训练/推理中强制 fetch_latency=0。
    f.append(('candidate_fetch_group_head', pa.int8()))
    f.append(('candidate_fetch_latency', pa.float64()))
    # Fetch 三段式辅助监督 label；推理主 clock 仍消费 fetch_latency。
    for name, typ in FETCH_AUX_LABELS:
        f.append((name, typ))
    return pa.schema([pa.field(n, t) for n, t in f])


# ============================================================ 旧入口 (jsonl)
def load_from_jsonl(in_jsonl, names):
    """流式读旧 nested-json jsonl。
    返回 (by_w_cols, macro_pc_vocab, n_total)。
    """
    print('[pass1] streaming jsonl -> per-workload columnar buffers ...',
          file=sys.stderr)
    by_w_cols = {}
    macro_pc_vocab = OrderedDict()
    # COMPAT-OLD-50M：缺失 V10 新字段时全程只 warn 一次。
    compat_warn = {'cacheline_paddr': False}

    n_total = 0
    with open(in_jsonl) as f:
        for ln in f:
            s = json.loads(ln)
            m, inp, uc, lb = s['meta'], s['input'], s['uarch_context'], s['labels']
            w = m['workload']
            if w not in by_w_cols:
                by_w_cols[w] = {n: [] for n in names}
            cols = by_w_cols[w]

            mpc = inp['macro_pc']
            if mpc not in macro_pc_vocab:
                macro_pc_vocab[mpc] = len(macro_pc_vocab)
            mpc_id = macro_pc_vocab[mpc]

            cols['core_id'].append(int(m['core_id']))
            cols['thread_id'].append(int(m['thread_id']))
            cols['micro_seq'].append(int(m['micro_seq']))
            cols['pos_in_thread'].append(0)              # 占位，下一阶段重写
            cols['macro_pc_id'].append(mpc_id)
            for k in SCALAR_BOOL:
                cols[k].append(int(inp[k]))
            for k in SCALAR_SMALL_INT:
                v = inp.get(k, uc.get(k, 0))
                cols[k].append(int(v))
            for k in SCALAR_U64:
                v = inp.get(k, uc.get(k, None))
                # COMPAT-OLD-50M: 旧 jsonl 的 uarch_context 不含 cacheline_paddr
                # 时，回退到 cacheline_addr（vaddr-line），让下游 cline_p_bucket
                # 与 cline_bucket 同桶；其他 SCALAR_U64 缺失仍按 0。全 V10+ 后可删。
                if v is None:
                    if k == 'cacheline_paddr':
                        if not compat_warn['cacheline_paddr']:
                            print(f"[pack_to_parquet][COMPAT-OLD-50M] "
                                  f"cacheline_paddr 缺失 -> fallback cacheline_addr "
                                  f"(workload={w})", file=sys.stderr)
                            compat_warn['cacheline_paddr'] = True
                        v = inp.get('cacheline_addr',
                                    uc.get('cacheline_addr', 0))
                    else:
                        v = 0
                cols[k].append(int(v) & ((1 << 64) - 1))
            pds = inp['producer_dists']
            pcs = inp['producer_classes']
            for i in range(4):
                cols[f'd{i}'].append(int(pds[i]) if i < len(pds) else -1)
                cols[f'pc{i}'].append(int(pcs[i]) if i < len(pcs) else -1)
            cols['fetch_tick'].append(int(lb['fetch_tick']))
            cols['ready_tick'].append(int(lb['ready_tick']))
            cols['commit_tick'].append(int(lb['commit_tick']))
            cols['mispredicted'].append(int(lb['mispredicted']))
            cols['fetch_latency'].append(float(lb['fetch_latency']))
            cols['execution_latency'].append(float(lb['execution_latency']))
            cols['is_fetch_group_head'].append(
                int(lb.get('is_fetch_group_head', 0)))
            cols['candidate_fetch_group_head'].append(
                int(lb.get('candidate_fetch_group_head', 0)))
            cols['candidate_fetch_latency'].append(float(
                lb.get('candidate_fetch_latency', 0.0)))
            cols['fetch_base_latency'].append(float(
                lb.get('fetch_base_latency', lb['fetch_latency'])))
            cols['fetch_after_mispred_latency'].append(float(
                lb.get('fetch_after_mispred_latency', 0.0)))
            cols['fetch_residual_tail_latency'].append(float(
                lb.get('fetch_residual_tail_latency', 0.0)))
            cols['fetch_after_mispred_k4'].append(int(
                lb.get('fetch_after_mispred_k4', 0)))

            n_total += 1
            if n_total % 500000 == 0:
                print(f'  read {n_total:,}', file=sys.stderr)

    print(f'[pass1] total = {n_total:,}, workloads = {len(by_w_cols)}',
          file=sys.stderr)
    print(f'[pass1] macro_pc unique = {len(macro_pc_vocab):,}', file=sys.stderr)
    return by_w_cols, macro_pc_vocab, n_total


# ============================================================ 新入口 (--from-parquet)
def load_from_flat_parquet(in_parquet, names):
    """读 sample_steady_balanced.py 输出的 flat parquet，
    构造与 jsonl 入口等价的 by_w_cols 结构。
    flat parquet 每行字段已与 nested jsonl 一一对应（详见 sample 阶段
    make_flat_schema）。

    本函数采用半向量化策略：把整张 table 一次性读进 numpy，
    再按 workload 分组写入 by_w_cols（避免逐行 json.loads）。

    返回 (by_w_cols, macro_pc_vocab, n_total)。
    """
    print(f'[pass1] reading flat parquet {in_parquet} ...', file=sys.stderr)
    table = pq.read_table(in_parquet)
    n_total = table.num_rows
    print(f'[pass1] total rows = {n_total:,}', file=sys.stderr)
    cols_in = {nm: table.column(nm) for nm in table.schema.names}
    sch_names = set(table.schema.names)

    # COMPAT-OLD-50M：cacheline_paddr 缺失时回退 cacheline_addr
    if 'cacheline_paddr' not in sch_names:
        print('[pack_to_parquet][COMPAT-OLD-50M] '
              'cacheline_paddr 缺失 -> fallback cacheline_addr',
              file=sys.stderr)
        cols_in['cacheline_paddr'] = cols_in['cacheline_addr']

    # 拉成 numpy
    def npy(name, dtype=None):
        a = cols_in[name].to_numpy(zero_copy_only=False)
        if dtype is not None and a.dtype != dtype:
            a = a.astype(dtype)
        return a

    workload_arr = npy('workload')
    cid_arr = npy('core_id', np.int64)
    tid_arr = npy('thread_id', np.int64)
    micro_seq_arr = npy('micro_seq', np.int64)
    macro_pc_arr = npy('macro_pc', np.uint64)

    # macro_pc vocab：按行序构造（保持 deterministic）
    macro_pc_vocab = OrderedDict()
    mpc_id_arr = np.empty(n_total, dtype=np.int32)
    # 用快速 dict 路径，但 mpc 是 uint64
    for i, v in enumerate(macro_pc_arr.tolist()):
        if v not in macro_pc_vocab:
            macro_pc_vocab[v] = len(macro_pc_vocab)
        mpc_id_arr[i] = macro_pc_vocab[v]

    # 按 workload 分组 mask
    unique_w = []
    seen = set()
    for w in workload_arr.tolist():
        if w not in seen:
            seen.add(w)
            unique_w.append(w)

    by_w_cols = {}
    # 预读 numpy
    np_pool = {
        'core_id': cid_arr,
        'thread_id': tid_arr,
        'micro_seq': micro_seq_arr,
        'macro_pc_id': mpc_id_arr,
    }
    for k in SCALAR_BOOL:
        np_pool[k] = npy(k, np.int64)
    for k in SCALAR_SMALL_INT:
        if k in sch_names:
            np_pool[k] = npy(k, np.int64)
        else:
            # COMPAT-OLD：旧 flat parquet 不含 P0-A 10 字段时 fallback 全 0
            np_pool[k] = np.zeros(n_total, dtype=np.int64)
    for k in SCALAR_U64:
        np_pool[k] = npy(k, np.uint64) if k != 'macro_pc' else macro_pc_arr
    np_pool['fetch_tick'] = npy('fetch_tick', np.int64)
    np_pool['ready_tick'] = npy('ready_tick', np.int64)
    np_pool['commit_tick'] = npy('commit_tick', np.int64)
    np_pool['mispredicted'] = npy('mispredicted', np.int64)
    np_pool['fetch_latency'] = npy('fetch_latency', np.float64)
    np_pool['execution_latency'] = npy('execution_latency', np.float64)
    np_pool['is_fetch_group_head'] = npy('is_fetch_group_head', np.int64)
    np_pool['candidate_fetch_group_head'] = (
        npy('candidate_fetch_group_head', np.int64)
        if 'candidate_fetch_group_head' in sch_names
        else np.zeros(n_total, dtype=np.int64)
    )
    np_pool['candidate_fetch_latency'] = (
        npy('candidate_fetch_latency', np.float64)
        if 'candidate_fetch_latency' in sch_names
        else np.zeros(n_total, dtype=np.float64)
    )
    if all(name in sch_names for name, _ in FETCH_AUX_LABELS):
        np_pool['fetch_base_latency'] = npy('fetch_base_latency', np.float64)
        np_pool['fetch_after_mispred_latency'] = npy(
            'fetch_after_mispred_latency', np.float64)
        np_pool['fetch_residual_tail_latency'] = npy(
            'fetch_residual_tail_latency', np.float64)
        np_pool['fetch_after_mispred_k4'] = npy('fetch_after_mispred_k4', np.int64)
    else:
        np_pool['fetch_base_latency'] = np_pool['fetch_latency']
        np_pool['fetch_after_mispred_latency'] = np.zeros(n_total, dtype=np.float64)
        np_pool['fetch_residual_tail_latency'] = np.zeros(n_total, dtype=np.float64)
        np_pool['fetch_after_mispred_k4'] = np.zeros(n_total, dtype=np.int64)

    # producer_dists / producer_classes -> d0..d3 / pc0..pc3
    pds_chunks = cols_in['producer_dists'].chunks if hasattr(cols_in['producer_dists'], 'chunks') else [cols_in['producer_dists']]
    pcs_chunks = cols_in['producer_classes'].chunks if hasattr(cols_in['producer_classes'], 'chunks') else [cols_in['producer_classes']]
    pds_lists = []
    for ch in pds_chunks:
        pds_lists.extend(ch.to_pylist())
    pcs_lists = []
    for ch in pcs_chunks:
        pcs_lists.extend(ch.to_pylist())
    d_arrs = [np.empty(n_total, dtype=np.int32) for _ in range(4)]
    pc_arrs = [np.empty(n_total, dtype=np.int32) for _ in range(4)]
    for i in range(n_total):
        pds = pds_lists[i] or []
        pcs = pcs_lists[i] or []
        for k in range(4):
            d_arrs[k][i] = int(pds[k]) if k < len(pds) else -1
            pc_arrs[k][i] = int(pcs[k]) if k < len(pcs) else -1
    for k in range(4):
        np_pool[f'd{k}'] = d_arrs[k]
        np_pool[f'pc{k}'] = pc_arrs[k]

    for w in unique_w:
        mask = (workload_arr == w)
        cols = {}
        for nm in names:
            if nm == 'pos_in_thread':
                cols[nm] = np.zeros(mask.sum(), dtype=np.int32)
            elif nm in P1C_CTX:
                # P1-C 列在 main() 里 sort 之后派生，这里占位即可
                cols[nm] = None
            elif nm.startswith('unique_cl_W') and nm != 'unique_cl_W64':
                # V10.3 B 派生列（W256/W1024）；占位
                cols[nm] = None
            elif nm in P1C_DRAM:
                # V10.3 C 派生列；占位
                cols[nm] = None
            else:
                cols[nm] = np_pool[nm][mask]
        by_w_cols[w] = cols
        print(f'  workload {w:20s} rows={mask.sum():>10,}', file=sys.stderr)

    print(f'[pass1] total = {n_total:,}, workloads = {len(by_w_cols)}',
          file=sys.stderr)
    print(f'[pass1] macro_pc unique = {len(macro_pc_vocab):,}',
          file=sys.stderr)
    return by_w_cols, macro_pc_vocab, n_total


# ============================================================ P1-C 派生
def derive_p1c_window(sorted_cid, sorted_tid, sorted_cols, n,
                      window=P1C_WINDOW):
    """按 (cid, tid) 分组、严格因果窗口聚合。返回 dict[name] -> int16 array。

    实现要点：
      - 单遍扫描 + 每 thread 内一个滑动窗口 + 一组 dict counter；
      - 当前行不计入窗口（先读出聚合，再 push 进窗口）；
      - 窗口跨 (cid,tid) 边界自动重置；
      - 计数列 clip 到 0..32767 之内（int16 上限）；
      - log 距离 = floor(log2(d)) clip 到 0..15；无先例填 15。
    """
    from math import log2

    is_load   = sorted_cols['is_load']
    is_store  = sorted_cols['is_store']
    is_atomic = sorted_cols['is_atomic']
    is_branch = sorted_cols['is_branch']
    cl_paddr  = sorted_cols['cacheline_paddr']
    macro_pc  = sorted_cols['macro_pc']
    d_bank_id = sorted_cols['d_bank_id']
    mem_mask_full = (is_load | is_store | is_atomic).astype(np.bool_)

    out = {k: np.zeros(n, dtype=np.int16) for k in P1C_CTX}

    from collections import deque, defaultdict
    win_mem    = 0
    win_branch = 0
    win_bank   = 0  # bank-conflict 计入：mem-touching 且 d_bank_id == cur
    cl_count   = defaultdict(int)
    pc_count   = defaultdict(int)
    bank_count = defaultdict(int)
    last_cl_pos = {}      # cl_paddr -> last seen idx (within thread)
    last_branch_pos = -1
    dq = deque()           # holds tuples (idx, mem, branch, cl, pc, bank, mem_for_bank)

    prev_cid = -1
    prev_tid = -1
    for i in range(n):
        cid_i = int(sorted_cid[i])
        tid_i = int(sorted_tid[i])
        if cid_i != prev_cid or tid_i != prev_tid:
            # reset thread state
            dq.clear()
            cl_count.clear()
            pc_count.clear()
            bank_count.clear()
            last_cl_pos.clear()
            win_mem = 0
            win_branch = 0
            win_bank = 0
            last_branch_pos = -1
            prev_cid = cid_i
            prev_tid = tid_i

        cur_cl = int(cl_paddr[i])
        cur_pc = int(macro_pc[i])
        cur_bk = int(d_bank_id[i])
        cur_mem = bool(mem_mask_full[i])

        # 1) read aggregates BEFORE pushing current
        out['mem_density_W64'][i]    = min(win_mem,    32767)
        out['branch_density_W64'][i] = min(win_branch, 32767)
        out['unique_cl_W64'][i]      = min(len(cl_count), 32767)
        out['pc_freq_W64'][i]        = min(pc_count.get(cur_pc, 0), 32767)
        # bank_conflict_W64：窗口内 d_bank_id == cur 且本身是 mem-touching
        if cur_mem:
            out['bank_conflict_W64'][i] = min(bank_count.get(cur_bk, 0),
                                              32767)
        else:
            out['bank_conflict_W64'][i] = 0

        if cur_mem and cur_cl in last_cl_pos:
            d = i - last_cl_pos[cur_cl]
            v = int(log2(d)) if d > 0 else 0
            out['cl_reuse_dist_log'][i] = max(0, min(v, 15))
        else:
            out['cl_reuse_dist_log'][i] = 15

        if last_branch_pos >= 0:
            d = i - last_branch_pos
            v = int(log2(d)) if d > 0 else 0
            out['time_since_last_branch_log'][i] = max(0, min(v, 15))
        else:
            out['time_since_last_branch_log'][i] = 15

        # 2) push current into window
        is_mem_i = cur_mem
        is_br_i  = bool(is_branch[i])
        dq.append((i, is_mem_i, is_br_i, cur_cl, cur_pc, cur_bk, is_mem_i))
        if is_mem_i:
            win_mem += 1
            cl_count[cur_cl] = cl_count.get(cur_cl, 0) + 1
            last_cl_pos[cur_cl] = i
            bank_count[cur_bk] = bank_count.get(cur_bk, 0) + 1
        if is_br_i:
            win_branch += 1
            last_branch_pos = i
        pc_count[cur_pc] = pc_count.get(cur_pc, 0) + 1

        # 3) evict tail items beyond window
        while len(dq) > window:
            (_, evm, evb, evcl, evpc, evbk, _) = dq.popleft()
            if evm:
                win_mem -= 1
                c = cl_count[evcl] - 1
                if c <= 0:
                    del cl_count[evcl]
                else:
                    cl_count[evcl] = c
                cb = bank_count[evbk] - 1
                if cb <= 0:
                    del bank_count[evbk]
                else:
                    bank_count[evbk] = cb
            if evb:
                win_branch -= 1
            pp = pc_count[evpc] - 1
            if pp <= 0:
                del pc_count[evpc]
            else:
                pc_count[evpc] = pp

    return out


# ============================================================ V10.3 B+C 派生
def derive_v10_3_window(sorted_cid, sorted_tid, sorted_cols, n,
                        dram_cfg):
    """V10.3 B + C 字段离线派生（与 derive_p1c_window 同源滑窗思路）。

    返回 dict[name] -> int16 array：
      B: unique_cl_W256, unique_cl_W1024
      C: dram_bank_id, dram_bank_freq_W256, dram_row_freq_W256

    严格因果：当前行 cl/bank/row 不计入窗口（先读出聚合，再 push 进窗口）。
    跨 (cid,tid) 边界自动重置。

    dram_cfg 形如 {'banks_per_channel': 16, 'row_size_b': 8192}。
    """
    from collections import deque, defaultdict

    cl_paddr = sorted_cols['cacheline_paddr']
    paddr    = sorted_cols['paddr']
    is_load  = sorted_cols['is_load']
    is_store = sorted_cols['is_store']
    is_atomic = sorted_cols['is_atomic']
    mem_mask = (is_load | is_store | is_atomic).astype(np.bool_)

    banks   = int(dram_cfg.get('banks_per_channel', 16))
    row_b   = int(dram_cfg.get('row_size_b', 8192))
    bank_mask = banks - 1  # 假设 banks 为 2 的幂（16 默认）
    # 防御：如果 banks 不是 2^n，退化为模
    use_mod = (banks & bank_mask) != 0

    out = {f'unique_cl_W{w}': np.zeros(n, dtype=np.int16)
           for w in P1C_LONG_WINDOWS}
    out['dram_bank_id']         = np.zeros(n, dtype=np.int16)
    out['dram_bank_freq_W256']  = np.zeros(n, dtype=np.int16)
    out['dram_row_freq_W256']   = np.zeros(n, dtype=np.int16)

    # 长窗口 unique_cl 各自一个 deque + counter
    cl_dqs   = {w: deque() for w in P1C_LONG_WINDOWS}
    cl_cnts  = {w: defaultdict(int) for w in P1C_LONG_WINDOWS}
    # DRAM W256：共享一个窗口（bank/row 同步进出）
    dram_dq    = deque()
    bank_cnt   = defaultdict(int)
    row_cnt    = defaultdict(int)

    prev_cid = -1
    prev_tid = -1
    for i in range(n):
        cid_i = int(sorted_cid[i])
        tid_i = int(sorted_tid[i])
        if cid_i != prev_cid or tid_i != prev_tid:
            for w in P1C_LONG_WINDOWS:
                cl_dqs[w].clear()
                cl_cnts[w].clear()
            dram_dq.clear()
            bank_cnt.clear()
            row_cnt.clear()
            prev_cid = cid_i
            prev_tid = tid_i

        cur_mem = bool(mem_mask[i])
        cur_cl  = int(cl_paddr[i])
        cur_pa  = int(paddr[i])
        cur_bank = (cur_pa >> 6) % banks if use_mod else (cur_pa >> 6) & bank_mask
        cur_row  = cur_pa // row_b if row_b > 0 else 0

        # 1) 读 aggregate（current 不计入）
        for w in P1C_LONG_WINDOWS:
            cap = P1C_LONG_CAPS[w]
            v = len(cl_cnts[w])
            out[f'unique_cl_W{w}'][i] = v if v < cap else cap

        # dram_bank_id 永远写当前值（cap 0..15）
        bid = cur_bank if cur_bank < 16 else 15
        out['dram_bank_id'][i] = bid

        if cur_mem:
            bf = bank_cnt.get(cur_bank, 0)
            rf = row_cnt.get(cur_row, 0)
            out['dram_bank_freq_W256'][i] = bf if bf < P1C_DRAM_CAP else P1C_DRAM_CAP
            out['dram_row_freq_W256'][i]  = rf if rf < P1C_DRAM_CAP else P1C_DRAM_CAP
        else:
            out['dram_bank_freq_W256'][i] = 0
            out['dram_row_freq_W256'][i]  = 0

        # 2) push current（仅 mem-touching 行计入 cl/bank/row）
        if cur_mem:
            for w in P1C_LONG_WINDOWS:
                cl_dqs[w].append((i, cur_cl))
                cl_cnts[w][cur_cl] += 1
            dram_dq.append((i, cur_bank, cur_row))
            bank_cnt[cur_bank] = bank_cnt.get(cur_bank, 0) + 1
            row_cnt[cur_row]   = row_cnt.get(cur_row, 0) + 1

        # 3) evict beyond window
        for w in P1C_LONG_WINDOWS:
            while len(cl_dqs[w]) > w:
                _, evcl = cl_dqs[w].popleft()
                c = cl_cnts[w][evcl] - 1
                if c <= 0:
                    del cl_cnts[w][evcl]
                else:
                    cl_cnts[w][evcl] = c
        while len(dram_dq) > P1C_DRAM_WINDOW:
            _, evbk, evrw = dram_dq.popleft()
            cb = bank_cnt[evbk] - 1
            if cb <= 0:
                del bank_cnt[evbk]
            else:
                bank_cnt[evbk] = cb
            cr = row_cnt[evrw] - 1
            if cr <= 0:
                del row_cnt[evrw]
            else:
                row_cnt[evrw] = cr

    return out


def derive_candidate_fetch_groups(sorted_cid, sorted_tid, sorted_cols, fetch_lat):
    n = len(sorted_cid)
    out = np.zeros(n, dtype=np.int8)
    cand_lat = np.zeros(n, dtype=np.float64)
    macro_pc = np.asarray(sorted_cols['macro_pc'], dtype=np.uint64)
    is_micro = np.asarray(sorted_cols['is_microop'], dtype=np.int64)
    is_last = np.asarray(sorted_cols['is_last_microop'], dtype=np.int64)
    is_branch = np.asarray(sorted_cols['is_branch'], dtype=np.int64)
    is_serialize = np.asarray(sorted_cols['is_serialize'], dtype=np.int64)
    fetch_lat = np.maximum(np.asarray(fetch_lat, dtype=np.float64), 0.0)
    prev_macro = None
    prev_i_cl = None
    prev_is_micro = 0
    prev_is_last = 0
    prev_key = None
    carry = 0.0
    for i in range(n):
        key = (int(sorted_cid[i]), int(sorted_tid[i]))
        if key != prev_key:
            prev_macro = None
            prev_i_cl = None
            prev_is_micro = 0
            prev_is_last = 0
            prev_key = key
            carry = 0.0
        cur_macro = int(macro_pc[i])
        cur_i_cl = cur_macro >> 6
        macro_head = (prev_macro is None or prev_is_micro == 0
                      or prev_is_last == 1 or cur_macro != prev_macro)
        i_group_head = (prev_i_cl is None or cur_i_cl != prev_i_cl)
        out[i] = int(macro_head or i_group_head
                     or int(is_branch[i]) > 0
                     or int(is_serialize[i]) > 0)
        if out[i] > 0:
            cand_lat[i] = carry + fetch_lat[i]
            carry = 0.0
        else:
            carry += fetch_lat[i]
        prev_macro = cur_macro
        prev_i_cl = cur_i_cl
        prev_is_micro = int(is_micro[i])
        prev_is_last = int(is_last[i])
    return out, cand_lat


# ============================================================ main
def main():
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument('--in-jsonl', help='旧入口：nested-json jsonl 文件')
    src.add_argument('--from-parquet',
                     help='方案 B 新入口：sample_steady_balanced.py 产出的 '
                          'flat parquet')
    ap.add_argument('--out-dir', required=True)
    ap.add_argument('--row-group-size', type=int, default=65536)
    ap.add_argument('--compression', default='zstd')
    ap.add_argument('--compression-level', type=int, default=3)
    # V10.3 C：DRAM 派生需要 banks_per_channel / row_size_b。
    #   V10.3 强制：必须提供 --uarch-profile，profile.dram 段是 DRAM C
    #   派生列的唯一权威源；缺省或字段缺失会直接 sys.exit(1) 报错。
    ap.add_argument('--uarch-profile', default=None,
                    help='V10.3 必填：uarch_profile.json 路径，packer 会读 '
                         'profile.dram.{banks_per_channel,row_size_b}；'
                         '缺省或缺字段会 FATAL 退出，杜绝训推 dram_cfg 不一致。')
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)

    # V10.3 C：解析 dram_cfg
    #   强制要求 --uarch-profile 提供 dram 段，杜绝默认值误用导致
    #   训推 dram_cfg 不一致 / DRAM C 派生列偏移。
    if not args.uarch_profile:
        print('[pack_to_parquet][FATAL] --uarch-profile is required '
              '(V10.3 强制走 profile，禁止 fallback 默认 dram_cfg)',
              file=sys.stderr)
        sys.exit(1)
    if not os.path.exists(args.uarch_profile):
        print(f'[pack_to_parquet][FATAL] uarch_profile not found: '
              f'{args.uarch_profile}', file=sys.stderr)
        sys.exit(1)
    try:
        _prof = load_uarch_profile(args.uarch_profile)
    except Exception as e:
        print(f'[pack_to_parquet][FATAL] failed to parse uarch_profile '
              f'{args.uarch_profile}: {e}', file=sys.stderr)
        sys.exit(1)
    _d = _prof.get('dram') or {}
    if not _d.get('banks_per_channel') or not _d.get('row_size_b'):
        print(f'[pack_to_parquet][FATAL] uarch_profile.dram missing '
              f'banks_per_channel / row_size_b: got {_d}', file=sys.stderr)
        sys.exit(1)
    dram_cfg = {
        'banks_per_channel': int(_d['banks_per_channel']),
        'row_size_b': int(_d['row_size_b']),
    }
    latency_meta = latency_unit_metadata(_prof)
    print(f'[v10.3] dram_cfg from profile: {dram_cfg}', file=sys.stderr)
    print(f'[latency] unit=cycle source=gem5_tick '
          f'ticks_per_cycle={latency_meta["ticks_per_cycle"]:.9g}',
          file=sys.stderr)

    schema = make_schema()
    # V10.3：把 schema_version 与 dram_cfg 写进 parquet 的 schema_metadata，
    # 便于推理端 / 下游 pipeline 直接从 parquet 文件本身读出，无需依赖
    # 平级 meta.json。key/value 必须是 bytes。
    schema = schema.with_metadata({
        b'schema_version': b'v10_3_pq_a_b_c',
        b'dram_cfg': json.dumps(dram_cfg, separators=(',', ':')).encode('utf-8'),
        b'latency_unit': b'cycle',
        b'latency_source_unit': b'gem5_tick',
        b'latency_transform': b'none',
        b'latency_unit_meta':
            json.dumps(latency_meta, separators=(',', ':')).encode('utf-8'),
    })
    names = list(schema.names)
    # V10.3 B 派生列名集合
    _v10_3_B_names = {f'unique_cl_W{w}' for w in P1C_LONG_WINDOWS}
    _v10_3_C_names = set(P1C_DRAM)

    if args.in_jsonl:
        by_w_cols, macro_pc_vocab, n_total = load_from_jsonl(
            args.in_jsonl, names)
    else:
        by_w_cols, macro_pc_vocab, n_total = load_from_flat_parquet(
            args.from_parquet, names)

    latency_report = build_latency_report({
        w: {
            'fetch_latency': cols['fetch_latency'],
            'execution_latency': cols['execution_latency'],
        }
        for w, cols in by_w_cols.items()
    })
    print_latency_report(latency_report, file=sys.stderr)

    # 写 parquet
    by_w_count = {}
    by_w_thr = {}
    for w, cols in by_w_cols.items():
        sub = os.path.join(args.out_dir, f'workload={w}')
        os.makedirs(sub, exist_ok=True)
        path = os.path.join(sub, 'part-000.parquet')

        # 1) 转成 numpy，按 (core, tid, micro_seq) 排序
        n = len(cols['core_id'])
        cid = np.asarray(cols['core_id'], dtype=np.int32)
        tid = np.asarray(cols['thread_id'], dtype=np.int32)
        seq = np.asarray(cols['micro_seq'], dtype=np.int64)
        order = np.lexsort((seq, tid, cid))   # 主键 cid > tid > seq
        # 2) pos_in_thread 重算（按 (cid, tid) 分组的 0-based 序号）
        sorted_cid = cid[order]
        sorted_tid = tid[order]
        # 边界：当 (cid,tid) 与前一个不同时重置
        new_thread = np.empty(n, dtype=bool)
        new_thread[0] = True
        new_thread[1:] = (sorted_cid[1:] != sorted_cid[:-1]) | (sorted_tid[1:] != sorted_tid[:-1])
        thread_id_run = np.cumsum(new_thread) - 1
        # group counter via np.diff trick
        pos = np.arange(n, dtype=np.int32)
        # 找每个 thread 的起始位置，pos -= start
        starts = np.where(new_thread)[0]
        thread_start_per_row = starts[thread_id_run]
        pos_in_thread = pos - thread_start_per_row

        n_thr = int(new_thread.sum())

        # P1-C：sort 后注入上下文窗口派生（严格因果）。
        #   依赖列：is_load/is_store/is_atomic/is_branch/cacheline_paddr/
        #          macro_pc/d_bank_id（前 5 个所有数据集都有；后 1 个 P0-A
        #          老数据上为 0，bank_conflict_W64 在该情形下退化为窗口内
        #          mem-touching 总数，可接受）。
        sorted_cols = {
            'is_load':   np.asarray(cols['is_load'],   dtype=np.int64)[order],
            'is_store':  np.asarray(cols['is_store'],  dtype=np.int64)[order],
            'is_atomic': np.asarray(cols['is_atomic'], dtype=np.int64)[order],
            'is_branch': np.asarray(cols['is_branch'], dtype=np.int64)[order],
            'is_serialize': np.asarray(cols['is_serialize'], dtype=np.int64)[order],
            'is_microop': np.asarray(cols['is_microop'], dtype=np.int64)[order],
            'is_last_microop': np.asarray(cols['is_last_microop'], dtype=np.int64)[order],
            'cacheline_paddr':
                np.asarray(cols['cacheline_paddr'], dtype=np.uint64)[order],
            'macro_pc':
                np.asarray(cols['macro_pc'],        dtype=np.uint64)[order],
            'micro_pc':
                np.asarray(cols['micro_pc'],        dtype=np.uint64)[order],
            'd_bank_id':
                np.asarray(cols['d_bank_id'],       dtype=np.int64)[order],
            # V10.3 B+C 派生需要 paddr
            'paddr':
                np.asarray(cols['paddr'],           dtype=np.uint64)[order],
        }
        p1c = derive_p1c_window(sorted_cid, sorted_tid, sorted_cols, n)
        # V10.3 B+C：长窗口 unique_cl + DRAM bank/row 派生
        v10_3 = derive_v10_3_window(sorted_cid, sorted_tid, sorted_cols, n,
                                    dram_cfg)
        candidate_head, candidate_fetch_lat = derive_candidate_fetch_groups(
            sorted_cid, sorted_tid, sorted_cols,
            np.asarray(cols['fetch_latency'], dtype=np.float64)[order])

        # 3) 按 order 重排所有列
        arrays = []
        type_map = {f.name: f.type for f in schema}
        for name in names:
            v = cols[name]
            if name == 'pos_in_thread':
                arr = pos_in_thread
            elif name in P1C_CTX:
                # P1-C 派生列（已经是 sorted-order，直接喂 schema）
                arr = p1c[name]
            elif name in _v10_3_B_names or name in _v10_3_C_names:
                # V10.3 B+C 派生列
                arr = v10_3[name]
            elif name == 'candidate_fetch_group_head':
                arr = candidate_head
            elif name == 'candidate_fetch_latency':
                arr = candidate_fetch_lat
            else:
                # 选合适的 numpy dtype，int8/int16 由 schema 决定
                t = type_map[name]
                if t == pa.uint64():
                    arr = np.asarray(v, dtype=np.uint64)[order]
                elif t == pa.int64():
                    arr = np.asarray(v, dtype=np.int64)[order]
                elif t == pa.int32():
                    arr = np.asarray(v, dtype=np.int32)[order]
                elif t == pa.int16():
                    arr = np.asarray(v, dtype=np.int16)[order]
                elif t == pa.int8():
                    arr = np.asarray(v, dtype=np.int8)[order]
                elif t == pa.float64():
                    arr = np.asarray(v, dtype=np.float64)[order]
                else:
                    arr = np.asarray(v)[order]
            arrays.append(pa.array(arr, type=type_map[name]))
            cols[name] = None  # 释放原 python list

        table = pa.Table.from_arrays(arrays, schema=schema)
        pq.write_table(
            table, path,
            compression=args.compression,
            compression_level=args.compression_level,
            row_group_size=args.row_group_size,
            use_dictionary=True,
            data_page_size=1 << 20,
            write_statistics=True,
        )
        size = os.path.getsize(path)
        by_w_count[w] = n
        by_w_thr[w] = n_thr
        print(f'  wrote {w:20s} rows={n:>10,} threads={n_thr:>3} '
              f'size={size/1e6:>7.1f} MB -> {path}', file=sys.stderr)
        del table, arrays

    # vocab
    vocab = {
        'macro_pc': {f'{k:#x}': v for k, v in macro_pc_vocab.items()},
    }
    with open(os.path.join(args.out_dir, 'vocab.json'), 'w') as f:
        json.dump(vocab, f, separators=(',', ':'))

    meta = {
        'schema_version': 'v10_3_pq_a_b_c',
        'n_total': n_total,
        'row_group_size': args.row_group_size,
        'compression': args.compression,
        'compression_level': args.compression_level,
        'workloads': list(by_w_count.keys()),
        'workload_rows': by_w_count,
        'workload_threads': by_w_thr,
        'context_len_recommended': 128,
        'producer_arity': 4,
        'source': 'jsonl' if args.in_jsonl else 'flat_parquet',
        # V10.3：DRAM C 派生使用的 (banks_per_channel, row_size_b)，便于
        #   推理端复现公式（推理时 packer 重跑同 dram_cfg → bit-exact）。
        'dram_cfg': dram_cfg,
        'latency_unit': 'cycle',
        'latency_source_unit': 'gem5_tick',
        'latency_transform': 'none',
        'latency_unit_meta': latency_meta,
        'latency_quantiles': latency_report,
    }
    with open(os.path.join(args.out_dir, 'meta.json'), 'w') as f:
        json.dump(meta, f, indent=2)

    print(f'\n[done] dataset @ {args.out_dir}  rows={n_total:,}',
          file=sys.stderr)


if __name__ == '__main__':
    main()
