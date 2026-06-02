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
from torch.utils.data import DataLoader

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


# =====================================================================
# Args
# =====================================================================
def get_args():
    ap = argparse.ArgumentParser()
    ap.add_argument('--data', required=True,
                    help='训练数据目录（Hive 分区 parquet 根目录）。V10 schema '
                         '应包含 cacheline_paddr 列；启动期会校验，缺失会打 '
                         'warning 但不会硬失败（COMPAT-OLD-50M）。')
    ap.add_argument('--ctx', type=int, default=128)
    ap.add_argument('--bs', type=int, default=128)
    ap.add_argument('--steps', type=int, default=200)
    ap.add_argument('--lr', type=float, default=3e-4)
    ap.add_argument('--wd', type=float, default=0.01)
    ap.add_argument('--warmup', type=int, default=20)
    ap.add_argument('--workers', type=int, default=8)
    ap.add_argument('--log-every', type=int, default=20,
                    help='每 N 步打印 + 写一次 status.json')
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
    # mispred
    ap.add_argument('--mispred-pos-weight', type=float, default=-1.0,
                    help='-1=自动；正数=手动；0=不加权')
    ap.add_argument('--mispred-focal-gamma', type=float, default=0.0)
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
        arr = pq.read_table(f, columns=['mispredicted'])['mispredicted'].to_numpy()
        p = int((arr > 0).sum())
        n_pos += p
        n_neg += len(arr) - p
    return (n_neg / n_pos) if n_pos else 1.0


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
    return {
        'model': model.state_dict(),
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
        torch.set_rng_state(state['torch_cpu'])
    if 'numpy' in state:
        np.random.set_state(state['numpy'])
    if 'python' in state:
        random.setstate(state['python'])


# =====================================================================
# Main
# =====================================================================
def main():
    args = get_args()

    save_path = args.save
    if not save_path:
        ts = time.strftime('%Y%m%d_%H%M%S')
        save_path = f'/data00/yinhaolang/simulators/tmp/ckpt/tao_{ts}.pt'
    save_dir = Path(save_path).parent
    save_dir.mkdir(parents=True, exist_ok=True)
    base_name = Path(save_path).stem
    status_path = str(save_dir / f'{base_name}.status.json')
    log_path = str(save_dir / f'{base_name}.log')

    setup_logging(log_path)

    log.info('=' * 64)
    log.info('args: %s', json.dumps(vars(args), indent=2))
    log.info('save : %s', save_path)
    log.info('log  : %s', log_path)
    log.info('stat : %s', status_path)
    log.info('=' * 64)

    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    random.seed(args.seed)
    torch.set_num_threads(args.num_threads)

    log.info('[env] OMP=%s MKL=%s torch.threads=%d mkldnn=%s',
             os.environ.get('OMP_NUM_THREADS'),
             os.environ.get('MKL_NUM_THREADS'),
             torch.get_num_threads(),
             torch.backends.mkldnn.is_available())

    log.info('[init] loading dataset from %s', args.data)
    t0 = time.time()
    spec = DatasetSpec(root=args.data, context_len=args.ctx)
    ds = ParquetWindowDataset(spec)
    log.info('[init] dataset rows=%d loaded in %.2fs', len(ds), time.time() - t0)
    # V10 schema sanity: cacheline_paddr 是否存在；缺失 => 旧 50M 数据，
    # COMPAT-OLD-50M fallback 路径生效，仅打 warning 不阻断训练。
    try:
        sample_pq = next(Path(args.data).rglob('*.parquet'))
        import pyarrow.parquet as _pq
        cols = set(_pq.read_metadata(sample_pq).schema.to_arrow_schema().names)
        if 'cacheline_paddr' in cols:
            log.info('[init] schema OK: cacheline_paddr present (V10 dataset)')
        else:
            log.warning('[init] cacheline_paddr MISSING in %s -> COMPAT-OLD-50M '
                        'fallback (cline_p_bucket==cline_bucket)', sample_pq)
    except StopIteration:
        log.warning('[init] no parquet found under %s', args.data)
    except Exception as _e:
        log.warning('[init] schema probe failed: %s', _e)
    vocabs = ds.num_features()
    log.info('[init] vocabs=%s', vocabs)

    if args.mispred_pos_weight < 0:
        t0 = time.time()
        pw = estimate_mispred_pos_weight(args.data)
        log.info('[init] auto mispred_pos_weight = %.2f (scan %.2fs)', pw, time.time() - t0)
    else:
        pw = float(args.mispred_pos_weight)
        log.info('[init] manual mispred_pos_weight = %.2f', pw)

    cfg = TaoConfig(
        context_len=args.ctx,
        # macro_pc_id 不再作为模型输入；保留 cfg 字段仅为旧 ckpt / infer 兼容。
        macro_pc_vocab=1,
        mispred_pos_weight=pw,
        mispred_focal_gamma=args.mispred_focal_gamma,
    )
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    model = TaoCoreTransformer(cfg).to(device)
    log.info('[init] model #params=%.2fM device=%s bf16=%s compile=%s focal_gamma=%.2f',
             model.num_params() / 1e6, device, args.bf16, args.compile,
             args.mispred_focal_gamma)

    if args.compile:
        try:
            model = torch.compile(model, mode='reduce-overhead')
            log.info('[init] torch.compile enabled')
        except Exception as e:
            log.warning('[init] torch.compile failed: %s; eager fallback', e)

    loader = DataLoader(
        ds,
        batch_size=args.bs,
        shuffle=True,
        num_workers=args.workers,
        pin_memory=(device.type == 'cuda'),
        persistent_workers=(args.workers > 0),
        collate_fn=collate,
        drop_last=True,
    )

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
        model.load_state_dict(ck['model'])
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

    def write_status_now(extra: dict | None = None):
        payload = {
            'pid': os.getpid(),
            'step': step,
            'total_steps': args.steps,
            'ema_loss': float(ema_loss) if ema_loss is not None else None,
            'best_loss': best_loss if best_loss != float('inf') else None,
            'lr': float(sched.get_last_lr()[0]),
            'elapsed_s': time.time() - t_start,
            'updated_ts': time.time(),
        }
        if extra:
            payload.update(extra)
        write_status(status_path, payload)

    try:
        while step < args.steps:
            for batch in loader:
                if step >= args.steps or stop_after_save['flag']:
                    break
                feat = {k: v.to(device, non_blocking=True) for k, v in batch['feat'].items()}
                batch_dev = {
                    'feat': feat,
                    'attn_mask': batch['attn_mask'].to(device, non_blocking=True),
                    'fetch_lat': batch['fetch_lat'].to(device, non_blocking=True),
                    'exec_lat': batch['exec_lat'].to(device, non_blocking=True),
                    'mispred': batch['mispred'].to(device, non_blocking=True),
                }
                with amp_ctx():
                    out = model(batch_dev)
                    losses = model.compute_loss(batch_dev, out)
                loss = losses['loss']
                optim.zero_grad(set_to_none=True)
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                optim.step()
                sched.step()

                li = float(loss.detach())
                ema_loss = li if ema_loss is None else 0.95 * ema_loss + 0.05 * li
                step += 1

                if step % args.log_every == 0 or step == 1:
                    now = time.time()
                    dt = now - last_t
                    tput = (step - last_step) * args.bs / max(dt, 1e-6)
                    last_t, last_step = now, step
                    log.info(
                        '[step %5d/%d] loss=%7.3f ema=%7.3f '
                        'mse_f=%.3f mse_e=%.3f bce_m=%.3f lr=%.2e samples/s=%.1f',
                        step, args.steps, li, ema_loss,
                        float(losses['mse_fetch']), float(losses['mse_exec']),
                        float(losses['bce_mispred']),
                        sched.get_last_lr()[0], tput,
                    )
                    write_status_now({'samples_per_sec': tput})

                # 周期性 ckpt（原子写 + 限量保留）
                if args.save_every > 0 and step - last_save_step >= args.save_every:
                    last_save_step = step
                    ck_path = str(save_dir / f'{base_name}.step{step}.pt')
                    atomic_torch_save(
                        collect_state(model, optim, sched, step, ema_loss, best_loss,
                                      cfg, args, snapshot_rng()),
                        ck_path,
                    )
                    log.info('[ckpt ] periodic -> %s', ck_path)
                    prune_periodic_ckpts(save_dir, base_name, args.keep_last)

                # 最佳 ckpt
                if ema_loss is not None and ema_loss < best_loss * 0.999:
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
        # 不管正常 / 异常 / 信号退出，都保存 last.pt
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
    log.info('[done] %d steps in %.1fs avg samples/s=%.1f final ema_loss=%s best=%.4f',
             step, elapsed,
             step * args.bs / max(elapsed, 1e-6),
             ema_loss, best_loss)

    # 最终命名 ckpt（与 args.save 一致）
    if save_path:
        atomic_torch_save(
            collect_state(model, optim, sched, step, ema_loss, best_loss,
                          cfg, args, snapshot_rng()),
            save_path,
        )
        log.info('[ckpt ] final -> %s', save_path)
    write_status_now({'finished': True})


if __name__ == '__main__':
    main()
