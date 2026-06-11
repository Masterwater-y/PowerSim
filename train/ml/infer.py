#!/usr/bin/env python3
"""V9.5 inference 脚本：用训练好的 ckpt 对 build_inference_input.py 产出的
jsonl 流逐条预测 (fetch_latency, execution_latency, mispredicted)。

要点：
  1. 模型 cfg 直接从 ckpt['cfg'] 还原，保证训练/推理特征口径一致。
  2. 输入 jsonl 行内字段集合与 build_micro_dataset.py 一致；本脚本在内存中
     按 (core_id, thread_id) 分组、按 micro_seq 升序，1:1 与训练时
     ParquetWindowDataset 的窗口/特征派生路径对齐。
  3. 新 schema 下不再依赖 macro_pc / macro_pc_id 词表。
  4. 推理批量按行序滑动窗口（不打乱）；mispredicted 输出 sigmoid 概率
     与硬阈值 0.5 的 0/1。
  5. fetch_lat / exec_lat 是 log1p(cycle)，需要 expm1 反变换。
  6. fetch 采用 head-gated 语义：仅当 head_hard=1 时输出非零 fetch_lat。
  7. CPU bf16 autocast。

输出 jsonl 行 schema：
  {"workload":..,"core_id":..,"thread_id":..,"micro_seq":..,
   "fetch_lat":<float-cycles>, "fetch_lat_pos":<float-cycles>,
   "exec_lat":<float-cycles>,
   "mispred_prob":<float>, "mispred_hard":0|1,
   "head_prob":<float>, "head_hard":0|1}
"""
from __future__ import annotations
import argparse
import json
import math
import os
import sys
import time
from collections import defaultdict, deque
from pathlib import Path

os.environ.setdefault('OMP_NUM_THREADS', '32')
os.environ.setdefault('MKL_NUM_THREADS', '32')

import numpy as np
import torch

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent))

from ml.dataset import (                                       # noqa: E402
    SCALAR_BOOL, SCALAR_SMALL_INT, SCALAR_P1C, SCALAR_V10_3_B,
    SCALAR_V10_3_C, hash_addr_bucket, bucketize_dist,
)
from ml.model import TaoConfig, TaoCoreTransformer             # noqa: E402


def load_input(in_jsonl: str):
    """读 build_inference_input.py 输出的 jsonl，按 (core_id, thread_id) 分组保存。
    返回：dict[(cid,tid)] -> list[dict]，每组按 micro_seq 升序。"""
    bins = defaultdict(list)
    with open(in_jsonl) as f:
        for ln in f:
            s = ln.strip()
            if not s.startswith('{'):
                continue
            r = json.loads(s)
            m = r['meta']
            bins[(int(m['core_id']), int(m['thread_id']))].append(r)
    for k in bins:
        bins[k].sort(key=lambda x: int(x['meta']['micro_seq']))
    return bins


