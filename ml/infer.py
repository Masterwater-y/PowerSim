#!/usr/bin/env python3
"""V9.5 inference 脚本：用训练好的 ckpt 对 build_inference_input.py 产出的
jsonl 流逐条预测 (fetch_latency, execution_latency, mispredicted)。

要点：
  1. 模型 cfg 直接从 ckpt['cfg'] 还原（保证 macro_pc_vocab 等口径一致）。
  2. 输入 jsonl 行内字段集合与 build_micro_dataset.py 一致；本脚本在内存中
     按 (core_id, thread_id) 分组、按 micro_seq 升序，1:1 与训练时
     ParquetWindowDataset 的窗口/特征派生路径对齐。
  3. macro_pc -> macro_pc_id 用 ckpt['vocab_path'] 或 dataset_root/vocab.json
     的 macro_pc 词表（命中：原 id；未命中：占位 0，与训练时 unseen 同规则）。
  4. 推理批量按行序滑动窗口（不打乱）；mispredicted 输出 sigmoid 概率
     与硬阈值 0.5 的 0/1。
  5. fetch_lat / exec_lat 是 log1p(cycle)，需要 expm1 反变换。
  6. CPU bf16 autocast。

输出 jsonl 行 schema：
  {"workload":..,"core_id":..,"thread_id":..,"micro_seq":..,
   "fetch_lat":<float-cycles>, "exec_lat":<float-cycles>,
   "mispred_prob":<float>, "mispred_hard":0|1}
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

os.environ.setdefault('OMP_NUM_THREADS', '32')
os.environ.setdefault('MKL_NUM_THREADS', '32')

import numpy as np
import torch

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent))

from ml.dataset import (                                       # noqa: E402
    SCALAR_BOOL, SCALAR_SMALL_INT, hash_addr_bucket, bucketize_dist,
)
from ml.model import TaoConfig, TaoCoreTransformer             # noqa: E402


def load_vocab(vocab_path: str) -> dict:
    if not vocab_path or not os.path.exists(vocab_path):
        return {}
    with open(vocab_path) as f:
        v = json.load(f)
    raw = v.get('macro_pc', {})
    # vocab.json 里 key 是 hex 字符串 "0x..."
    out = {}
    for k, val in raw.items():
        try:
            out[int(k, 16)] = int(val)
        except (ValueError, TypeError):
            pass
    return out


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


def encode_row(row: dict, vocab_macro_pc: dict, macro_pc_vocab_size: int):
    """把单条 sample 行编码成等价于 ParquetWindowDataset 的 feat dict（标量）。
    返回 dict[col_name] -> int。"""
    inp = row['input']
    uc = row['uarch_context']
    f = {}
    for k in SCALAR_BOOL:
        f[k] = int(inp[k])
    # SCALAR_SMALL_INT 既包括 input 里的 n_src/n_dst/size，也包括 uarch_context 里的 8 oracle + oracle_source
    for k in SCALAR_SMALL_INT:
        v = inp.get(k, uc.get(k, 0))
        f[k] = int(v)
    # i_* 字段在 hold-out 推理流里不存在 ifetch oracle，置 0 + i_oracle_source=1
    f.setdefault('i_path_class', 0)
    f.setdefault('i_coh_oracle', 0)
    f.setdefault('i_mesi_before', 0)
    f.setdefault('i_oracle_source', 1)
    # macro_pc -> id
    mpc = int(inp['macro_pc'])
    mpc_id = vocab_macro_pc.get(mpc, 0)
    if mpc_id >= macro_pc_vocab_size:
        mpc_id = 0
    f['macro_pc_id'] = mpc_id
    # producer_dists / classes
    pds = inp.get('producer_dists', [0, 0, 0, 0])
    pcs = inp.get('producer_classes', [255, 255, 255, 255])
    for i in range(4):
        f[f'd{i}'] = int(pds[i]) if i < len(pds) else -1
        f[f'pc{i}'] = int(pcs[i]) if i < len(pcs) else -1
    # 地址（先存原值，后批量 hash）
    f['vaddr'] = int(inp.get('vaddr', 0)) & ((1 << 64) - 1)
    f['paddr'] = int(uc.get('paddr', 0)) & ((1 << 64) - 1)
    f['cline'] = int(uc.get('cacheline_addr', 0)) & ((1 << 64) - 1)
    return f


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
        'i_path_class', 'i_coh_oracle', 'i_mesi_before', 'i_oracle_source']
    feat = {}
    for k in bool_keys + si_keys + ['macro_pc_id']:
        arr = np.array([r[k] for r in sl], dtype=np.int32)
        feat[k] = arr
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
    feat['vaddr_bucket'] = hash_addr_bucket(vaddr)
    feat['paddr_bucket'] = hash_addr_bucket(paddr)
    feat['cline_bucket'] = hash_addr_bucket(cline)

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
                    help='训练数据集 vocab.json (含 macro_pc 词表)；'
                         '不提供则全部映射为 0（unseen 占位）')
    ap.add_argument('--out-jsonl', required=True)
    ap.add_argument('--batch-size', type=int, default=64)
    ap.add_argument('--bf16', action='store_true', default=True)
    args = ap.parse_args()

    t0 = time.time()
    ck = torch.load(args.ckpt, map_location='cpu', weights_only=False)
    cfg_dict = ck['cfg']
    cfg = TaoConfig(**{k: v for k, v in cfg_dict.items()
                       if k in TaoConfig.__dataclass_fields__})
    print(f'[infer] cfg.macro_pc_vocab={cfg.macro_pc_vocab} '
          f'context_len={cfg.context_len}', file=sys.stderr)
    model = TaoCoreTransformer(cfg)
    model.load_state_dict(ck['model'])
    model.eval()
    n_param = model.num_params() / 1e6
    print(f'[infer] loaded ckpt step={ck.get("step")} '
          f'#params={n_param:.2f}M in {time.time()-t0:.2f}s',
          file=sys.stderr)

    vocab = load_vocab(args.vocab_json)
    print(f'[infer] vocab.macro_pc unique = {len(vocab)}', file=sys.stderr)

    bins = load_input(args.input_jsonl)
    print(f'[infer] groups (cid,tid) = {len(bins)}', file=sys.stderr)

    # 预编码每个分组
    encoded = {}
    n_total = 0
    for key, rows in bins.items():
        enc = [encode_row(r, vocab, cfg.macro_pc_vocab) for r in rows]
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
                fl = out['fetch_lat'].float().cpu().numpy()      # log1p(cycle)
                el = out['exec_lat'].float().cpu().numpy()
                mp = torch.sigmoid(out['mispred_logit']).float().cpu().numpy()
                fl_cyc = np.expm1(np.maximum(fl, 0))
                el_cyc = np.expm1(np.maximum(el, 0))
                for i, idx in enumerate(range(off, off + len(items))):
                    m = metas[idx]['meta']
                    fout.write(json.dumps({
                        'workload': m.get('workload', workload_name),
                        'core_id': cid, 'thread_id': tid,
                        'micro_seq': int(m['micro_seq']),
                        'fetch_lat': float(fl_cyc[i]),
                        'exec_lat': float(el_cyc[i]),
                        'mispred_prob': float(mp[i]),
                        'mispred_hard': int(mp[i] > 0.5),
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
