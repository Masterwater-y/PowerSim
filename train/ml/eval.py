#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path

os.environ.setdefault('OMP_NUM_THREADS', '8')
os.environ.setdefault('MKL_NUM_THREADS', '8')
os.environ.setdefault('PYTHONUNBUFFERED', '1')

import numpy as np
import torch
from torch.utils.data import DataLoader, Subset

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent))

from ml.dataset import DatasetSpec, ParquetWindowDataset, collate  # noqa: E402
from ml.model import TaoConfig, TaoCoreTransformer  # noqa: E402
from ml.train import estimate_mispred_pos_weight, run_validation  # noqa: E402


def get_args():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ckpt', required=True)
    ap.add_argument('--data', required=True)
    ap.add_argument('--sample-size', type=int, default=50000,
                    help='随机抽样多少条样本做评估')
    ap.add_argument('--batch-size', type=int, default=1024)
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--seed', type=int, default=0)
    ap.add_argument('--device', default='auto',
                    help='auto/cpu/cuda/cuda:0')
    return ap.parse_args()


def pick_device(arg: str) -> torch.device:
    if arg == 'auto':
        return torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    return torch.device(arg)


def main():
    args = get_args()
    t0 = time.time()
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    random.seed(args.seed)

    device = pick_device(args.device)
    ck = torch.load(args.ckpt, map_location='cpu', weights_only=False)
    ck_args = ck.get('args', {})
    cfg_dict = ck['cfg']
    cfg = TaoConfig(**{k: v for k, v in cfg_dict.items()
                       if k in TaoConfig.__dataclass_fields__})

    ctx = int(ck_args.get('ctx', cfg.context_len))
    spec = DatasetSpec(root=args.data, context_len=ctx)
    ds = ParquetWindowDataset(spec)
    total_rows = len(ds)
    sample_size = min(args.sample_size, total_rows)

    rng = np.random.default_rng(args.seed)
    indices = rng.choice(total_rows, size=sample_size, replace=False)
    subset = Subset(ds, indices.tolist())
    loader = DataLoader(
        subset,
        batch_size=args.batch_size,
        shuffle=False,
        num_workers=args.workers,
        pin_memory=(device.type == 'cuda'),
        persistent_workers=(args.workers > 0),
        collate_fn=collate,
        drop_last=False,
    )

    if cfg.mispred_pos_weight <= 1.0:
        cfg.mispred_pos_weight = float(
            ck_args.get('mispred_pos_weight', estimate_mispred_pos_weight(args.data))
        )

    model = TaoCoreTransformer(cfg).to(device)
    model.load_state_dict(ck['model'])
    model.eval()

    use_amp = bool(ck_args.get('bf16', True))
    amp_ctx = (lambda: torch.amp.autocast(device_type=device.type, dtype=torch.bfloat16)) \
        if use_amp and device.type in ('cpu', 'cuda') else \
        (lambda: torch.autocast(device_type='cpu', enabled=False))

    result = run_validation(
        model, model, loader, ds, device, amp_ctx, max_batches=0)

    result.update({
        'ckpt': os.path.abspath(args.ckpt),
        'data': os.path.abspath(args.data),
        'sample_size': result['rows'],
        'device': str(device),
        'elapsed_s': time.time() - t0,
        'train_step': ck.get('step'),
        'train_best_loss': ck.get('best_loss'),
        'train_ema_loss': ck.get('ema_loss'),
    })
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
