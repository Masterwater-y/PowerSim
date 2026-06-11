"""TaoCoreTransformer 训练器（生产级最佳实践版）。

核心机制
========
1. 实时日志（不会再"卡缓冲"）
   - 直接用 logging + StreamHandler(sys.stderr) + FileHandler，每条记录后强制 flush + fsync；
   - tqdm 进度条实时刷新到 tty 与日志文件均可读；
   - print() 全部替换为 log.info(...)，不依赖 stdout 缓冲。

2. Checkpoint 三层兜底
   - 周期 ckpt：每 --save-every 步保存 step-suffix 文件，原子写入（写到 .tmp 再 rename）；
   - 最佳 ckpt：按 ema_loss 单调下降时刻保存 best.pt，便于早停场景使用；
   - 信号触发：SIGUSR1 立刻打一份 emergency 快照；SIGINT/SIGTERM 优雅写完 last.pt 再退出；
   - 异常兜底：训练循环包 try/finally，即使 OOM / 崩溃也尝试存最后一帧。

3. 断点续训（--resume PATH）
   - 完整恢复 model / optimizer / scheduler / step / rng / ema_loss / best；

4. 状态外化（无侵入观察进度）
   - 每 --log-every 步把 {step, ema_loss, samples_per_sec, ts} 写到 ckpt-dir/status.json；
   - 外部脚本 / 监控只需 cat status.json 即可看进度。

5. CPU SPR 友好默认
   - BF16 autocast、OMP/MKL=32、KMP_AFFINITY=fine,compact；
   - 可选 torch.compile（默认关）。
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
import signal
import sys
import time
import traceback
from pathlib import Path

# 必须在 import torch 之前设置，才能影响 OpenMP/MKL 线程池
os.environ.setdefault('OMP_NUM_THREADS', '32')
os.environ.setdefault('MKL_NUM_THREADS', '32')
os.environ.setdefault('KMP_AFFINITY', 'granularity=fine,compact,1,0')
# 让 Python 默认使用行缓冲；即使 stdout/stderr 走管道也即时落盘
os.environ.setdefault('PYTHONUNBUFFERED', '1')

import numpy as np
import torch
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.utils.data import DataLoader
from torch.utils.data.distributed import DistributedSampler

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent))

from ml.dataset import ParquetWindowDataset, DatasetSpec, collate    # noqa: E402
from ml.model import TaoConfig, TaoCoreTransformer                   # noqa: E402

log = logging.getLogger('tao.train')


# =====================================================================
# Logging
# =====================================================================
class _FlushFileHandler(logging.FileHandler):
    """每条记录后立刻 flush + fsync，避免 tail -f 看不到。"""

    def emit(self, record):
        super().emit(record)
        try:
            self.flush()
            if self.stream is not None:
                os.fsync(self.stream.fileno())
        except (OSError, ValueError):
            pass


def setup_logging(log_path: str | None, level: int = logging.INFO) -> None:
    fmt = '%(asctime)s %(levelname).1s %(message)s'
    datefmt = '%H:%M:%S'
    formatter = logging.Formatter(fmt, datefmt)
    log.setLevel(level)
    log.handlers.clear()

    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(formatter)
    log.addHandler(sh)

    if log_path:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        fh = _FlushFileHandler(log_path, mode='a', encoding='utf-8')
        fh.setFormatter(formatter)
        log.addHandler(fh)

    log.propagate = False


def dist_is_initialized() -> bool:
    return dist.is_available() and dist.is_initialized()


def is_main_process() -> bool:
    return (not dist_is_initialized()) or dist.get_rank() == 0


def get_rank() -> int:
    return dist.get_rank() if dist_is_initialized() else 0


def get_world_size() -> int:
    return dist.get_world_size() if dist_is_initialized() else 1


# =====================================================================
# Args
# =====================================================================
def get_args():
    ap = argparse.ArgumentParser()
    ap.add_argument('--data', required=True,
                    help='数据目录。既可直接指向单一 parquet 根目录，也可指向'
                         '包含 train/ 和 val/ 的 split 根目录。V10 schema 应'
                         '包含 cacheline_paddr 列；macro_pc / macro_pc_id '
                         '不再作为模型输入。启动期会校验，缺失会打 warning '
                         '但不会硬失败（COMPAT-OLD-50M）。')
    ap.add_argument('--train-data', default='',
                    help='显式指定训练集根目录；为空时从 --data 自动推断')
    ap.add_argument('--val-data', default='',
                    help='显式指定验证集根目录；为空时从 --data 自动推断；留空表示不做 validation')
    ap.add_argument('--ctx', type=int, default=128)
    ap.add_argument('--bs', type=int, default=128)
    ap.add_argument('--steps', type=int, default=200)
    ap.add_argument('--lr', type=float, default=3e-4)
    ap.add_argument('--wd', type=float, default=0.01)
    ap.add_argument('--warmup', type=int, default=20)
    ap.add_argument('--workers', type=int, default=8)
    ap.add_argument('--val-workers', type=int, default=-1,
                    help='验证 DataLoader worker 数；-1 表示继承 --workers')
    ap.add_argument('--log-every', type=int, default=20,
                    help='每 N 步打印 + 写一次 status.json')
    ap.add_argument('--val-every', type=int, default=2000,
                    help='>0 则每 N 步跑一次 validation；没有 val 集时自动忽略')
    ap.add_argument('--val-batch-size', type=int, default=0,
                    help='验证时全局 batch size；0 表示继承 --bs')
    ap.add_argument('--val-max-batches', type=int, default=0,
                    help='验证最多跑多少个 batch；0=全量验证')
    ap.add_argument('--save', default='',
                    help='最终 ckpt 路径；周期性/紧急 ckpt 都放它的同目录')
    ap.add_argument('--save-every', type=int, default=500,
                    help='>0 则每 N 步存一份 step-suffix ckpt')
    ap.add_argument('--keep-last', type=int, default=3,
                    help='周期 ckpt 只保留最近 K 份；0=保留全部')
    ap.add_argument('--resume', default='', help='resume from ckpt path')
    ap.add_argument('--seed', type=int, default=0)
    # 加速
    ap.add_argument('--bf16', action='store_true', default=True)
    ap.add_argument('--no-bf16', dest='bf16', action='store_false')
    ap.add_argument('--compile', action='store_true', default=False)
    ap.add_argument('--num-threads', type=int, default=32)
    ap.add_argument('--gpus', type=int, default=0,
                    help='使用的 GPU 数量；0=自动使用全部可见 GPU')
    # mispred
    ap.add_argument('--mispred-pos-weight', type=float, default=-1.0,
                    help='-1=自动；正数=手动；0=不加权')
    ap.add_argument('--mispred-focal-gamma', type=float, default=0.0)
    ap.add_argument('--head-pos-weight', type=float, default=-1.0,
                    help='-1=自动；正数=手动；0=不加权')
    ap.add_argument('--tail-p95-pos-weight', type=float, default=-1.0,
                    help='-1=按训练集 tail rate 自动；正数=手动；0=不加权')
    ap.add_argument('--tail-p99-pos-weight', type=float, default=-1.0,
                    help='-1=按训练集 tail rate 自动；正数=手动；0=不加权')
    ap.add_argument('--fetch-tail-p95-pos-weight', type=float, default=-1.0,
                    help='-1=按训练集 fetch tail rate 自动；正数=手动；0=不加权')
    ap.add_argument('--fetch-tail-p99-pos-weight', type=float, default=-1.0,
                    help='-1=按训练集 fetch tail rate 自动；正数=手动；0=不加权')
    ap.add_argument('--w-tail-bce', type=float, default=0.15,
                    help='p95/p99 exceedance BCE 辅助损失权重')
    ap.add_argument('--w-tail-mae', type=float, default=0.05,
                    help='真实 tail 样本上的 normalized raw-cycle MAE 权重')
    ap.add_argument('--w-fetch-tail-bce', type=float, default=0.15,
                    help='fetch p95/p99 exceedance BCE 辅助损失权重')
    ap.add_argument('--w-fetch-tail-mae', type=float, default=0.05,
                    help='fetch 真实 tail 样本上的 normalized raw-cycle MAE 权重')
    ap.add_argument('--w-fetch-decomp', type=float, default=0.1,
                    help='fetch base/after-mispred/residual 分解辅助监督权重')
    ap.add_argument('--w-fetch-decomp-cons', type=float, default=0.0,
                    help='fetch 分解分量和与 fetch_total 一致性损失权重')
    return ap.parse_args()


def lr_lambda(step, warmup, total):
    if step < warmup:
        return step / max(1, warmup)
    p = (step - warmup) / max(1, total - warmup)
    return 0.1 + 0.9 * 0.5 * (1 + math.cos(math.pi * min(1.0, p)))


# =====================================================================
# Helpers
# =====================================================================
def estimate_mispred_pos_weight(data_root: str) -> float:
    import glob
    import pyarrow.parquet as pq
    files = sorted(glob.glob(os.path.join(data_root, 'workload=*/part-*.parquet')))
    n_pos = n_neg = 0
    for f in files:
        tbl = pq.read_table(
            f, columns=['mispredicted', 'is_branch', 'is_microop', 'is_last_microop'])
        arr = tbl['mispredicted'].to_numpy()
        mask = (
            (tbl['is_branch'].to_numpy() > 0)
            & ((tbl['is_last_microop'].to_numpy() > 0)
               | (tbl['is_microop'].to_numpy() == 0))
        )
        arr = arr[mask]
        p = int((arr > 0).sum())
        n_pos += p
        n_neg += len(arr) - p
    return (n_neg / n_pos) if n_pos else 1.0


def resolve_data_roots(data_root: str, train_data: str, val_data: str) -> tuple[str, str | None]:
    train_root = train_data or data_root
    val_root = val_data or ''
    if not train_data:
        cand_train = os.path.join(data_root, 'train')
        cand_val = os.path.join(data_root, 'val')
        if os.path.isdir(cand_train):
            train_root = cand_train
            if os.path.isdir(cand_val):
                val_root = cand_val
    if val_root and not os.path.isdir(val_root):
        raise FileNotFoundError(f'验证集目录不存在: {val_root}')
    if not os.path.isdir(train_root):
        raise FileNotFoundError(f'训练集目录不存在: {train_root}')
    return train_root, (val_root or None)


def probe_schema_cacheline_paddr(data_root: str) -> tuple[Path | None, bool | None]:
    try:
        sample_pq = next(Path(data_root).rglob('*.parquet'))
        import pyarrow.parquet as _pq
        cols = set(_pq.read_metadata(sample_pq).schema.to_arrow_schema().names)
        return sample_pq, ('cacheline_paddr' in cols)
    except StopIteration:
        return None, None


def run_validation(model, base_model, loader, dataset, device: torch.device, amp_ctx,
                   max_batches: int = 0) -> dict:
    was_training = model.training
    model.eval()
    workloads, p95_thr_cpu, p99_thr_cpu = tail_threshold_arrays(dataset)
    n_workloads = len(workloads)
    p95_thr = p95_thr_cpu.to(device)
    p99_thr = p99_thr_cpu.to(device)
    sums = torch.zeros(31, device=device, dtype=torch.float64)
    p95_mae_sum = torch.zeros(n_workloads, device=device, dtype=torch.float64)
    p95_count = torch.zeros(n_workloads, device=device, dtype=torch.float64)
    p95_tp = torch.zeros(n_workloads, device=device, dtype=torch.float64)
    p95_fp = torch.zeros(n_workloads, device=device, dtype=torch.float64)
    p95_fn = torch.zeros(n_workloads, device=device, dtype=torch.float64)
    p99_mae_sum = torch.zeros(n_workloads, device=device, dtype=torch.float64)
    p99_count = torch.zeros(n_workloads, device=device, dtype=torch.float64)
    p99_tp = torch.zeros(n_workloads, device=device, dtype=torch.float64)
    p99_fp = torch.zeros(n_workloads, device=device, dtype=torch.float64)
    p99_fn = torch.zeros(n_workloads, device=device, dtype=torch.float64)

    with torch.no_grad():
        for batch_idx, batch in enumerate(loader):
            if max_batches > 0 and batch_idx >= max_batches:
                break
            feat = {k: v.to(device, non_blocking=True) for k, v in batch['feat'].items()}
            batch_dev = {
                'feat': feat,
                'attn_mask': batch['attn_mask'].to(device, non_blocking=True),
                'fetch_lat': batch['fetch_lat'].to(device, non_blocking=True),
                'fetch_lat_raw': batch['fetch_lat_raw'].to(device, non_blocking=True),
                'fetch_base': batch['fetch_base'].to(device, non_blocking=True),
                'fetch_after_mispred': batch['fetch_after_mispred'].to(device, non_blocking=True),
                'fetch_residual_tail': batch['fetch_residual_tail'].to(device, non_blocking=True),
                'fetch_tail_p95': batch['fetch_tail_p95'].to(device, non_blocking=True),
                'fetch_tail_p99': batch['fetch_tail_p99'].to(device, non_blocking=True),
                'fetch_tail_p95_thr': batch['fetch_tail_p95_thr'].to(device, non_blocking=True),
                'fetch_tail_p99_thr': batch['fetch_tail_p99_thr'].to(device, non_blocking=True),
                'fetch_after_mispred_mask': batch['fetch_after_mispred_mask'].to(device, non_blocking=True),
                'fetch_residual_tail_mask': batch['fetch_residual_tail_mask'].to(device, non_blocking=True),
                'exec_lat': batch['exec_lat'].to(device, non_blocking=True),
                'exec_lat_raw': batch['exec_lat_raw'].to(device, non_blocking=True),
                'exec_bucket': batch['exec_bucket'].to(device, non_blocking=True),
                'exec_residual': batch['exec_residual'].to(device, non_blocking=True),
                'exec_tail_p95': batch['exec_tail_p95'].to(device, non_blocking=True),
                'exec_tail_p99': batch['exec_tail_p99'].to(device, non_blocking=True),
                'exec_tail_p95_thr': batch['exec_tail_p95_thr'].to(device, non_blocking=True),
                'exec_tail_p99_thr': batch['exec_tail_p99_thr'].to(device, non_blocking=True),
                'mispred': batch['mispred'].to(device, non_blocking=True),
                'mispred_mask': batch['mispred_mask'].to(device, non_blocking=True),
                'head': batch['head'].to(device, non_blocking=True),
                'workload_id': batch['workload_id'].to(device, non_blocking=True),
            }
            with amp_ctx():
                out = model(batch_dev)
                losses = base_model.compute_loss(batch_dev, out)

            bs = int(batch_dev['fetch_lat'].shape[0])
            sums[0] += bs
            sums[1] += losses['loss'].to(torch.float64) * bs
            sums[2] += losses['mse_fetch'].to(torch.float64) * bs
            sums[3] += losses['mse_fetch_cons'].to(torch.float64) * bs
            sums[4] += losses['huber_exec'].to(torch.float64) * bs
            sums[5] += losses['ce_exec_bucket'].to(torch.float64) * bs
            sums[6] += losses['huber_exec_residual'].to(torch.float64) * bs
            sums[7] += losses['pinball_exec_quantile'].to(torch.float64) * bs
            sums[22] += losses['bce_tail'].to(torch.float64) * bs
            sums[23] += losses['tail_raw_mae'].to(torch.float64) * bs
            sums[24] += losses['bce_fetch_tail'].to(torch.float64) * bs
            sums[25] += losses['fetch_tail_raw_mae'].to(torch.float64) * bs
            sums[26] += losses['fetch_decomp'].to(torch.float64) * bs
            sums[27] += losses['fetch_decomp_cons'].to(torch.float64) * bs
            sums[9] += losses['bce_head'].to(torch.float64) * bs

            pred_m = (torch.sigmoid(out['mispred_logit']) > 0.5).to(torch.int32)
            gold_m = (batch_dev['mispred'] > 0.5).to(torch.int32)
            valid_m = batch_dev['mispred_mask'] > 0.5
            valid_m_count = int(valid_m.sum())
            sums[8] += losses['bce_mispred'].to(torch.float64) * valid_m_count
            sums[12] += valid_m_count
            pred_m = pred_m[valid_m]
            gold_m = gold_m[valid_m]
            head_prob = torch.sigmoid(out['head_logit'])
            pred_h = (torch.sigmoid(out['head_logit']) > 0.5).to(torch.int32)
            gold_h = (batch_dev['fetch_lat'] > 0.0).to(torch.int32)
            pred_fetch_raw = torch.expm1(torch.clamp(out['fetch_lat'], min=0.0)) * head_prob
            pred_fetch = torch.log1p(pred_fetch_raw.clamp(min=0.0))
            sums[10] += torch.abs(
                pred_fetch - batch_dev['fetch_lat']).to(torch.float64).sum()
            sums[11] += torch.abs(out['exec_lat'] - batch_dev['exec_lat']).to(torch.float64).sum()

            sums[13] += ((pred_m == 1) & (gold_m == 1)).to(torch.float64).sum()
            sums[14] += ((pred_m == 0) & (gold_m == 0)).to(torch.float64).sum()
            sums[15] += ((pred_m == 1) & (gold_m == 0)).to(torch.float64).sum()
            sums[16] += ((pred_m == 0) & (gold_m == 1)).to(torch.float64).sum()
            sums[17] += ((pred_h == 1) & (gold_h == 1)).to(torch.float64).sum()
            sums[18] += ((pred_h == 0) & (gold_h == 0)).to(torch.float64).sum()
            sums[19] += ((pred_h == 1) & (gold_h == 0)).to(torch.float64).sum()
            sums[20] += ((pred_h == 0) & (gold_h == 1)).to(torch.float64).sum()
            sums[21] += bs

            pred_exec_cyc = torch.expm1(torch.clamp(out['exec_lat'], min=0.0))
            gold_exec_cyc = batch_dev['exec_lat_raw'].to(torch.float64)
            abs_err_cyc = torch.abs(pred_exec_cyc.to(torch.float64) - gold_exec_cyc)
            wid = batch_dev['workload_id']

            for thr, mae_sum, tail_count, tp, fp, fn in (
                (p95_thr, p95_mae_sum, p95_count, p95_tp, p95_fp, p95_fn),
                (p99_thr, p99_mae_sum, p99_count, p99_tp, p99_fp, p99_fn),
            ):
                tail_thr = thr[wid]
                gold_tail = gold_exec_cyc >= tail_thr
                pred_tail = pred_exec_cyc.to(torch.float64) >= tail_thr
                ones = torch.ones_like(wid, dtype=torch.float64)
                if torch.any(gold_tail):
                    mae_sum.index_add_(0, wid[gold_tail], abs_err_cyc[gold_tail])
                    tail_count.index_add_(0, wid[gold_tail], ones[gold_tail])
                if torch.any(pred_tail & gold_tail):
                    tp.index_add_(0, wid[pred_tail & gold_tail], ones[pred_tail & gold_tail])
                if torch.any(pred_tail & ~gold_tail):
                    fp.index_add_(0, wid[pred_tail & ~gold_tail], ones[pred_tail & ~gold_tail])
                if torch.any((~pred_tail) & gold_tail):
                    fn.index_add_(0, wid[(~pred_tail) & gold_tail], ones[(~pred_tail) & gold_tail])

    if dist_is_initialized():
        dist.all_reduce(sums, op=dist.ReduceOp.SUM)
        for t in (p95_mae_sum, p95_count, p95_tp, p95_fp, p95_fn,
                  p99_mae_sum, p99_count, p99_tp, p99_fp, p99_fn):
            dist.all_reduce(t, op=dist.ReduceOp.SUM)
    total_g = max(float(sums[0].item()), 1.0)
    tp_m, tn_m, fp_m, fn_m = [float(sums[i].item()) for i in range(13, 17)]
    tp_h, tn_h, fp_h, fn_h = [float(sums[i].item()) for i in range(17, 21)]
    total_m = max(tp_m + tn_m + fp_m + fn_m, 1.0)
    total_h = max(tp_h + tn_h + fp_h + fn_h, 1.0)
    m_prec = tp_m / max(tp_m + fp_m, 1.0)
    m_rec = tp_m / max(tp_m + fn_m, 1.0)
    h_prec = tp_h / max(tp_h + fp_h, 1.0)
    h_rec = tp_h / max(tp_h + fn_h, 1.0)
    p95_by_workload, p95_overall = summarize_tail_metrics(
        workloads, p95_mae_sum, p95_count, p95_tp, p95_fp, p95_fn)
    p99_by_workload, p99_overall = summarize_tail_metrics(
        workloads, p99_mae_sum, p99_count, p99_tp, p99_fp, p99_fn)
    if was_training:
        model.train()
    return {
        'rows': int(total_g),
        'loss': float(sums[1].item() / total_g),
        'mse_fetch_pos': float(sums[2].item() / total_g),
        'mse_fetch_cons': float(sums[3].item() / total_g),
        'huber_exec': float(sums[4].item() / total_g),
        'ce_exec_bucket': float(sums[5].item() / total_g),
        'huber_exec_residual': float(sums[6].item() / total_g),
        'pinball_exec_quantile': float(sums[7].item() / total_g),
        'bce_tail': float(sums[22].item() / total_g),
        'tail_raw_mae': float(sums[23].item() / total_g),
        'bce_fetch_tail': float(sums[24].item() / total_g),
        'fetch_tail_raw_mae': float(sums[25].item() / total_g),
        'fetch_decomp': float(sums[26].item() / total_g),
        'fetch_decomp_cons': float(sums[27].item() / total_g),
        'bce_mispred': float(sums[8].item() / max(float(sums[12].item()), 1.0)),
        'bce_head': float(sums[9].item() / total_g),
        'mae_fetch_log': float(sums[10].item() / total_g),
        'mae_exec_log': float(sums[11].item() / total_g),
        'branch_mispred_eval_count': int(tp_m + tn_m + fp_m + fn_m),
        'true_branch_mispred_count': int(tp_m + fn_m),
        'pred_branch_mispred_count': int(tp_m + fp_m),
        'branch_mispred_count_abs_error': abs((tp_m + fp_m) - (tp_m + fn_m)),
        'branch_mispred_count_rel_error': (
            abs((tp_m + fp_m) - (tp_m + fn_m)) / max(tp_m + fn_m, 1.0)),
        'mispred_acc': (tp_m + tn_m) / total_m,
        'mispred_precision': m_prec,
        'mispred_recall': m_rec,
        'mispred_f1': (2.0 * m_prec * m_rec) / max(m_prec + m_rec, 1e-12),
        'head_acc': (tp_h + tn_h) / total_h,
        'head_precision': h_prec,
        'head_recall': h_rec,
        'head_f1': (2.0 * h_prec * h_rec) / max(h_prec + h_rec, 1e-12),
        'tail_p95': {'overall': p95_overall, 'by_workload': p95_by_workload},
        'tail_p99': {'overall': p99_overall, 'by_workload': p99_by_workload},
    }


def binary_stats_from_logits(logits: torch.Tensor, target: torch.Tensor) -> dict:
    """从二分类 logits 计算 acc / precision / recall / f1。"""
    pred = (torch.sigmoid(logits) > 0.5).to(torch.int32)
    gold = (target > 0.5).to(torch.int32)
    tp = int(((pred == 1) & (gold == 1)).sum())
    tn = int(((pred == 0) & (gold == 0)).sum())
    fp = int(((pred == 1) & (gold == 0)).sum())
    fn = int(((pred == 0) & (gold == 1)).sum())
    total = max(tp + tn + fp + fn, 1)
    acc = (tp + tn) / total
    prec = tp / max(tp + fp, 1)
    rec = tp / max(tp + fn, 1)
    f1 = (2.0 * prec * rec) / max(prec + rec, 1e-12)
    return {
        'acc': acc,
        'precision': prec,
        'recall': rec,
        'f1': f1,
        'tp': tp, 'tn': tn, 'fp': fp, 'fn': fn,
    }


def atomic_torch_save(state: dict, path: str) -> None:
    """先写到 .tmp 再 rename，避免崩溃时留半截 ckpt。"""
    tmp = path + '.tmp'
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    torch.save(state, tmp)
    os.replace(tmp, path)


def write_status(status_path: str, payload: dict) -> None:
    tmp = status_path + '.tmp'
    Path(status_path).parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, 'w') as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, status_path)


def prune_periodic_ckpts(save_dir: Path, base_name: str, keep_last: int) -> None:
    if keep_last <= 0:
        return
    cks = sorted(save_dir.glob(f'{base_name}.step*.pt'),
                 key=lambda p: p.stat().st_mtime)
    for old in cks[:-keep_last]:
        try:
            old.unlink()
        except OSError:
            pass


def collect_state(model, optim, sched, step, ema_loss, best_loss, cfg, args, rng):
    if isinstance(model, (torch.nn.DataParallel, DDP)):
        model_to_save = model.module
    else:
        model_to_save = model
    return {
        'model': model_to_save.state_dict(),
        'optim': optim.state_dict(),
        'sched': sched.state_dict(),
        'step': step,
        'ema_loss': ema_loss,
        'best_loss': best_loss,
        'cfg': cfg.__dict__,
        'args': vars(args),
        'rng': rng,
    }


def snapshot_rng() -> dict:
    return {
        'torch_cpu': torch.get_rng_state(),
        'numpy': np.random.get_state(),
        'python': random.getstate(),
    }


def restore_rng(state: dict) -> None:
    if not state:
        return
    if 'torch_cpu' in state:
        rng_state = state['torch_cpu']
        if isinstance(rng_state, torch.Tensor):
            rng_state = rng_state.detach().cpu().to(torch.uint8)
        elif isinstance(rng_state, np.ndarray):
            rng_state = torch.from_numpy(
                rng_state.astype(np.uint8, copy=False)).cpu()
        else:
            rng_state = torch.as_tensor(rng_state, dtype=torch.uint8).cpu()
        torch.set_rng_state(rng_state)
    if 'numpy' in state:
        np.random.set_state(state['numpy'])
    if 'python' in state:
        random.setstate(state['python'])


def reduce_mean_tensor(x: torch.Tensor) -> torch.Tensor:
    if dist_is_initialized():
        y = x.clone()
        dist.all_reduce(y, op=dist.ReduceOp.SUM)
        y /= get_world_size()
        return y
    return x


def reduce_binary_counts(tp: int, tn: int, fp: int, fn: int, device: torch.device) -> dict:
    counts = torch.tensor([tp, tn, fp, fn], device=device, dtype=torch.float64)
    if dist_is_initialized():
        dist.all_reduce(counts, op=dist.ReduceOp.SUM)
    tp_f, tn_f, fp_f, fn_f = [float(v) for v in counts.tolist()]
    total = max(tp_f + tn_f + fp_f + fn_f, 1.0)
    acc = (tp_f + tn_f) / total
    prec = tp_f / max(tp_f + fp_f, 1.0)
    rec = tp_f / max(tp_f + fn_f, 1.0)
    f1 = (2.0 * prec * rec) / max(prec + rec, 1e-12)
    return {
        'acc': acc,
        'precision': prec,
        'recall': rec,
        'f1': f1,
        'tp': int(tp_f), 'tn': int(tn_f), 'fp': int(fp_f), 'fn': int(fn_f),
    }


def binary_metrics(tp: float, fp: float, fn: float) -> dict:
    prec = tp / max(tp + fp, 1.0)
    rec = tp / max(tp + fn, 1.0)
    f1 = (2.0 * prec * rec) / max(prec + rec, 1e-12)
    return {'precision': prec, 'recall': rec, 'f1': f1}


def tail_threshold_arrays(dataset) -> tuple[list[str], torch.Tensor, torch.Tensor]:
    report = dataset.latency_quantiles()
    by_workload = report.get('by_workload', {})
    workloads = list(dataset.workloads)
    p95 = []
    p99 = []
    for w in workloads:
        exec_q = (by_workload.get(w) or {}).get('execution_latency', {})
        p95.append(float(exec_q.get('0.95', exec_q.get('0.99', 0.0))))
        p99.append(float(exec_q.get('0.99', exec_q.get('0.95', 0.0))))
    return workloads, torch.tensor(p95, dtype=torch.float64), torch.tensor(p99, dtype=torch.float64)


def summarize_tail_metrics(workloads: list[str], mae_sum: torch.Tensor, count: torch.Tensor,
                           tp: torch.Tensor, fp: torch.Tensor, fn: torch.Tensor) -> tuple[dict, dict]:
    by_workload = {}
    for i, w in enumerate(workloads):
        tp_i = float(tp[i].item())
        fp_i = float(fp[i].item())
        fn_i = float(fn[i].item())
        count_i = float(count[i].item())
        mae_i = float(mae_sum[i].item())
        m = binary_metrics(tp_i, fp_i, fn_i)
        by_workload[w] = {
            'mae_exec_cycle': mae_i / max(count_i, 1.0),
            'tail_count': int(count_i),
            'precision': m['precision'],
            'recall': m['recall'],
            'f1': m['f1'],
        }
    overall = binary_metrics(float(tp.sum().item()),
                             float(fp.sum().item()),
                             float(fn.sum().item()))
    overall['mae_exec_cycle'] = float(mae_sum.sum().item()) / max(float(count.sum().item()), 1.0)
    overall['tail_count'] = int(count.sum().item())
    return by_workload, overall


# =====================================================================
# Main
# =====================================================================
def main():
    args = get_args()

    ddp = int(os.environ.get('WORLD_SIZE', '1')) > 1
    local_rank = int(os.environ.get('LOCAL_RANK', '0'))
    if ddp:
        if not torch.cuda.is_available():
            raise RuntimeError('DDP 需要 CUDA 环境')
        torch.cuda.set_device(local_rank)
        dist.init_process_group(backend='nccl', init_method='env://')
    rank = get_rank()
    world_size = get_world_size()
    main_proc = is_main_process()

    save_path = args.save
    if not save_path:
        ts = time.strftime('%Y%m%d_%H%M%S')
        ckpt_root = os.environ.get('TAO_CKPT_ROOT', os.path.join(os.getcwd(), 'ckpt'))
        save_path = os.path.join(ckpt_root, f'tao_{ts}.pt')
    save_dir = Path(save_path).parent
    save_dir.mkdir(parents=True, exist_ok=True)
    base_name = Path(save_path).stem
    status_path = str(save_dir / f'{base_name}.status.json')
    log_path = str(save_dir / f'{base_name}.log')

    setup_logging(log_path if main_proc else None,
                  logging.INFO if main_proc else logging.WARNING)

    log.info('=' * 64)
    log.info('args: %s', json.dumps(vars(args), indent=2))
    log.info('save : %s', save_path)
    log.info('log  : %s', log_path)
    log.info('stat : %s', status_path)
    log.info('dist : ddp=%s rank=%d world=%d local_rank=%d', ddp, rank, world_size, local_rank)
    log.info('=' * 64)

    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    random.seed(args.seed)
    threads_this_rank = max(1, args.num_threads // world_size) if ddp else args.num_threads
    torch.set_num_threads(threads_this_rank)

    log.info('[env] OMP=%s MKL=%s torch.threads=%d mkldnn=%s',
             os.environ.get('OMP_NUM_THREADS'),
             os.environ.get('MKL_NUM_THREADS'),
             torch.get_num_threads(),
             torch.backends.mkldnn.is_available())

    train_root, val_root = resolve_data_roots(args.data, args.train_data, args.val_data)
    log.info('[init] data roots: train=%s val=%s', train_root, val_root or '<disabled>')
    log.info('[init] loading train dataset from %s', train_root)
    t0 = time.time()
    spec = DatasetSpec(root=train_root, context_len=args.ctx)
    ds = ParquetWindowDataset(spec)
    log.info('[init] train dataset rows=%d loaded in %.2fs', len(ds), time.time() - t0)
    val_ds = None
    if val_root:
        t0 = time.time()
        val_ds = ParquetWindowDataset(DatasetSpec(root=val_root, context_len=args.ctx))
        log.info('[init] val dataset rows=%d loaded in %.2fs', len(val_ds), time.time() - t0)
    # V10 schema sanity: cacheline_paddr 是否存在；缺失 => 旧 50M 数据，
    # COMPAT-OLD-50M fallback 路径生效，仅打 warning 不阻断训练。
    try:
        sample_pq, has_cacheline_paddr = probe_schema_cacheline_paddr(train_root)
        if sample_pq is None:
            log.warning('[init] no parquet found under %s', train_root)
        elif has_cacheline_paddr:
            log.info('[init] schema OK: cacheline_paddr present (V10 dataset)')
        else:
            log.warning('[init] cacheline_paddr MISSING in %s -> COMPAT-OLD-50M '
                        'fallback (cline_p_bucket==cline_bucket)', sample_pq)
    except Exception as _e:
        log.warning('[init] schema probe failed: %s', _e)
    vocabs = ds.num_features()
    log.info('[init] vocabs=%s', vocabs)
    pos_rates = ds.label_positive_rates()
    log.info('[init] train positive rates=%s', pos_rates)
    if val_ds is not None:
        log.info('[init] val positive rates=%s', val_ds.label_positive_rates())

    if args.mispred_pos_weight < 0:
        t0 = time.time()
        pw = estimate_mispred_pos_weight(train_root)
        log.info('[init] auto mispred_pos_weight = %.2f (scan %.2fs)', pw, time.time() - t0)
    else:
        pw = float(args.mispred_pos_weight)
        log.info('[init] manual mispred_pos_weight = %.2f', pw)

    if args.head_pos_weight < 0:
        head_rate = float(pos_rates.get('is_fetch_group_head', 0.0))
        head_pw = ((1.0 - head_rate) / head_rate) if head_rate > 0 else 1.0
        log.info('[init] auto head_pos_weight = %.2f (pos_rate=%.4f)', head_pw, head_rate)
    else:
        head_pw = float(args.head_pos_weight)
        log.info('[init] manual head_pos_weight = %.2f', head_pw)

    if args.tail_p95_pos_weight < 0:
        tail95_rate = float(pos_rates.get('exec_tail_p95', 0.0))
        tail95_pw = ((1.0 - tail95_rate) / tail95_rate) if tail95_rate > 0 else 1.0
        log.info('[init] auto tail_p95_pos_weight = %.2f (pos_rate=%.4f)', tail95_pw, tail95_rate)
    else:
        tail95_pw = float(args.tail_p95_pos_weight)
        log.info('[init] manual tail_p95_pos_weight = %.2f', tail95_pw)

    if args.tail_p99_pos_weight < 0:
        tail99_rate = float(pos_rates.get('exec_tail_p99', 0.0))
        tail99_pw = ((1.0 - tail99_rate) / tail99_rate) if tail99_rate > 0 else 1.0
        log.info('[init] auto tail_p99_pos_weight = %.2f (pos_rate=%.4f)', tail99_pw, tail99_rate)
    else:
        tail99_pw = float(args.tail_p99_pos_weight)
        log.info('[init] manual tail_p99_pos_weight = %.2f', tail99_pw)

    if args.fetch_tail_p95_pos_weight < 0:
        fetch_tail95_rate = float(pos_rates.get('fetch_tail_p95', 0.0))
        fetch_tail95_pw = ((1.0 - fetch_tail95_rate) / fetch_tail95_rate) if fetch_tail95_rate > 0 else 1.0
        log.info('[init] auto fetch_tail_p95_pos_weight = %.2f (pos_rate=%.4f)',
                 fetch_tail95_pw, fetch_tail95_rate)
    else:
        fetch_tail95_pw = float(args.fetch_tail_p95_pos_weight)
        log.info('[init] manual fetch_tail_p95_pos_weight = %.2f', fetch_tail95_pw)

    if args.fetch_tail_p99_pos_weight < 0:
        fetch_tail99_rate = float(pos_rates.get('fetch_tail_p99', 0.0))
        fetch_tail99_pw = ((1.0 - fetch_tail99_rate) / fetch_tail99_rate) if fetch_tail99_rate > 0 else 1.0
        log.info('[init] auto fetch_tail_p99_pos_weight = %.2f (pos_rate=%.4f)',
                 fetch_tail99_pw, fetch_tail99_rate)
    else:
        fetch_tail99_pw = float(args.fetch_tail_p99_pos_weight)
        log.info('[init] manual fetch_tail_p99_pos_weight = %.2f', fetch_tail99_pw)

    cfg = TaoConfig(
        context_len=args.ctx,
        mispred_pos_weight=pw,
        mispred_focal_gamma=args.mispred_focal_gamma,
        head_pos_weight=head_pw,
        fetch_tail_p95_pos_weight=fetch_tail95_pw,
        fetch_tail_p99_pos_weight=fetch_tail99_pw,
        tail_p95_pos_weight=tail95_pw,
        tail_p99_pos_weight=tail99_pw,
        w_fetch_tail_bce=float(args.w_fetch_tail_bce),
        w_fetch_tail_mae=float(args.w_fetch_tail_mae),
        w_fetch_decomp=float(args.w_fetch_decomp),
        w_fetch_decomp_cons=float(args.w_fetch_decomp_cons),
        w_tail_bce=float(args.w_tail_bce),
        w_tail_mae=float(args.w_tail_mae),
    )
    if ddp:
        device = torch.device('cuda', local_rank)
        gpu_count = torch.cuda.device_count()
        requested_gpus = world_size
        if args.gpus > 0 and args.gpus != world_size and main_proc:
            log.warning('[init] DDP world_size=%d but --gpus=%d; 以 world_size 为准',
                        world_size, args.gpus)
    else:
        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        gpu_count = torch.cuda.device_count() if device.type == 'cuda' else 0
        requested_gpus = gpu_count if args.gpus <= 0 else min(args.gpus, gpu_count)

    global_bs = int(args.bs)
    if ddp:
        if global_bs % world_size != 0:
            raise ValueError(f'全局 batch {global_bs} 不能被 world_size={world_size} 整除')
        per_rank_bs = global_bs // world_size
    else:
        per_rank_bs = global_bs
    val_global_bs = int(args.val_batch_size) if int(args.val_batch_size) > 0 else global_bs
    if ddp:
        if val_global_bs % world_size != 0:
            raise ValueError(f'验证 batch {val_global_bs} 不能被 world_size={world_size} 整除')
        val_per_rank_bs = val_global_bs // world_size
    else:
        val_per_rank_bs = val_global_bs
    val_workers = args.workers if int(args.val_workers) < 0 else int(args.val_workers)

    base_model = TaoCoreTransformer(cfg).to(device)
    model = base_model
    log.info('[init] model #params=%.2fM device=%s bf16=%s compile=%s focal_gamma=%.2f gpus=%d/%d global_bs=%d per_rank_bs=%d',
             base_model.num_params() / 1e6, device, args.bf16, args.compile,
             args.mispred_focal_gamma, requested_gpus, gpu_count, global_bs, per_rank_bs)

    if args.compile:
        try:
            base_model = torch.compile(base_model, mode='reduce-overhead')
            model = base_model
            log.info('[init] torch.compile enabled on base model')
        except Exception as e:
            log.warning('[init] torch.compile failed: %s; eager fallback', e)

    if ddp:
        model = DDP(base_model, device_ids=[local_rank], output_device=local_rank,
                    broadcast_buffers=False, find_unused_parameters=False)

    sampler = DistributedSampler(ds, num_replicas=world_size, rank=rank,
                                 shuffle=True, drop_last=True) if ddp else None
    loader = DataLoader(
        ds,
        batch_size=per_rank_bs,
        shuffle=(sampler is None),
        sampler=sampler,
        num_workers=args.workers,
        pin_memory=(device.type == 'cuda'),
        persistent_workers=(args.workers > 0),
        collate_fn=collate,
        drop_last=True,
    )
    val_sampler = None
    val_loader = None
    if val_ds is not None:
        val_sampler = DistributedSampler(
            val_ds, num_replicas=world_size, rank=rank, shuffle=False, drop_last=False
        ) if ddp else None
        val_loader = DataLoader(
            val_ds,
            batch_size=val_per_rank_bs,
            shuffle=False,
            sampler=val_sampler,
            num_workers=val_workers,
            pin_memory=(device.type == 'cuda'),
            persistent_workers=(val_workers > 0),
            collate_fn=collate,
            drop_last=False,
        )
        log.info('[init] validation enabled: rows=%d global_bs=%d per_rank_bs=%d every=%d max_batches=%d',
                 len(val_ds), val_global_bs, val_per_rank_bs, args.val_every, args.val_max_batches)
    else:
        log.info('[init] validation disabled')

    optim = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=args.wd,
                              betas=(0.9, 0.95))
    sched = torch.optim.lr_scheduler.LambdaLR(
        optim, lambda s: lr_lambda(s, args.warmup, args.steps))

    # bf16 在 CPU/CUDA 上都可用；GPU 上 autocast(device_type='cuda', dtype=bfloat16)
    # 是 A100/H100/4090 等 Ampere+ 架构的标准训练精度。
    use_amp = bool(args.bf16)
    amp_ctx = (lambda: torch.amp.autocast(device_type=device.type, dtype=torch.bfloat16)) \
        if use_amp else (lambda: torch.autocast(device_type=device.type, enabled=False))

    # ---- resume ----
    step = 0
    ema_loss = None
    best_loss = float('inf')
    if args.resume and os.path.exists(args.resume):
        log.info('[resume] loading %s', args.resume)
        ck = torch.load(args.resume, map_location=device, weights_only=False)
        base_model.load_state_dict(ck['model'])
        if 'optim' in ck:
            optim.load_state_dict(ck['optim'])
        if 'sched' in ck:
            sched.load_state_dict(ck['sched'])
        step = int(ck.get('step', 0))
        ema_loss = ck.get('ema_loss')
        best_loss = float(ck.get('best_loss', float('inf')))
        restore_rng(ck.get('rng') or {})
        log.info('[resume] step=%d ema_loss=%s best_loss=%s', step, ema_loss, best_loss)

    # ---- signal handlers ----
    stop_after_save = {'flag': False}

    def _emergency_save(tag: str) -> None:
        if not main_proc:
            return
        path = str(save_dir / f'{base_name}.{tag}.pt')
        try:
            atomic_torch_save(
                collect_state(model, optim, sched, step, ema_loss, best_loss,
                              cfg, args, snapshot_rng()),
                path,
            )
            log.warning('[signal] saved emergency ckpt -> %s', path)
        except Exception:
            log.error('[signal] save failed:\n%s', traceback.format_exc())

    def _on_sigusr1(signum, frame):
        _emergency_save(f'sigusr1.step{step}')

    def _on_term(signum, frame):
        log.warning('[signal] received signal %d, will save & exit after current step', signum)
        stop_after_save['flag'] = True

    signal.signal(signal.SIGUSR1, _on_sigusr1)
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    # ---- train loop ----
    model.train()
    t_start = time.time()
    last_t = t_start
    last_step = step
    last_save_step = step
    interval_data_t = 0.0
    interval_compute_t = 0.0
    interval_steps = 0
    status_extra: dict = {}

    def write_status_now(extra: dict | None = None):
        if not main_proc:
            return
        nonlocal status_extra
        payload = {
            'pid': os.getpid(),
            'step': step,
            'total_steps': args.steps,
            'ema_loss': float(ema_loss) if ema_loss is not None else None,
            'best_loss': best_loss if best_loss != float('inf') else None,
            'lr': float(sched.get_last_lr()[0]),
            'elapsed_s': time.time() - t_start,
            'updated_ts': time.time(),
            'train_data': train_root,
            'val_data': val_root,
        }
        payload.update(status_extra)
        if extra:
            status_extra.update(extra)
            payload.update(extra)
        write_status(status_path, payload)

    try:
        epoch = 0
        while step < args.steps:
            if sampler is not None:
                sampler.set_epoch(epoch)
            epoch += 1
            loader_it = iter(loader)
            while True:
                if step >= args.steps or stop_after_save['flag']:
                    break
                data_t0 = time.time()
                try:
                    batch = next(loader_it)
                except StopIteration:
                    break
                data_time = time.time() - data_t0
                feat = {k: v.to(device, non_blocking=True) for k, v in batch['feat'].items()}
                batch_dev = {
                    'feat': feat,
                    'attn_mask': batch['attn_mask'].to(device, non_blocking=True),
                    'fetch_lat': batch['fetch_lat'].to(device, non_blocking=True),
                    'fetch_lat_raw': batch['fetch_lat_raw'].to(device, non_blocking=True),
                    'fetch_base': batch['fetch_base'].to(device, non_blocking=True),
                    'fetch_after_mispred': batch['fetch_after_mispred'].to(device, non_blocking=True),
                    'fetch_residual_tail': batch['fetch_residual_tail'].to(device, non_blocking=True),
                    'fetch_tail_p95': batch['fetch_tail_p95'].to(device, non_blocking=True),
                    'fetch_tail_p99': batch['fetch_tail_p99'].to(device, non_blocking=True),
                    'fetch_tail_p95_thr': batch['fetch_tail_p95_thr'].to(device, non_blocking=True),
                    'fetch_tail_p99_thr': batch['fetch_tail_p99_thr'].to(device, non_blocking=True),
                    'fetch_after_mispred_mask': batch['fetch_after_mispred_mask'].to(device, non_blocking=True),
                    'fetch_residual_tail_mask': batch['fetch_residual_tail_mask'].to(device, non_blocking=True),
                    'exec_lat': batch['exec_lat'].to(device, non_blocking=True),
                    'exec_lat_raw': batch['exec_lat_raw'].to(device, non_blocking=True),
                    'exec_bucket': batch['exec_bucket'].to(device, non_blocking=True),
                    'exec_residual': batch['exec_residual'].to(device, non_blocking=True),
                    'exec_tail_p95': batch['exec_tail_p95'].to(device, non_blocking=True),
                    'exec_tail_p99': batch['exec_tail_p99'].to(device, non_blocking=True),
                    'exec_tail_p95_thr': batch['exec_tail_p95_thr'].to(device, non_blocking=True),
                    'exec_tail_p99_thr': batch['exec_tail_p99_thr'].to(device, non_blocking=True),
                    'mispred': batch['mispred'].to(device, non_blocking=True),
                    'mispred_mask': batch['mispred_mask'].to(device, non_blocking=True),
                    'head': batch['head'].to(device, non_blocking=True),
                    'workload_id': batch['workload_id'].to(device, non_blocking=True),
                }
                if device.type == 'cuda':
                    torch.cuda.synchronize(device)
                compute_t0 = time.time()
                with amp_ctx():
                    out = model(batch_dev)
                    losses = base_model.compute_loss(batch_dev, out)
                loss = losses['loss']
                if not torch.isfinite(loss):
                    log.error(
                        '[nan  ] non-finite loss before backward at step=%d: '
                        'loss=%s mse_f=%s huber_e=%s bce_m=%s',
                        step,
                        loss.detach(),
                        losses['mse_fetch'],
                        losses['huber_exec'],
                        losses['bce_mispred'],
                    )
                    raise RuntimeError(f'non-finite loss before backward at step {step}')
                optim.zero_grad(set_to_none=True)
                loss.backward()
                torch.nn.utils.clip_grad_norm_(
                    model.parameters(), 1.0, error_if_nonfinite=True)
                optim.step()
                sched.step()
                if device.type == 'cuda':
                    torch.cuda.synchronize(device)
                compute_time = time.time() - compute_t0

                li = float(reduce_mean_tensor(loss.detach()).item())
                ema_loss = li if ema_loss is None else 0.95 * ema_loss + 0.05 * li
                step += 1
                interval_data_t += data_time
                interval_compute_t += compute_time
                interval_steps += 1

                local_loss_t = torch.tensor([
                    float(losses['mse_fetch']),
                    float(losses['mse_fetch_cons']),
                    float(losses['huber_exec']),
                    float(losses['ce_exec_bucket']),
                    float(losses['huber_exec_residual']),
                    float(losses['pinball_exec_quantile']),
                    float(losses['bce_tail']),
                    float(losses['tail_raw_mae']),
                    float(losses['bce_fetch_tail']),
                    float(losses['fetch_tail_raw_mae']),
                    float(losses['fetch_decomp']),
                    float(losses['fetch_decomp_cons']),
                    float(losses['bce_mispred']),
                    float(losses['bce_head']),
                ], device=device, dtype=torch.float64)
                global_loss_t = reduce_mean_tensor(local_loss_t)
                pred_m = (torch.sigmoid(out['mispred_logit'].detach()) > 0.5).to(torch.int32)
                gold_m = (batch_dev['mispred'] > 0.5).to(torch.int32)
                valid_m = batch_dev['mispred_mask'] > 0.5
                pred_m = pred_m[valid_m]
                gold_m = gold_m[valid_m]
                mispred_stats = reduce_binary_counts(
                    int(((pred_m == 1) & (gold_m == 1)).sum()),
                    int(((pred_m == 0) & (gold_m == 0)).sum()),
                    int(((pred_m == 1) & (gold_m == 0)).sum()),
                    int(((pred_m == 0) & (gold_m == 1)).sum()),
                    device,
                )
                pred_h = (torch.sigmoid(out['head_logit'].detach()) > 0.5).to(torch.int32)
                gold_h = (batch_dev['head'] > 0.5).to(torch.int32)
                head_stats = reduce_binary_counts(
                    int(((pred_h == 1) & (gold_h == 1)).sum()),
                    int(((pred_h == 0) & (gold_h == 0)).sum()),
                    int(((pred_h == 1) & (gold_h == 0)).sum()),
                    int(((pred_h == 0) & (gold_h == 1)).sum()),
                    device,
                )

                if main_proc and (step % args.log_every == 0 or step == 1):
                    now = time.time()
                    dt = now - last_t
                    tput = (step - last_step) * global_bs / max(dt, 1e-6)
                    avg_data_ms = 1000.0 * interval_data_t / max(interval_steps, 1)
                    avg_compute_ms = 1000.0 * interval_compute_t / max(interval_steps, 1)
                    avg_step_ms = avg_data_ms + avg_compute_ms
                    last_t, last_step = now, step
                    interval_data_t = 0.0
                    interval_compute_t = 0.0
                    interval_steps = 0
                    log.info(
                        '[step %5d/%d] loss=%7.3f ema=%7.3f '
                        'mse_f_pos=%.3f mse_f_cons=%.3f huber_e=%.3f '
                        'ce_e=%.3f huber_res=%.3f pinball_q=%.3f '
                        'bce_tail=%.3f tail_mae=%.3f '
                        'bce_f_tail=%.3f f_tail_mae=%.3f f_decomp=%.3f f_cons=%.3f '
                        'bce_m=%.3f m_acc=%.3f m_f1=%.3f '
                        'bce_h=%.3f h_acc=%.3f h_p=%.3f h_r=%.3f h_f1=%.3f '
                        'lr=%.2e samples/s=%.1f step_ms=%.1f data_ms=%.1f compute_ms=%.1f',
                        step, args.steps, li, ema_loss,
                        float(global_loss_t[0]),
                        float(global_loss_t[1]),
                        float(global_loss_t[2]),
                        float(global_loss_t[3]),
                        float(global_loss_t[4]),
                        float(global_loss_t[5]),
                        float(global_loss_t[6]),
                        float(global_loss_t[7]),
                        float(global_loss_t[8]),
                        float(global_loss_t[9]),
                        float(global_loss_t[10]),
                        float(global_loss_t[11]),
                        float(global_loss_t[12]),
                        mispred_stats['acc'], mispred_stats['f1'],
                        float(global_loss_t[13]),
                        head_stats['acc'], head_stats['precision'],
                        head_stats['recall'], head_stats['f1'],
                        sched.get_last_lr()[0], tput, avg_step_ms, avg_data_ms, avg_compute_ms,
                    )
                    write_status_now({
                        'samples_per_sec': tput,
                        'step_time_ms': avg_step_ms,
                        'data_time_ms': avg_data_ms,
                        'compute_time_ms': avg_compute_ms,
                        'mse_fetch_pos': float(global_loss_t[0]),
                        'mse_fetch_cons': float(global_loss_t[1]),
                        'huber_exec': float(global_loss_t[2]),
                        'ce_exec_bucket': float(global_loss_t[3]),
                        'huber_exec_residual': float(global_loss_t[4]),
                        'pinball_exec_quantile': float(global_loss_t[5]),
                        'bce_tail': float(global_loss_t[6]),
                        'tail_raw_mae': float(global_loss_t[7]),
                        'bce_fetch_tail': float(global_loss_t[8]),
                        'fetch_tail_raw_mae': float(global_loss_t[9]),
                        'fetch_decomp': float(global_loss_t[10]),
                        'fetch_decomp_cons': float(global_loss_t[11]),
                        'mispred_acc': mispred_stats['acc'],
                        'mispred_f1': mispred_stats['f1'],
                        'head_acc': head_stats['acc'],
                        'head_precision': head_stats['precision'],
                        'head_recall': head_stats['recall'],
                        'head_f1': head_stats['f1'],
                    })

                if val_loader is not None and args.val_every > 0 and step % args.val_every == 0:
                    val_metrics = run_validation(
                        model, base_model, val_loader, val_ds, device, amp_ctx,
                        max_batches=args.val_max_batches)
                    if main_proc:
                        log.info(
                            '[val  %5d] loss=%.3f huber_e=%.3f ce_e=%.3f '
                            'huber_res=%.3f pinball_q=%.3f bce_tail=%.3f tail_mae=%.3f '
                            'bce_f_tail=%.3f f_tail_mae=%.3f f_decomp=%.3f f_cons=%.3f '
                            'mae_exec_log=%.3f p95_mae=%.1f p95_r=%.3f p95_p=%.3f '
                            'p99_mae=%.1f p99_r=%.3f p99_p=%.3f',
                            step,
                            val_metrics['loss'],
                            val_metrics['huber_exec'],
                            val_metrics['ce_exec_bucket'],
                            val_metrics['huber_exec_residual'],
                            val_metrics['pinball_exec_quantile'],
                            val_metrics['bce_tail'],
                            val_metrics['tail_raw_mae'],
                            val_metrics['bce_fetch_tail'],
                            val_metrics['fetch_tail_raw_mae'],
                            val_metrics['fetch_decomp'],
                            val_metrics['fetch_decomp_cons'],
                            val_metrics['mae_exec_log'],
                            val_metrics['tail_p95']['overall']['mae_exec_cycle'],
                            val_metrics['tail_p95']['overall']['recall'],
                            val_metrics['tail_p95']['overall']['precision'],
                            val_metrics['tail_p99']['overall']['mae_exec_cycle'],
                            val_metrics['tail_p99']['overall']['recall'],
                            val_metrics['tail_p99']['overall']['precision'],
                        )
                        write_status_now({
                            'val_loss': val_metrics['loss'],
                            'val_huber_exec': val_metrics['huber_exec'],
                            'val_ce_exec_bucket': val_metrics['ce_exec_bucket'],
                            'val_huber_exec_residual': val_metrics['huber_exec_residual'],
                            'val_pinball_exec_quantile': val_metrics['pinball_exec_quantile'],
                            'val_bce_tail': val_metrics['bce_tail'],
                            'val_tail_raw_mae': val_metrics['tail_raw_mae'],
                            'val_bce_fetch_tail': val_metrics['bce_fetch_tail'],
                            'val_fetch_tail_raw_mae': val_metrics['fetch_tail_raw_mae'],
                            'val_fetch_decomp': val_metrics['fetch_decomp'],
                            'val_fetch_decomp_cons': val_metrics['fetch_decomp_cons'],
                            'val_mae_exec_log': val_metrics['mae_exec_log'],
                            'val_tail_p95_mae_exec_cycle': val_metrics['tail_p95']['overall']['mae_exec_cycle'],
                            'val_tail_p95_recall': val_metrics['tail_p95']['overall']['recall'],
                            'val_tail_p95_precision': val_metrics['tail_p95']['overall']['precision'],
                            'val_tail_p99_mae_exec_cycle': val_metrics['tail_p99']['overall']['mae_exec_cycle'],
                            'val_tail_p99_recall': val_metrics['tail_p99']['overall']['recall'],
                            'val_tail_p99_precision': val_metrics['tail_p99']['overall']['precision'],
                        })

                # 周期性 ckpt（原子写 + 限量保留）
                if main_proc and args.save_every > 0 and step - last_save_step >= args.save_every:
                    last_save_step = step
                    ck_path = str(save_dir / f'{base_name}.step{step}.pt')
                    atomic_torch_save(
                        collect_state(model, optim, sched, step, ema_loss, best_loss,
                                      cfg, args, snapshot_rng()),
                        ck_path,
                    )
                    log.info('[ckpt ] periodic -> %s', ck_path)
                    prune_periodic_ckpts(save_dir, base_name, args.keep_last)

                # 最佳 ckpt：跟随周期性 checkpoint 节奏，避免 early phase 每步刷盘。
                best_due = (
                    step == 1
                    or args.save_every <= 0
                    or step % max(1, args.save_every) == 0
                    or step >= args.steps
                )
                if main_proc and best_due and ema_loss is not None and ema_loss < best_loss * 0.999:
                    best_loss = float(ema_loss)
                    best_path = str(save_dir / f'{base_name}.best.pt')
                    atomic_torch_save(
                        collect_state(model, optim, sched, step, ema_loss, best_loss,
                                      cfg, args, snapshot_rng()),
                        best_path,
                    )
                    log.info('[ckpt ] new best ema=%.4f -> %s', best_loss, best_path)

            if stop_after_save['flag']:
                break
    except Exception:
        log.error('[train] exception:\n%s', traceback.format_exc())
        raise
    finally:
        if dist_is_initialized():
            dist.barrier()
        # 不管正常 / 异常 / 信号退出，都保存 last.pt
        if main_proc:
            last_path = str(save_dir / f'{base_name}.last.pt')
            try:
                atomic_torch_save(
                    collect_state(model, optim, sched, step, ema_loss, best_loss,
                                  cfg, args, snapshot_rng()),
                    last_path,
                )
                log.info('[ckpt ] last -> %s', last_path)
            except Exception:
                log.error('[ckpt ] last save failed:\n%s', traceback.format_exc())

    elapsed = time.time() - t_start
    if main_proc:
        log.info('[done] %d steps in %.1fs avg samples/s=%.1f final ema_loss=%s best=%.4f',
                 step, elapsed,
                 step * global_bs / max(elapsed, 1e-6),
                 ema_loss, best_loss)

    # 最终命名 ckpt（与 args.save 一致）
    if main_proc and save_path:
        atomic_torch_save(
            collect_state(model, optim, sched, step, ema_loss, best_loss,
                          cfg, args, snapshot_rng()),
            save_path,
        )
        log.info('[ckpt ] final -> %s', save_path)
    write_status_now({'finished': True})
    if dist_is_initialized():
        dist.destroy_process_group()


if __name__ == '__main__':
    main()
