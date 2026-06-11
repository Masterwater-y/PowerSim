#!/usr/bin/env python3
import argparse, os, sys, time

ap = argparse.ArgumentParser()
ap.add_argument("--ckpt", required=True)
ap.add_argument("--ctx-len", type=int, default=128)
ap.add_argument("--device", default="cuda:0")
ap.add_argument("--warmup", type=int, default=5)
ap.add_argument("--iters", type=int, default=20)
ap.add_argument("--batches", default="64,128,256,512,1024,2048,4096")
args = ap.parse_args()

# import path
HERE = os.path.dirname(os.path.abspath(__file__))
INFER_ROOT = os.path.abspath(os.path.join(HERE, "..", "src", "04_infer"))
sys.path.insert(0, INFER_ROOT)

import numpy as np
import torch
from ml.model import TaoConfig, TaoCoreTransformer
from ml.dataset import (SCALAR_BOOL, SCALAR_SMALL_INT, SCALAR_P1C,
                        SCALAR_V10_3_B, SCALAR_V10_3_C)

device = torch.device(args.device)
torch.set_float32_matmul_precision("high")

print(f"[batch-sweep] loading ckpt {args.ckpt}", file=sys.stderr)
ck = torch.load(args.ckpt, map_location="cpu", weights_only=False)
cfg = TaoConfig(**{k: v for k, v in ck["cfg"].items()
                   if k in TaoConfig.__dataclass_fields__})
model = TaoCoreTransformer(cfg).to(device).eval()
model.load_state_dict(ck["model"])

ctx_len = args.ctx_len
i32_keys = list(SCALAR_BOOL) + list(SCALAR_SMALL_INT) + ['i_group_head', 'i_group_pos', 'uop_pos_in_macro'] \
           + list(SCALAR_P1C) + list(SCALAR_V10_3_B) + list(SCALAR_V10_3_C) + ['is_macro_head']
d_keys = ['d0', 'd1', 'd2', 'd3']
pc_keys = ['pc0', 'pc1', 'pc2', 'pc3']
bucket_keys = ['vaddr_bucket', 'paddr_bucket', 'cline_bucket', 'cline_p_bucket']

def make_input(B):
    feat = {}
    for k in i32_keys + d_keys + pc_keys + bucket_keys:
        feat[k] = torch.zeros((B, ctx_len), dtype=torch.long, device=device)
    attn = torch.ones((B, ctx_len), dtype=torch.bool, device=device)
    return feat, attn

print(f"{'batch':>6}  {'forward_ms':>11}  {'us_per_sample':>14}  {'rel_to_B64':>10}", file=sys.stderr)
print("-" * 50, file=sys.stderr)

results = []
T0 = None
batches = [int(x) for x in args.batches.split(",") if x.strip()]
for B in batches:
    feat, attn = make_input(B)

    # warmup
    with torch.inference_mode(), torch.autocast(device_type="cuda", dtype=torch.bfloat16):
        for _ in range(args.warmup):
            _ = model({"feat": feat, "attn_mask": attn})
    torch.cuda.synchronize()

    t0 = time.perf_counter()
    with torch.inference_mode(), torch.autocast(device_type="cuda", dtype=torch.bfloat16):
        for _ in range(args.iters):
            out = model({"feat": feat, "attn_mask": attn})
    torch.cuda.synchronize()
    t1 = time.perf_counter()

    ms = 1000 * (t1 - t0) / args.iters
    us_per = 1000 * ms / B
    if T0 is None:
        T0 = ms
    rel = ms / T0
    results.append((B, ms, us_per, rel))
    print(f"{B:>6}  {ms:>11.2f}  {us_per:>14.2f}  {rel:>10.2f}x", file=sys.stderr)

print("\n=== summary (csv) ===")
print("batch,forward_ms,us_per_sample,rel_to_first")
for B, ms, us, rel in results:
    print(f"{B},{ms:.3f},{us:.3f},{rel:.3f}")
