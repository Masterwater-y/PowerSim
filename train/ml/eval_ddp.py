#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from contextlib import nullcontext
from pathlib import Path

os.environ.setdefault('OMP_NUM_THREADS', '8')
os.environ.setdefault('MKL_NUM_THREADS', '8')
os.environ.setdefault('PYTHONUNBUFFERED', '1')

import numpy as np
import torch
import torch.distributed as dist
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
                    help='评估样本数；大于等于验证集行数时使用全量顺序评估')
    ap.add_argument('--batch-size', type=int, default=1024,
                    help='每个 rank / GPU 的 batch size')
    ap.add_argument('--workers', type=int, default=4,
                    help='每个 rank 的 DataLoader worker 数')
    ap.add_argument('--seed', type=int, default=0)
    ap.add_argument('--device', default='cuda',
                    help='cuda/cpu；cuda 时每个 rank 使用 LOCAL_RANK 对应 GPU')
    ap.add_argument('--progress-width', type=int, default=40)
    ap.add_argument('--progress-every', type=int, default=1,
                    help='rank0 每多少个本地 batch 刷新一次进度条')
    return ap.parse_args()


def setup_dist() -> tuple[int, int, int]:
    if 'RANK' not in os.environ:
        return 0, 1, 0
    rank = int(os.environ['RANK'])
    world = int(os.environ['WORLD_SIZE'])
    local_rank = int(os.environ.get('LOCAL_RANK', 0))
    backend = 'nccl' if torch.cuda.is_available() else 'gloo'
    dist.init_process_group(backend=backend)
    return rank, world, local_rank


def cleanup_dist() -> None:
    if dist.is_available() and dist.is_initialized():
        try:
            dist.destroy_process_group()
        except RuntimeError:
            # Best-effort cleanup: avoid printing a long NCCL teardown tail after
            # results are already emitted.
            pass


def dist_barrier(device: torch.device) -> None:
    if not (dist.is_available() and dist.is_initialized()):
        return
    if device.type == 'cuda':
        dist.barrier(device_ids=[device.index])
    else:
        dist.barrier()


def pick_device(arg: str, local_rank: int) -> torch.device:
    if arg == 'cpu':
        return torch.device('cpu')
    if not torch.cuda.is_available():
        raise RuntimeError('CUDA is not available; use --device cpu or fix CUDA visibility')
    torch.cuda.set_device(local_rank)
    return torch.device('cuda', local_rank)


def make_indices(total_rows: int, sample_size: int, seed: int) -> np.ndarray:
    sample_size = min(sample_size, total_rows)
    if sample_size >= total_rows:
        return np.arange(total_rows, dtype=np.int64)
    rng = np.random.default_rng(seed)
    return rng.choice(total_rows, size=sample_size, replace=False).astype(np.int64)


def progress_bar(done: int, total: int, start: float, width: int) -> str:
    total = max(1, total)
    done = min(done, total)
    frac = done / total
    filled = int(width * frac)
    bar = '#' * filled + '-' * (width - filled)
    elapsed = time.time() - start
    rate = done / elapsed if elapsed > 0 else 0.0
    eta = (total - done) / rate if rate > 0 else 0.0
    return (f'\r[eval] |{bar}| {done}/{total} '
            f'({frac * 100:6.2f}%) {rate:,.1f} samples/s ETA {eta:,.0f}s')


def main():
    args = get_args()
    rank, world, local_rank = setup_dist()
    is_main = rank == 0
    t0 = time.time()

    torch.manual_seed(args.seed + rank)
    np.random.seed(args.seed + rank)
    random.seed(args.seed + rank)

    try:
        device = pick_device(args.device, local_rank)
        ck = torch.load(args.ckpt, map_location='cpu', weights_only=False)
        ck_args = ck.get('args', {})
        cfg_dict = ck['cfg']
        cfg = TaoConfig(**{k: v for k, v in cfg_dict.items()
                           if k in TaoConfig.__dataclass_fields__})

        ctx = int(ck_args.get('ctx', cfg.context_len))
        spec = DatasetSpec(root=args.data, context_len=ctx)
        ds = ParquetWindowDataset(spec)
        total_rows = len(ds)
        indices = make_indices(total_rows, args.sample_size, args.seed)
        sample_size = int(indices.shape[0])
        local_indices = indices[rank::world]

        subset = Subset(ds, local_indices.tolist())
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
        amp_ctx = (
            lambda: torch.amp.autocast(device_type=device.type, dtype=torch.bfloat16)
        ) if use_amp and device.type in ('cpu', 'cuda') else nullcontext

        if is_main:
            print(json.dumps({
                'event': 'eval_start',
                'ckpt': os.path.abspath(args.ckpt),
                'data': os.path.abspath(args.data),
                'sample_size': sample_size,
                'total_rows': total_rows,
                'world_size': world,
                'batch_size_per_rank': args.batch_size,
                'workers_per_rank': args.workers,
                'device': str(device),
            }, ensure_ascii=False), flush=True)

        if is_main:
            sys.stderr.write(progress_bar(0, sample_size, t0, args.progress_width))
            sys.stderr.flush()
        result = run_validation(
            model, model, loader, ds, device, amp_ctx, max_batches=0)
        if dist.is_available() and dist.is_initialized():
            dist_barrier(device)

        if is_main:
            sys.stderr.write('\n')
            global_total = int(result['rows'])
            if global_total <= 0:
                raise RuntimeError('No samples were evaluated')
            final = {
                'ckpt': os.path.abspath(args.ckpt),
                'data': os.path.abspath(args.data),
                'sample_size': global_total,
                'world_size': world,
                'device': args.device,
                'elapsed_s': time.time() - t0,
                'samples_per_sec': global_total / max(1e-9, time.time() - t0),
                'train_step': ck.get('step'),
                'train_best_loss': ck.get('best_loss'),
                'train_ema_loss': ck.get('ema_loss'),
            }
            final.update(result)
            print(json.dumps(final, indent=2, ensure_ascii=False), flush=True)
        if dist.is_available() and dist.is_initialized():
            # Ensure non-zero ranks do not teardown the process group while rank0
            # is still printing the final JSON.
            dist_barrier(device)
    finally:
        cleanup_dist()


if __name__ == '__main__':
    main()