def encode_row(row: dict, _compat_warn: dict = {'cacheline_paddr': False}):
    """把单条 sample 行编码成等价于 ParquetWindowDataset 的 feat dict（标量）。
    返回 dict[col_name] -> int。"""
    inp = row['input']
    uc = row['uarch_context']
    f = {}
    for k in SCALAR_BOOL:
        f[k] = int(inp[k])
    # SCALAR_SMALL_INT 只包含 functional trace 与 d-side timing-functional 字段。
    for k in SCALAR_SMALL_INT:
        v = inp.get(k, uc.get(k, 0))
        f[k] = int(v)
    for k in SCALAR_P1C:
        f[k] = int(inp.get(k, uc.get(k, 0)))
    for k in SCALAR_V10_3_B:
        f[k] = int(inp.get(k, uc.get(k, 0)))
    for k in SCALAR_V10_3_C:
        f[k] = int(inp.get(k, uc.get(k, 0)))
    # producer_dists / classes
    pds = inp.get('producer_dists', [0, 0, 0, 0])
    pcs = inp.get('producer_classes', [255, 255, 255, 255])
    for i in range(4):
        f[f'd{i}'] = int(pds[i]) if i < len(pds) else -1
        f[f'pc{i}'] = int(pcs[i]) if i < len(pcs) else -1
    f['macro_pc'] = int(inp.get('macro_pc', 0)) & ((1 << 64) - 1)
    f['micro_pc'] = int(inp.get('micro_pc', 0)) & ((1 << 32) - 1)
    # 地址（先存原值，后批量 hash / 派生窗口特征）
    f['vaddr'] = int(inp.get('vaddr', 0)) & ((1 << 64) - 1)
    f['paddr'] = int(uc.get('paddr', 0)) & ((1 << 64) - 1)
    f['cline'] = int(uc.get('cacheline_addr', 0)) & ((1 << 64) - 1)
    # V10 方案 B：paddr-line 真值；
    # COMPAT-OLD-50M: 旧 inference jsonl 无 cacheline_paddr，回退到 cacheline_addr。
    # 全 V10+ 重采且重建 inference 输入后该 fallback 可删。
    if 'cacheline_paddr' in uc:
        f['cline_p'] = int(uc['cacheline_paddr']) & ((1 << 64) - 1)
    else:
        if not _compat_warn['cacheline_paddr']:
            import sys as _sys
            print("[infer][COMPAT-OLD-50M] cacheline_paddr 缺失 -> "
                  "fallback cacheline_addr", file=_sys.stderr)
            _compat_warn['cacheline_paddr'] = True
        f['cline_p'] = int(uc.get('cacheline_addr', 0)) & ((1 << 64) - 1)
    return f


