"""Train the clean v26 full Q/K/V/R model.

This implementation follows docs/v26_full_qkvr_plan.md. It intentionally uses
structured tensors and V26KVQRModel. It requires windows/cache rebuilt with the
v26_14 UOP field schema.

Important limitation:
  Existing v16/v25a caches provide only 6 UOP fields and cannot be upgraded in
  place because the tensor cache already discarded the missing fields.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time

import torch
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.utils.data import DataLoader, Sampler, Subset, random_split
from torch.utils.data.distributed import DistributedSampler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from model.v26_kvqr import V26KVQRConfig, V26KVQRModel, V26_PMU_KEYS  # noqa: E402
from model import tokenizer as tk  # noqa: E402
from train.dataset import WindowDataset, make_collate_v26_structured  # noqa: E402


V26_DENOM_KEYS = [
    "branch_count",
    "loads",
    "stores",
    "atomics",
    "mem_ops",
    "page_touches",
]
V26_DENOM_IDX = {k: i for i, k in enumerate(V26_DENOM_KEYS)}
V26_KEY_TO_DENOM = {
    "branch_miss": "branch_count",
    "l1d_ld_miss": "loads",
    "l1d_st_miss": "store_ops",
    "l2_ld_miss": "loads",
    "l2_st_miss": "store_ops",
    "llc_miss": "mem_ops",
    "dtlb_miss": "mem_ops",
}
V26_LOSS_SCHEMA = (
    "cpi_abs0.3_topk0.5_pairgapw0.4_cycles0.2_countlog0.05_v3"
)
V26_MODEL_SCHEMA = "v26b_8key_clean14field_doc_qkvr_packed_v1"


class V26KVQRLoss(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.key_idx = {k: i for i, k in enumerate(V26_PMU_KEYS)}

    @staticmethod
    def _huber(x, y, delta: float):
        e = x - y
        ae = e.abs()
        return torch.where(
            ae <= delta,
            0.5 * e * e,
            delta * (ae - 0.5 * delta),
        )

    def _denom_for(self, denoms: torch.Tensor, key: str) -> torch.Tensor:
        denom_key = V26_KEY_TO_DENOM[key]
        if denom_key == "store_ops":
            return (
                denoms[..., V26_DENOM_IDX["stores"]]
                + denoms[..., V26_DENOM_IDX["atomics"]]
            )
        return denoms[..., V26_DENOM_IDX[denom_key]]

    def forward(self, pred: torch.Tensor, label: torch.Tensor,
                core_mask: torch.Tensor, uops: torch.Tensor,
                denoms: torch.Tensor):
        m = core_mask.to(pred.dtype)
        eps = 1.0e-6
        pred_log_cpi = pred[..., 0]
        label_log_cpi = torch.log(label[..., 0].clamp(min=eps).to(pred.dtype))
        cpi_loss = self._huber(pred_log_cpi, label_log_cpi, 0.3)
        cpi_abs = (cpi_loss * m).sum() / m.sum().clamp(min=1.0)

        active_cpi_loss = cpi_loss[core_mask.to(torch.bool)]
        if active_cpi_loss.numel() > 0:
            k = max(1, (int(active_cpi_loss.numel()) + 4) // 5)
            cpi_topk = torch.topk(active_cpi_loss, k).values.mean()
        else:
            cpi_topk = pred.new_zeros(())

        active = core_mask.to(torch.bool)
        C = pred.shape[1]
        pair_mask = (
            active[:, :, None]
            & active[:, None, :]
            & (
                torch.arange(C, device=pred.device)[:, None]
                < torch.arange(C, device=pred.device)[None, :]
            )[None, :, :]
        )
        pair_mask_f = pair_mask.to(pred.dtype)
        pred_gap = pred_log_cpi[:, :, None] - pred_log_cpi[:, None, :]
        label_gap = label_log_cpi[:, :, None] - label_log_cpi[:, None, :]
        pair_weight = (label_gap.abs() / 0.3).clamp(0.5, 3.0).detach()
        pair_denom = (pair_weight * pair_mask_f).sum().clamp(min=1.0)
        pairwise = (
            self._huber(pred_gap, label_gap, 0.5) * pair_weight * pair_mask_f
        ).sum() / pair_denom

        pred_cpi = torch.exp(pred_log_cpi.clamp(-20.0, 20.0))
        label_cpi = label[..., 0].to(pred.dtype).clamp(min=eps)
        uops_t = uops.to(pred.dtype).clamp(min=1.0)
        cyc_pred = (pred_cpi * uops_t * m).sum(dim=1).clamp(min=eps)
        cyc_tgt = (label_cpi * uops_t * m).sum(dim=1).clamp(min=eps)
        cycles = self._huber(torch.log(cyc_pred), torch.log(cyc_tgt), 0.1).mean()

        count_losses = []
        for out_i, key in enumerate(V26_PMU_KEYS[1:], start=1):
            denom = self._denom_for(denoms.to(pred.dtype), key).clamp(min=0.0)
            pred_rate = pred[..., out_i].clamp(0.0, 1.0)
            label_count = label[..., out_i].to(pred.dtype).clamp(min=0.0)
            pred_count = pred_rate * denom
            count_loss = (
                self._huber(torch.log1p(pred_count), torch.log1p(label_count), 0.5)
                * m
            ).sum() / m.sum().clamp(min=1.0)
            count_losses.append(count_loss)

        count_log = torch.stack(count_losses).mean()

        centered = pred.new_zeros(())
        rate = pred.new_zeros(())
        physical = pred.new_zeros(())
        rank = pred.new_zeros(())
        total = (
            1.0 * cpi_abs
            + 0.5 * cpi_topk
            + 0.4 * pairwise
            + 0.2 * cycles
            + 0.05 * count_log
        )
        logs = {
            "loss": total.detach(),
            "L_cpi_uop": cpi_abs.detach(),
            "L_cpi_topk": cpi_topk.detach(),
            "L_centered_cpi": centered.detach(),
            "L_pairwise_cpi": pairwise.detach(),
            "L_cycles": cycles.detach(),
            "L_count_log": count_log.detach(),
            "L_rate": rate.detach(),
            "L_phys": physical.detach(),
            "L_rank": rank.detach(),
        }
        return total, logs


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--cache-path", default=None)
    ap.add_argument("--out", default="ckpt/v26_kvqr_smoke")
    ap.add_argument("--steps", type=int, default=200)
    ap.add_argument("--bs", type=int, default=4)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--weight-decay", type=float, default=0.05)
    ap.add_argument("--max-len", type=int, default=32768)
    ap.add_argument("--d-model", type=int, default=320)
    ap.add_argument("--n-heads", type=int, default=8)
    ap.add_argument("--n-layers", type=int, default=8)
    ap.add_argument("--ffn-dim", type=int, default=1280)
    ap.add_argument("--field-dim", type=int, default=96)
    ap.add_argument("--head-hidden", type=int, default=256)
    ap.add_argument("--max-uops-per-core", type=int, default=32768,
                    help="Model position capacity for one core. This no "
                         "longer controls the training compute budget.")
    ap.add_argument("--train-max-uops-per-core", type=int, default=0,
                    help="Optional training filter on max per-core UOPs. "
                         "0 disables this filter.")
    ap.add_argument("--train-max-total-uops", type=int, default=8192,
                    help="Training filter on sum of UOPs over active cores. "
                         "This is the main ragged-attention compute cap; "
                         "0 disables this filter.")
    ap.add_argument("--no-filter-long-uops", action="store_true",
                    help="Disable training UOP count filters. This is only "
                         "useful for debugging capacity errors.")
    ap.add_argument("--no-bucket-by-shape", action="store_true",
                    help="Disable C/L bucketed batch sampling.")
    ap.add_argument("--length-bucket-size", type=int, default=512,
                    help="Round total UOP count up to this bin size for "
                         "shape bucketed sampling.")
    ap.add_argument("--dropout", type=float, default=0.1)
    ap.add_argument("--amp-dtype", choices=("fp16", "bf16", "none"),
                    default="bf16",
                    help="CUDA autocast dtype. fp16 uses GradScaler; bf16 "
                         "does not. CPU ignores this setting.")
    ap.add_argument("--sdpa-backend",
                    choices=("auto", "flash", "no_flash", "math",
                             "efficient"),
                    default="auto",
                    help="SDPA backend selector. no_flash keeps bf16 autocast "
                         "but excludes cuDNN/Flash attention backends.")
    ap.add_argument("--require-flash-attn", dest="require_flash_attn",
                    action="store_true", default=True,
                    help="Profile the first real CUDA train batch and fail "
                         "unless flash attention kernels are observed.")
    ap.add_argument("--no-require-flash-attn", dest="require_flash_attn",
                    action="store_false",
                    help="Disable first-batch flash attention verification.")
    ap.add_argument("--val-frac", type=float, default=0.05)
    ap.add_argument("--eval-every", type=int, default=100)
    ap.add_argument("--eval-batches", type=int, default=20)
    ap.add_argument("--save-every", type=int, default=500,
                    help="Save resumable step_XXXXXX snapshots every N global "
                         "steps. 0 disables periodic snapshots.")
    ap.add_argument("--init-ckpt", default=None,
                    help="Optional checkpoint file or directory to resume from. "
                         "Defaults to INIT_CKPT env when set.")
    ap.add_argument("--step-offset", type=int, default=None,
                    help="Global step offset for resumed runs. Defaults to "
                         "STEP_OFFSET env or checkpoint step.")
    ap.add_argument("--skip-train-batches", type=int, default=None,
                    help="Number of already-consumed train batches. Defaults "
                         "to SKIP_TRAIN_BATCHES env or step offset.")
    ap.add_argument("--num-workers", type=int, default=2)
    ap.add_argument("--prefetch-factor", type=int, default=2,
                    help="DataLoader prefetch_factor when num_workers > 0.")
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--device", default=None)
    return ap.parse_args()


def move_batch(batch: dict, device: torch.device) -> dict:
    return {k: v.to(device, non_blocking=True) if torch.is_tensor(v) else v
            for k, v in batch.items()}


def _env_int(name: str, default: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return int(default)
    return int(raw)


def _resolve_checkpoint_path(path: str | None) -> str | None:
    if not path:
        return None
    if os.path.isdir(path):
        for name in ("last.pt", "best.pt"):
            candidate = os.path.join(path, name)
            if os.path.isfile(candidate):
                return candidate
        return None
    return path if os.path.isfile(path) else None


def _atomic_torch_save(obj: dict, path: str) -> None:
    tmp = path + ".tmp"
    torch.save(obj, tmp)
    os.replace(tmp, path)


def _move_optimizer_state(optim: torch.optim.Optimizer,
                          device: torch.device) -> None:
    for state in optim.state.values():
        for key, value in list(state.items()):
            if torch.is_tensor(value):
                state[key] = value.to(device)


def _amp_dtype(name: str, device: torch.device) -> torch.dtype | None:
    if device.type != "cuda" or name == "none":
        return None
    if name == "fp16":
        return torch.float16
    if name == "bf16":
        return torch.bfloat16
    raise ValueError(f"unknown amp dtype: {name}")


def _autocast(device: torch.device, dtype: torch.dtype | None):
    return torch.amp.autocast(
        device_type=device.type,
        dtype=dtype,
        enabled=device.type == "cuda" and dtype is not None,
    )


def _profile_attention_events(prof: torch.profiler.profile) -> list[str]:
    names = set()
    for event in prof.key_averages():
        key = str(event.key)
        low = key.lower()
        if "attention" in low or "scaled_dot_product" in low or "flash" in low:
            names.add(key)
    return sorted(names)


def _has_flash_attention_event(events: list[str]) -> bool:
    for name in events:
        low = name.lower()
        if "flash" in low and (
            "attention" in low
            or "scaled_dot_product" in low
            or "flash_fwd" in low
            or "flash_bwd" in low
            or "flash_fprop" in low
            or "flash_bprop" in low
        ):
            return True
    return False


def _all_ranks_bool(value: bool, device: torch.device) -> bool:
    if not dist.is_available() or not dist.is_initialized():
        return bool(value)
    t = torch.tensor([1 if value else 0], device=device, dtype=torch.int32)
    dist.all_reduce(t, op=dist.ReduceOp.MIN)
    return bool(int(t.item()))


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    pos = (len(xs) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    frac = pos - lo
    return float(xs[lo] * (1.0 - frac) + xs[hi] * frac)


def _filter_indices_by_uops_from_tensor_cache(
    cache_path: str,
    max_core_limit: int,
    max_total_limit: int,
):
    cache_dir = os.path.realpath(cache_path)
    manifest_path = os.path.join(cache_dir, "manifest.pt")
    if not os.path.isdir(cache_dir) or not os.path.isfile(manifest_path):
        return None
    try:
        manifest = torch.load(manifest_path, map_location="cpu")
    except Exception:
        return None
    if not isinstance(manifest, dict) or not manifest.get("shards"):
        return None

    keep: list[int] = []
    max_core_values: list[float] = []
    total_uop_values: list[float] = []
    core_values: list[int] = []
    total = 0
    for shard_info in manifest["shards"]:
        shard_path = os.path.join(cache_dir, shard_info["file"])
        shard = torch.load(shard_path, map_location="cpu")
        if "uops" not in shard or "n_core" not in shard:
            return None
        n = int(shard.get("count", len(shard["n_core"])))
        n_core = shard["n_core"][:n].long()
        uops = shard["uops"][:n].float()
        max_nc = int(uops.shape[1])
        active = (
            torch.arange(max_nc, device=uops.device)[None, :]
            < n_core[:, None]
        )
        active_uops = torch.where(
            active, uops, torch.zeros_like(uops)
        )
        per_sample_l = active_uops.amax(dim=1)
        per_sample_total = active_uops.sum(dim=1)
        keep_mask = torch.ones((n,), dtype=torch.bool)
        if max_core_limit > 0:
            keep_mask &= per_sample_l <= float(max_core_limit)
        if max_total_limit > 0:
            keep_mask &= per_sample_total <= float(max_total_limit)
        local_keep = keep_mask.nonzero(as_tuple=False).flatten().tolist()
        keep.extend(total + int(i) for i in local_keep)
        max_core_values.extend(float(x) for x in per_sample_l.tolist())
        total_uop_values.extend(float(x) for x in per_sample_total.tolist())
        core_values.extend(int(x) for x in n_core.tolist())
        total += n

    stats = {
        "source": "tensor_cache_uops",
        "total": int(total),
        "kept": int(len(keep)),
        "dropped": int(total - len(keep)),
        "max_core_limit": int(max_core_limit),
        "max_total_limit": int(max_total_limit),
        "max_core_L_p50": _quantile(max_core_values, 0.50),
        "max_core_L_p90": _quantile(max_core_values, 0.90),
        "max_core_L_p99": _quantile(max_core_values, 0.99),
        "max_core_L_max": max(max_core_values) if max_core_values else 0.0,
        "total_uops_p50": _quantile(total_uop_values, 0.50),
        "total_uops_p90": _quantile(total_uop_values, 0.90),
        "total_uops_p99": _quantile(total_uop_values, 0.99),
        "total_uops_max": max(total_uop_values) if total_uop_values else 0.0,
    }
    return keep, stats, max_core_values, total_uop_values, core_values


def _filter_indices_by_uops_from_dataset(
    ds: WindowDataset,
    max_core_limit: int,
    max_total_limit: int,
):
    keep: list[int] = []
    max_core_values: list[float] = []
    total_uop_values: list[float] = []
    core_values: list[int] = []
    for idx in range(len(ds)):
        item = ds[idx]
        nc = int(item["n_core"])
        uops = item.get("uops", item["instr_retired"])
        active = [float(x) for x in uops[:nc]]
        max_l = max(active) if active else 0.0
        total_l = sum(active)
        max_core_values.append(max_l)
        total_uop_values.append(total_l)
        core_values.append(nc)
        ok = True
        if max_core_limit > 0 and max_l > float(max_core_limit):
            ok = False
        if max_total_limit > 0 and total_l > float(max_total_limit):
            ok = False
        if ok:
            keep.append(idx)
    stats = {
        "source": "dataset_uops_scan",
        "total": int(len(ds)),
        "kept": int(len(keep)),
        "dropped": int(len(ds) - len(keep)),
        "max_core_limit": int(max_core_limit),
        "max_total_limit": int(max_total_limit),
        "max_core_L_p50": _quantile(max_core_values, 0.50),
        "max_core_L_p90": _quantile(max_core_values, 0.90),
        "max_core_L_p99": _quantile(max_core_values, 0.99),
        "max_core_L_max": max(max_core_values) if max_core_values else 0.0,
        "total_uops_p50": _quantile(total_uop_values, 0.50),
        "total_uops_p90": _quantile(total_uop_values, 0.90),
        "total_uops_p99": _quantile(total_uop_values, 0.99),
        "total_uops_max": max(total_uop_values) if total_uop_values else 0.0,
    }
    return keep, stats, max_core_values, total_uop_values, core_values


def filter_dataset_by_uops(ds: WindowDataset, cache_path: str,
                           max_core_limit: int,
                           max_total_limit: int):
    if max_core_limit <= 0 and max_total_limit <= 0:
        n = int(len(ds))
        return ds, {
            "source": "disabled",
            "total": n,
            "kept": n,
            "dropped": 0,
            "max_core_limit": int(max_core_limit),
            "max_total_limit": int(max_total_limit),
        }
    result = _filter_indices_by_uops_from_tensor_cache(
        cache_path, max_core_limit, max_total_limit)
    if result is None:
        result = _filter_indices_by_uops_from_dataset(
            ds, max_core_limit, max_total_limit)
    keep, stats, max_core_lengths, total_uops, n_cores = result
    if not keep:
        raise RuntimeError(
            "no samples remain after UOP filters "
            f"max_core={max_core_limit} max_total={max_total_limit}"
        )
    subset = Subset(ds, keep)
    subset.v26_lengths = [int(total_uops[i]) for i in keep]
    subset.v26_max_core_uops = [int(max_core_lengths[i]) for i in keep]
    subset.v26_total_uops = [int(total_uops[i]) for i in keep]
    subset.v26_n_cores = [int(n_cores[i]) for i in keep]
    return subset, stats


def filter_dataset_by_max_uops(ds: WindowDataset, cache_path: str, limit: int):
    return filter_dataset_by_uops(
        ds, cache_path, max_core_limit=limit, max_total_limit=0)


def shape_meta_for_dataset(ds):
    lengths = getattr(ds, "v26_lengths", None)
    n_cores = getattr(ds, "v26_n_cores", None)
    if lengths is not None and n_cores is not None:
        return list(lengths), list(n_cores)
    if isinstance(ds, Subset):
        parent = shape_meta_for_dataset(ds.dataset)
        if parent is None:
            return None
        parent_lengths, parent_cores = parent
        return (
            [int(parent_lengths[int(i)]) for i in ds.indices],
            [int(parent_cores[int(i)]) for i in ds.indices],
        )
    return None


def require_dataset_uop_field_count(ds, required: int,
                                    probe_samples: int = 64) -> dict:
    checked = 0
    max_seen = 0
    short_seen = None
    limit = min(int(len(ds)), int(probe_samples))
    for idx in range(limit):
        item = ds[idx]
        fields = item.get("uop_fields", [])
        if torch.is_tensor(fields):
            rows_iter = range(int(fields.shape[0]))
            width_for = lambda _pos: int(fields.shape[1])
        else:
            fields = list(fields)
            rows_iter = range(len(fields))
            width_for = lambda pos: len(fields[pos] or [])
        for pos in rows_iter:
            width = width_for(pos)
            checked += 1
            max_seen = max(max_seen, width)
            if width < required:
                short_seen = {
                    "sample_idx": int(idx),
                    "uop_pos": int(pos),
                    "field_count": int(width),
                }
                break
        if short_seen is not None:
            break
        if checked >= 32 and max_seen >= required:
            break
    if short_seen is not None or max_seen < required:
        detail = short_seen or {
            "sample_idx": None,
            "uop_pos": None,
            "field_count": int(max_seen),
        }
        raise ValueError(
            f"v26 clean training requires {required}-field UOP rows, but "
            f"dataset/cache only exposes {detail['field_count']} fields "
            f"(sample={detail['sample_idx']} pos={detail['uop_pos']}). "
            "Rebuild windows/cache with data/build_windows.py "
            "--uop-field-schema v26_14 and remove stale tensor_cache."
        )
    return {
        "required": int(required),
        "checked_uops": int(checked),
        "max_seen": int(max_seen),
    }


class ShapeBucketBatchSampler(Sampler[list[int]]):
    """Batch sampler that keeps C and total UOP count similar per step."""

    def __init__(self, lengths: list[int], n_cores: list[int],
                 batch_size: int, seed: int, bucket_size: int = 64,
                 rank: int = 0, world: int = 1, drop_last: bool = True,
                 shuffle: bool = True):
        if len(lengths) != len(n_cores):
            raise ValueError("lengths and n_cores must have the same length")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if world <= 0:
            raise ValueError("world must be positive")
        self.lengths = [int(x) for x in lengths]
        self.n_cores = [int(x) for x in n_cores]
        self.batch_size = int(batch_size)
        self.seed = int(seed)
        self.bucket_size = max(1, int(bucket_size))
        self.rank = int(rank)
        self.world = int(world)
        self.drop_last = bool(drop_last)
        self.shuffle = bool(shuffle)
        self.epoch = 0
        self._buckets = self._build_buckets()

    def _bucket_key(self, idx: int) -> tuple[int, int]:
        l_bin = (
            (max(1, self.lengths[idx]) + self.bucket_size - 1)
            // self.bucket_size
        ) * self.bucket_size
        return self.n_cores[idx], l_bin

    def _build_buckets(self) -> dict[tuple[int, int], list[int]]:
        buckets: dict[tuple[int, int], list[int]] = {}
        for idx in range(len(self.lengths)):
            buckets.setdefault(self._bucket_key(idx), []).append(idx)
        return buckets

    def set_epoch(self, epoch: int) -> None:
        self.epoch = int(epoch)

    def __len__(self) -> int:
        if self.world > 1:
            unit = self.batch_size * self.world
            return sum(len(v) // unit for v in self._buckets.values())
        return sum(len(v) // self.batch_size for v in self._buckets.values())

    def __iter__(self):
        rng = random.Random(self.seed + self.epoch)
        bucket_items = [
            (key, list(indices)) for key, indices in self._buckets.items()
        ]
        if self.shuffle:
            rng.shuffle(bucket_items)
        batches: list[list[int]] = []
        for _, indices in bucket_items:
            if self.shuffle:
                rng.shuffle(indices)
            if self.world > 1:
                unit = self.batch_size * self.world
                usable = (len(indices) // unit) * unit
                if not self.drop_last and usable < len(indices):
                    extra = unit - (len(indices) - usable)
                    indices = indices + indices[:extra]
                    usable = len(indices)
                for start in range(0, usable, unit):
                    chunk = indices[start:start + unit]
                    local_start = self.rank * self.batch_size
                    local = chunk[local_start:local_start + self.batch_size]
                    if len(local) == self.batch_size:
                        batches.append(local)
            else:
                usable = (len(indices) // self.batch_size) * self.batch_size
                if not self.drop_last and usable < len(indices):
                    usable = len(indices)
                for start in range(0, usable, self.batch_size):
                    local = indices[start:start + self.batch_size]
                    if len(local) == self.batch_size or not self.drop_last:
                        batches.append(local)
        if self.shuffle:
            rng.shuffle(batches)
        return iter(batches)


def setup_ddp():
    if "RANK" in os.environ and "WORLD_SIZE" in os.environ:
        rank = int(os.environ["RANK"])
        local_rank = int(os.environ.get("LOCAL_RANK", 0))
        world = int(os.environ["WORLD_SIZE"])
        dist.init_process_group(backend="nccl" if torch.cuda.is_available() else "gloo")
        if torch.cuda.is_available():
            torch.cuda.set_device(local_rank)
        return True, rank, local_rank, world
    return False, 0, 0, 1


def main() -> None:
    args = parse_args()
    torch.manual_seed(args.seed)
    is_ddp, rank, local_rank, world = setup_ddp()
    device = torch.device(
        args.device
        or (f"cuda:{local_rank}" if torch.cuda.is_available() else "cpu")
    )
    amp_dtype = _amp_dtype(args.amp_dtype, device)
    amp_enabled = device.type == "cuda" and amp_dtype is not None
    scaler = torch.amp.GradScaler(
        "cuda",
        enabled=amp_enabled and amp_dtype == torch.float16,
    )
    flash_check_enabled = bool(args.require_flash_attn and device.type == "cuda")
    if flash_check_enabled and args.sdpa_backend in {
            "no_flash", "math", "efficient"}:
        raise RuntimeError(
            "--require-flash-attn conflicts with "
            f"--sdpa-backend={args.sdpa_backend}"
        )
    if flash_check_enabled and amp_dtype not in (torch.float16, torch.bfloat16):
        raise RuntimeError(
            "flash attention verification requires --amp-dtype fp16 or bf16"
        )
    if rank == 0:
        os.makedirs(args.out, exist_ok=True)

    cache_path = args.cache_path or WindowDataset.default_cache_path(
        args.data, args.max_len)
    ds = WindowDataset(
        args.data,
        max_len=args.max_len,
        cache_path=cache_path,
        require_cache=True,
        label_keys=V26_PMU_KEYS,
    )
    if args.no_filter_long_uops:
        filter_stats = {
            "source": "disabled",
            "total": int(len(ds)),
            "kept": int(len(ds)),
            "dropped": 0,
            "max_core_limit": int(args.train_max_uops_per_core),
            "max_total_limit": int(args.train_max_total_uops),
        }
    else:
        ds, filter_stats = filter_dataset_by_uops(
            ds, cache_path,
            max_core_limit=args.train_max_uops_per_core,
            max_total_limit=args.train_max_total_uops,
        )
    uop_field_stats = require_dataset_uop_field_count(
        ds, tk.V26_UOP_FIELD_COUNT
    )
    n_val = max(1, int(len(ds) * args.val_frac))
    n_train = len(ds) - n_val
    gen = torch.Generator().manual_seed(args.seed)
    train_ds, val_ds = random_split(ds, [n_train, n_val], generator=gen)

    collate = make_collate_v26_structured(
        field_count=tk.V26_UOP_FIELD_COUNT,
    )
    loader_kwargs = {
        "collate_fn": collate,
        "num_workers": args.num_workers,
        "persistent_workers": args.num_workers > 0,
        "pin_memory": device.type == "cuda",
    }
    if args.num_workers > 0:
        loader_kwargs["prefetch_factor"] = max(1, int(args.prefetch_factor))
    bucket_sampler = None
    shape_meta = None if args.no_bucket_by_shape else shape_meta_for_dataset(train_ds)
    if shape_meta is not None:
        lengths, n_cores = shape_meta
        bucket_sampler = ShapeBucketBatchSampler(
            lengths, n_cores,
            batch_size=args.bs,
            seed=args.seed,
            bucket_size=args.length_bucket_size,
            rank=rank if is_ddp else 0,
            world=world if is_ddp else 1,
            drop_last=True,
            shuffle=True,
        )
    if is_ddp:
        val_sampler = DistributedSampler(
            val_ds, num_replicas=world, rank=rank, shuffle=False,
            drop_last=False,
        )
        if bucket_sampler is not None:
            train_sampler = bucket_sampler
            train_dl = DataLoader(
                train_ds, batch_sampler=bucket_sampler,
                **loader_kwargs,
            )
        else:
            train_sampler = DistributedSampler(
                train_ds, num_replicas=world, rank=rank, shuffle=True,
                drop_last=True,
            )
            train_dl = DataLoader(
                train_ds, batch_size=args.bs, sampler=train_sampler,
                drop_last=True,
                **loader_kwargs,
            )
        val_dl = DataLoader(
            val_ds, batch_size=args.bs, sampler=val_sampler,
            **loader_kwargs,
        )
    else:
        if bucket_sampler is not None:
            train_sampler = bucket_sampler
            train_dl = DataLoader(
                train_ds, batch_sampler=bucket_sampler,
                **loader_kwargs,
            )
        else:
            train_sampler = None
            train_dl = DataLoader(
                train_ds, batch_size=args.bs, shuffle=True, drop_last=True,
                **loader_kwargs,
            )
        val_dl = DataLoader(
            val_ds, batch_size=args.bs, shuffle=False, **loader_kwargs,
        )

    cfg = V26KVQRConfig(
        d_model=args.d_model,
        field_dim=args.field_dim,
        n_heads=args.n_heads,
        n_layers=args.n_layers,
        ffn_dim=args.ffn_dim,
        head_hidden=args.head_hidden,
        side_feat_dim=len(tk.SIDE_FEATURE_KEYS),
        global_feat_dim=13,
        max_uops_per_core=args.max_uops_per_core,
        dropout=args.dropout,
        uop_field_count=tk.V26_UOP_FIELD_COUNT,
        sdpa_backend=args.sdpa_backend,
    )
    model = V26KVQRModel(cfg).to(device)
    loss_fn = V26KVQRLoss().to(device)
    if is_ddp:
        model = DDP(model, device_ids=[local_rank] if torch.cuda.is_available() else None)
    optim = torch.optim.AdamW(
        model.parameters(), lr=args.lr, weight_decay=args.weight_decay,
        betas=(0.9, 0.95),
    )
    init_ckpt = args.init_ckpt or os.environ.get("INIT_CKPT")
    resume_path = _resolve_checkpoint_path(init_ckpt)
    step_offset_arg = (
        args.step_offset
        if args.step_offset is not None
        else _env_int("STEP_OFFSET", 0)
    )
    skip_batches_arg = (
        args.skip_train_batches
        if args.skip_train_batches is not None
        else _env_int("SKIP_TRAIN_BATCHES", step_offset_arg)
    )
    best = float("inf")
    step_offset = int(step_offset_arg)
    if resume_path:
        ckpt = torch.load(resume_path, map_location=device)
        raw_model = model.module if is_ddp else model
        raw_model.load_state_dict(ckpt["model"], strict=True)
        if "optim" in ckpt:
            optim.load_state_dict(ckpt["optim"])
            _move_optimizer_state(optim, device)
        if scaler.is_enabled() and ckpt.get("scaler"):
            scaler.load_state_dict(ckpt["scaler"])
        ckpt_step = int(ckpt.get("step") or 0)
        if step_offset <= 0:
            step_offset = ckpt_step
        best = float(ckpt.get("best_val_loss", ckpt.get("val_loss", best)))

    def _ckpt_payload(global_step: int, val_loss: float | None = None) -> dict:
        raw_model = model.module if is_ddp else model
        payload = {
            "model": raw_model.state_dict(),
            "optim": optim.state_dict(),
            "config": cfg.__dict__,
            "step": int(global_step),
            "best_val_loss": float(best),
            "schema": V26_MODEL_SCHEMA,
            "loss_schema": V26_LOSS_SCHEMA,
            "amp_dtype": args.amp_dtype,
            "scaler": scaler.state_dict() if scaler.is_enabled() else None,
        }
        if val_loss is not None:
            payload["val_loss"] = float(val_loss)
        return payload

    def _save_snapshot(path: str, global_step: int,
                       val_loss: float | None = None) -> None:
        os.makedirs(os.path.join(path, "lora_best"), exist_ok=True)
        _atomic_torch_save(
            _ckpt_payload(global_step, val_loss),
            os.path.join(path, "last.pt"),
        )
        _atomic_torch_save({
            "step": int(global_step),
            "val_loss": float(best),
            "schema": V26_MODEL_SCHEMA,
            "loss_schema": V26_LOSS_SCHEMA,
        }, os.path.join(path, "head_best.pt"))

    def run_val() -> float:
        model.eval()
        total = 0.0
        count = 0
        with torch.no_grad():
            for bi, batch in enumerate(val_dl):
                if args.eval_batches and bi >= args.eval_batches:
                    break
                batch = move_batch(batch, device)
                with _autocast(device, amp_dtype):
                    pred = model(
                        batch["uop_fields"], batch["uop_mask"],
                        batch["core_mask"], batch["side_feats"],
                        batch["global_feats"],
                    )
                loss, _ = loss_fn(
                    pred.float(), batch["label"], batch["core_mask"],
                    uops=batch["uops"], denoms=batch["denoms"],
                )
                total += float(loss.detach().cpu())
                count += 1
        model.train()
        if is_ddp:
            t = torch.tensor([total, count], dtype=torch.float64, device=device)
            dist.all_reduce(t, op=dist.ReduceOp.SUM)
            total = float(t[0].cpu())
            count = int(t[1].cpu())
        return total / max(count, 1)

    if rank == 0:
        print(json.dumps({
            "event": "start",
            "data": args.data,
            "cache_path": cache_path,
            "train": n_train,
            "val": n_val,
            "device": str(device),
            "is_ddp": is_ddp,
            "world": world,
            "pmu_schema": "v26a_8key",
            "loss_schema": V26_LOSS_SCHEMA,
            "uop_fields": tk.V26_UOP_FIELD_COUNT,
            "uop_field_schema": "v26_14",
            "uop_field_stats": uop_field_stats,
            "input_mode": "v26_structured",
            "attention": "doc_qkvr",
            "attention_impl": cfg.attention_impl,
            "sdpa_backend": cfg.sdpa_backend,
            "amp_dtype": args.amp_dtype,
            "amp_enabled": amp_enabled,
            "grad_scaler": scaler.is_enabled(),
            "require_flash_attn": flash_check_enabled,
            "n_layers": args.n_layers,
            "max_uops_per_core": args.max_uops_per_core,
            "train_max_uops_per_core": args.train_max_uops_per_core,
            "train_max_total_uops": args.train_max_total_uops,
            "filter_long_uops": not args.no_filter_long_uops,
            "filter_stats": filter_stats,
            "bucket_by_shape": bucket_sampler is not None,
            "length_bucket_size": args.length_bucket_size,
            "pin_memory": bool(loader_kwargs["pin_memory"]),
            "prefetch_factor": (
                int(loader_kwargs["prefetch_factor"])
                if "prefetch_factor" in loader_kwargs else None
            ),
            "save_every": args.save_every,
            "resume_path": resume_path,
            "step_offset": step_offset,
            "skip_train_batches": int(skip_batches_arg),
            "train_batches_per_epoch": len(train_dl),
            "dataset_clean14_required": True,
        }, ensure_ascii=False), flush=True)

    model.train()
    epoch_batches = max(1, len(train_dl))
    current_epoch = max(0, int(skip_batches_arg) // epoch_batches)
    skip_in_epoch = max(0, int(skip_batches_arg) % epoch_batches)
    if train_sampler is not None:
        train_sampler.set_epoch(current_epoch)
    iterator = iter(train_dl)
    for _ in range(skip_in_epoch):
        try:
            next(iterator)
        except StopIteration:
            current_epoch += 1
            if train_sampler is not None:
                train_sampler.set_epoch(current_epoch)
            iterator = iter(train_dl)
            next(iterator)
    t0 = time.time()
    flash_checked = not flash_check_enabled

    def forward_loss(batch: dict):
        with _autocast(device, amp_dtype):
            pred = model(
                batch["uop_fields"], batch["uop_mask"], batch["core_mask"],
                batch["side_feats"], batch["global_feats"],
            )
        return loss_fn(
            pred.float(), batch["label"], batch["core_mask"],
            uops=batch["uops"], denoms=batch["denoms"],
        )

    for step in range(1, args.steps + 1):
        global_step = step_offset + step
        try:
            batch = next(iterator)
        except StopIteration:
            current_epoch += 1
            if train_sampler is not None:
                train_sampler.set_epoch(current_epoch)
            iterator = iter(train_dl)
            batch = next(iterator)
        batch = move_batch(batch, device)
        optim.zero_grad(set_to_none=True)
        if not flash_checked:
            activities = [
                torch.profiler.ProfilerActivity.CPU,
                torch.profiler.ProfilerActivity.CUDA,
            ]
            with torch.profiler.profile(
                activities=activities,
                record_shapes=False,
                profile_memory=False,
            ) as prof:
                loss, logs = forward_loss(batch)
                if scaler.is_enabled():
                    scaler.scale(loss).backward()
                else:
                    loss.backward()
                torch.cuda.synchronize(device)
            flash_events = _profile_attention_events(prof)
            local_flash = _has_flash_attention_event(flash_events)
            all_flash = _all_ranks_bool(local_flash, device)
            if rank == 0:
                print(json.dumps({
                    "event": "flash_check",
                    "step": global_step,
                    "local_rank0_flash": local_flash,
                    "all_ranks_flash": all_flash,
                    "events": flash_events[:32],
                }, ensure_ascii=False), flush=True)
            if not all_flash:
                raise RuntimeError(
                    "flash attention was not observed by profiler on every "
                    "rank; check amp dtype, GPU support, SDPA backend and "
                    "attention shapes"
                )
            flash_checked = True
        else:
            loss, logs = forward_loss(batch)
            if scaler.is_enabled():
                scaler.scale(loss).backward()
            else:
                loss.backward()
        if scaler.is_enabled():
            scaler.unscale_(optim)
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        if scaler.is_enabled():
            scaler.step(optim)
            scaler.update()
        else:
            optim.step()

        if rank == 0 and (step == 1 or step % 20 == 0):
            print(json.dumps({
                "event": "train",
                "step": global_step,
                "local_step": step,
                "loss": float(loss.detach().cpu()),
                "L_cpi_uop": float(logs.get("L_cpi_uop", loss).detach().cpu()),
                "L_cpi_topk": float(
                    logs.get("L_cpi_topk", loss).detach().cpu()
                ),
                "L_pairwise_cpi": float(
                    logs.get("L_pairwise_cpi", loss).detach().cpu()
                ),
                "L_cycles": float(logs.get("L_cycles", loss).detach().cpu()),
                "L_count_log": float(
                    logs.get("L_count_log", loss).detach().cpu()
                ),
                "elapsed_s": round(time.time() - t0, 1),
            }, ensure_ascii=False), flush=True)
        if args.eval_every and global_step % args.eval_every == 0:
            val_loss = run_val()
            if rank == 0:
                print(json.dumps({
                    "event": "val",
                    "step": global_step,
                    "local_step": step,
                    "val_loss": val_loss,
                }, ensure_ascii=False), flush=True)
            if rank == 0 and val_loss < best:
                best = val_loss
                os.makedirs(os.path.join(args.out, "lora_best"), exist_ok=True)
                _atomic_torch_save(
                    _ckpt_payload(global_step, best),
                    os.path.join(args.out, "best.pt"),
                )
                _atomic_torch_save({
                    "step": global_step,
                    "val_loss": best,
                    "schema": V26_MODEL_SCHEMA,
                    "loss_schema": V26_LOSS_SCHEMA,
                }, os.path.join(args.out, "head_best.pt"))
        if rank == 0 and args.save_every and global_step % args.save_every == 0:
            snapshot_dir = os.path.join(args.out, f"step_{global_step:06d}")
            _save_snapshot(snapshot_dir, global_step)

    if rank == 0:
        final_step = step_offset + args.steps
        _save_snapshot(args.out, final_step)
        print(json.dumps({
            "event": "done",
            "steps": final_step,
            "local_steps": args.steps,
            "best_val_loss": best,
            "out": args.out,
        }, ensure_ascii=False), flush=True)
    if is_ddp:
        dist.destroy_process_group()


if __name__ == "__main__":
    main()