def derive_sequence_features(rows_enc: list):
    """按单个 (core_id, thread_id) 全序列派生上下文 / 结构 / i-group 特征。"""
    win64 = deque()
    win256_cl = deque()
    win1024_cl = deque()
    win256_dram = deque()
    cl_count64 = defaultdict(int)
    pc_count64 = defaultdict(int)
    bank_count64 = defaultdict(int)
    cl_count256 = defaultdict(int)
    cl_count1024 = defaultdict(int)
    dram_bank_count = defaultdict(int)
    dram_row_count = defaultdict(int)
    last_cl_pos = {}
    last_branch_pos = -1
    prev_i_cl = None
    prev_macro_pc = None
    prev_is_micro = 0
    prev_is_last_micro = 0
    i_group_pos = 0
    uop_pos_in_macro = 0
    banks = 16
    row_b = 8192
    bank_mask = banks - 1

    for i, r in enumerate(rows_enc):
        cur_mem = bool(r['is_load'] or r['is_store'] or r['is_atomic'])
        cur_br = bool(r['is_branch'])
        cur_cl = int(r['cline_p'])
        cur_pc = int(r['macro_pc'])
        cur_bk = int(r.get('d_bank_id', 0))
        cur_pa = int(r['paddr'])
        cur_dram_bank = int((cur_pa >> 6) & bank_mask)
        cur_row = int(cur_pa // row_b) if row_b > 0 else 0
        cur_i_cl = int(cur_pc >> 6)

        r['mem_density_W64'] = min(sum(1 for _, mem, _, _, _, _ in win64 if mem), 32767)
        r['branch_density_W64'] = min(sum(1 for _, _, br, _, _, _ in win64 if br), 32767)
        r['unique_cl_W64'] = min(len(cl_count64), 32767)
        r['pc_freq_W64'] = min(pc_count64.get(cur_pc, 0), 32767)
        r['bank_conflict_W64'] = min(bank_count64.get(cur_bk, 0), 32767) if cur_mem else 0
        if cur_mem and cur_cl in last_cl_pos:
            dist = i - last_cl_pos[cur_cl]
            r['cl_reuse_dist_log'] = max(0, min(int(math.log2(dist)) if dist > 0 else 0, 15))
        else:
            r['cl_reuse_dist_log'] = 15
        if last_branch_pos >= 0:
            dist = i - last_branch_pos
            r['time_since_last_branch_log'] = max(0, min(int(math.log2(dist)) if dist > 0 else 0, 15))
        else:
            r['time_since_last_branch_log'] = 15

        r['unique_cl_W256'] = min(len(cl_count256), 255)
        r['unique_cl_W1024'] = min(len(cl_count1024), 2047)
        r['dram_bank_id'] = min(cur_dram_bank, 15)
        r['dram_bank_freq_W256'] = min(dram_bank_count.get(cur_dram_bank, 0), 255) if cur_mem else 0
        r['dram_row_freq_W256'] = min(dram_row_count.get(cur_row, 0), 255) if cur_mem else 0

        macro_head = 1 if (prev_macro_pc is None or
                           prev_is_micro == 0 or
                           prev_is_last_micro == 1 or
                           cur_pc != prev_macro_pc) else 0
        uop_pos_in_macro = 0 if macro_head else min(uop_pos_in_macro + 1, 15)
        r['is_macro_head'] = macro_head
        r['uop_pos_in_macro'] = uop_pos_in_macro

        head = 1 if prev_i_cl is None or cur_i_cl != prev_i_cl else 0
        i_group_pos = 0 if head else min(i_group_pos + 1, 15)
        r['i_group_head'] = head
        r['i_group_pos'] = i_group_pos

        win64.append((i, cur_mem, cur_br, cur_cl, cur_pc, cur_bk))
        pc_count64[cur_pc] += 1
        if cur_mem:
            cl_count64[cur_cl] += 1
            last_cl_pos[cur_cl] = i
            bank_count64[cur_bk] += 1
            win256_cl.append((i, cur_cl))
            cl_count256[cur_cl] += 1
            win1024_cl.append((i, cur_cl))
            cl_count1024[cur_cl] += 1
            win256_dram.append((i, cur_dram_bank, cur_row))
            dram_bank_count[cur_dram_bank] += 1
            dram_row_count[cur_row] += 1
        if cur_br:
            last_branch_pos = i
        prev_i_cl = cur_i_cl
        prev_macro_pc = cur_pc
        prev_is_micro = int(r['is_microop'])
        prev_is_last_micro = int(r['is_last_microop'])

        while len(win64) > 64:
            _, ev_mem, _, ev_cl, ev_pc, ev_bk = win64.popleft()
            pc_left = pc_count64[ev_pc] - 1
            if pc_left <= 0:
                del pc_count64[ev_pc]
            else:
                pc_count64[ev_pc] = pc_left
            if ev_mem:
                cl_left = cl_count64[ev_cl] - 1
                if cl_left <= 0:
                    del cl_count64[ev_cl]
                else:
                    cl_count64[ev_cl] = cl_left
                bk_left = bank_count64[ev_bk] - 1
                if bk_left <= 0:
                    del bank_count64[ev_bk]
                else:
                    bank_count64[ev_bk] = bk_left
        while len(win256_cl) > 256:
            _, ev_cl = win256_cl.popleft()
            left = cl_count256[ev_cl] - 1
            if left <= 0:
                del cl_count256[ev_cl]
            else:
                cl_count256[ev_cl] = left
        while len(win1024_cl) > 1024:
            _, ev_cl = win1024_cl.popleft()
            left = cl_count1024[ev_cl] - 1
            if left <= 0:
                del cl_count1024[ev_cl]
            else:
                cl_count1024[ev_cl] = left
        while len(win256_dram) > 256:
            _, ev_bank, ev_row = win256_dram.popleft()
            left = dram_bank_count[ev_bank] - 1
            if left <= 0:
                del dram_bank_count[ev_bank]
            else:
                dram_bank_count[ev_bank] = left
            left = dram_row_count[ev_row] - 1
            if left <= 0:
                del dram_row_count[ev_row]
            else:
                dram_row_count[ev_row] = left


def feats_to_window(rows_enc: list, anchor_idx: int, ctx_len: int):
    """从 (core,tid) 已编码序列中取 anchor 处的窗口；返回 numpy dict + attn_mask。"""
    seg_start = 0
    row = anchor_idx
    ctx_start = max(seg_start, row + 1 - ctx_len)
    real_len = (row + 1) - ctx_start
    pad = ctx_len - real_len
    sl = rows_enc[ctx_start:row + 1]

    bool_keys = list(SCALAR_BOOL)
    si_keys = list(SCALAR_SMALL_INT) + [
        'i_group_head', 'i_group_pos', 'uop_pos_in_macro']
    ctx_keys = list(SCALAR_P1C) + list(SCALAR_V10_3_B) + list(SCALAR_V10_3_C)
    feat = {}
    for k in bool_keys + si_keys + ctx_keys:
        arr = np.array([r[k] for r in sl], dtype=np.int32)
        feat[k] = arr
    feat['is_macro_head'] = np.array([r['is_macro_head'] for r in sl], dtype=np.int32)
    for i in range(4):
        d = np.array([r[f'd{i}'] for r in sl], dtype=np.int64)
        pc = np.array([r[f'pc{i}'] for r in sl], dtype=np.int32)
        feat[f'd{i}'] = bucketize_dist(d)
        pc = np.where(pc == 255, 7, pc)
        pc = np.clip(pc, 0, 7)
        feat[f'pc{i}'] = pc
    vaddr = np.array([r['vaddr'] for r in sl], dtype=np.uint64)
    paddr = np.array([r['paddr'] for r in sl], dtype=np.uint64)
    cline = np.array([r['cline'] for r in sl], dtype=np.uint64)
    cline_p = np.array([r['cline_p'] for r in sl], dtype=np.uint64)
    feat['vaddr_bucket'] = hash_addr_bucket(vaddr)
    feat['paddr_bucket'] = hash_addr_bucket(paddr)
    feat['cline_bucket'] = hash_addr_bucket(cline)
    # V10 方案 B：与 ml/dataset.py / model.py 对齐。
    # COMPAT-OLD-50M: cline_p 在 encode_row 已 fallback 为 cline。
    feat['cline_p_bucket'] = hash_addr_bucket(cline_p)

    if pad > 0:
        for k, v in feat.items():
            pad_arr = np.zeros((pad,) + v.shape[1:], dtype=v.dtype)
            feat[k] = np.concatenate([pad_arr, v], axis=0)
    attn_mask = np.concatenate([
        np.zeros(pad, dtype=np.int8),
        np.ones(real_len, dtype=np.int8),
    ])
    return feat, attn_mask


def collate_batch(items: list):
    """items: list of (feat_dict, attn_mask)；返回 torch tensors."""
    keys = list(items[0][0].keys())
    out = {}
    for k in keys:
        a = np.stack([it[0][k] for it in items], axis=0)
        out[k] = torch.from_numpy(a).long()
    am = torch.from_numpy(np.stack([it[1] for it in items], axis=0)).bool()
    return out, am


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ckpt', required=True)
    ap.add_argument('--input-jsonl', required=True,
                    help='build_inference_input.py 的输出')
    ap.add_argument('--vocab-json', default='',
                    help='兼容旧命令行参数；当前 schema 下不会使用')
    ap.add_argument('--out-jsonl', required=True)
    ap.add_argument('--batch-size', type=int, default=64)
    ap.add_argument('--head-threshold', type=float, default=0.5)
    ap.add_argument('--bf16', action='store_true', default=True)
    args = ap.parse_args()

    t0 = time.time()
    ck = torch.load(args.ckpt, map_location='cpu', weights_only=False)
    cfg_dict = ck['cfg']
    cfg = TaoConfig(**{k: v for k, v in cfg_dict.items()
                       if k in TaoConfig.__dataclass_fields__})
    print(f'[infer] context_len={cfg.context_len}', file=sys.stderr)
    model = TaoCoreTransformer(cfg)
    model.load_state_dict(ck['model'])
    model.eval()
    n_param = model.num_params() / 1e6
    print(f'[infer] loaded ckpt step={ck.get("step")} '
          f'#params={n_param:.2f}M in {time.time()-t0:.2f}s',
          file=sys.stderr)

    bins = load_input(args.input_jsonl)
    print(f'[infer] groups (cid,tid) = {len(bins)}', file=sys.stderr)

    # 预编码每个分组
    encoded = {}
    n_total = 0
    for key, rows in bins.items():
        enc = [encode_row(r) for r in rows]
        derive_sequence_features(enc)
        encoded[key] = enc
        n_total += len(enc)
    print(f'[infer] total rows = {n_total}', file=sys.stderr)

    use_amp = args.bf16
    amp_ctx = (torch.amp.autocast(device_type='cpu', dtype=torch.bfloat16)
               if use_amp else torch.autocast(device_type='cpu', enabled=False))

    fout = open(args.out_jsonl, 'w')
    n_done = 0
    t1 = time.time()
    workload_name = bins[next(iter(bins))][0]['meta'].get('workload', '')
    with torch.no_grad():
        for key, enc in encoded.items():
            cid, tid = key
            metas = bins[key]
            B = args.batch_size
            for off in range(0, len(enc), B):
                items = [(*feats_to_window(enc, off + i, cfg.context_len),)
                         for i in range(min(B, len(enc) - off))]
                feat, attn = collate_batch(items)
                with amp_ctx:
                    out = model({'feat': feat, 'attn_mask': attn})
                fl = out['fetch_lat'].float().cpu().numpy()      # log1p(cycle), positive branch
                el = out['exec_lat'].float().cpu().numpy()
                eq = out['exec_quantiles'].float().cpu().numpy()
                eb = out['exec_bucket_logits'].argmax(dim=-1).cpu().numpy()
                mp = torch.sigmoid(out['mispred_logit']).float().cpu().numpy()
                hp = torch.sigmoid(out['head_logit']).float().cpu().numpy()
                head_hard = (hp > float(args.head_threshold)).astype(np.int32)
                fl_pos_cyc = np.expm1(np.maximum(fl, 0))
                fl_cyc = fl_pos_cyc * head_hard
                el_cyc = np.expm1(np.maximum(el, 0))
                eq_cyc = np.expm1(np.maximum(eq, 0))
                for i, idx in enumerate(range(off, off + len(items))):
                    m = metas[idx]['meta']
                    inp = metas[idx].get('input', {})
                    mispred_valid = (
                        int(inp.get('is_branch', 0)) > 0
                        and (int(inp.get('is_last_microop', 0)) > 0
                             or int(inp.get('is_microop', 0)) == 0)
                    )
                    mp_raw = float(mp[i])
                    mp_out = mp_raw if mispred_valid else 0.0
                    fout.write(json.dumps({
                        'workload': m.get('workload', workload_name),
                        'core_id': cid, 'thread_id': tid,
                        'micro_seq': int(m['micro_seq']),
                        'fetch_lat': float(fl_cyc[i]),
                        'fetch_lat_pos': float(fl_pos_cyc[i]),
                        'exec_lat': float(el_cyc[i]),
                        'exec_bucket': int(eb[i]),
                        'exec_lat_p50': float(eq_cyc[i, 0]),
                        'exec_lat_p90': float(eq_cyc[i, 1]),
                        'exec_lat_p99': float(eq_cyc[i, 2]),
                        'mispred_valid': int(mispred_valid),
                        'mispred_prob_raw': mp_raw,
                        'mispred_prob': mp_out,
                        'mispred_hard': int(mispred_valid and mp_raw > 0.5),
                        'head_prob': float(hp[i]),
                        'head_hard': int(head_hard[i]),
                    }, separators=(',', ':')))
                    fout.write('\n')
                n_done += len(items)
                if n_done % 50000 < B:
                    dt = time.time() - t1
                    print(f'[infer] {n_done}/{n_total} '
                          f'({n_done/dt:.0f} rows/s)', file=sys.stderr)
    fout.close()
    print(f'[infer] done {n_done} rows -> {args.out_jsonl} '
          f'in {time.time()-t0:.2f}s', file=sys.stderr)


if __name__ == '__main__':
    main()
