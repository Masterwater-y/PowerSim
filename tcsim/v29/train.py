"""DDP-capable v29 training loop."""
from __future__ import annotations

import math
import os
import random
import time
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import torch
import torch.distributed as dist
import torch.nn.functional as F
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.utils.data import DataLoader, DistributedSampler, WeightedRandomSampler

from ..utils.config import TCSimConfig
from ..utils.io import dump_json
from .contracts import CHECKPOINT_SCHEMA_VERSION
from .dataset import V29GlobalTimeDataset, collate_v29_sequences
from .losses import compute_v29_losses
from .model import (
    BRANCH_MODE_NEURAL_HEAD,
    BRANCH_MODES_WITHOUT_HEAD,
    LONG_HISTORY_MODE_MEMORY_GATED_CORRECTION,
    TCSimV29Model,
    build_model,
)
from ..v30.model import GSS_ADAPTER_MODES, GSS_MODE_NONE
from .sampling import (
    COVERAGE_FIRST_TRACE_BALANCED_SAMPLER,
    CoverageFirstTraceBalancedSampler,
)


@dataclass
class TrainState:
    step: int = 0
    best_validation: float = float("inf")
    best_post_coverage_validation: float = float("inf")


CONTROL_KEYS = {
    "sample_ptr", "sequence_ptr", "row_sequence", "row_sequence_step",
    "core_slots", "cursors", "sample_indices", "horizons",
}


def _to_device(batch: Dict[str, Any], device: torch.device) -> Dict[str, Any]:
    return {
        key: (
            value if key in CONTROL_KEYS else value.to(device)
        ) if isinstance(value, torch.Tensor) else value
        for key, value in batch.items()
    }


def _setup_distributed(device: str) -> Tuple[bool, int, int, int, torch.device]:
    distributed = "RANK" in os.environ and "WORLD_SIZE" in os.environ
    rank = int(os.environ.get("RANK", "0"))
    local_rank = int(os.environ.get("LOCAL_RANK", "0"))
    world = int(os.environ.get("WORLD_SIZE", "1"))
    cuda_requested = str(device) in {"auto", "cuda"} or str(device).startswith("cuda:")
    if distributed and not dist.is_initialized():
        dist.init_process_group(
            backend="nccl" if torch.cuda.is_available() and cuda_requested else "gloo"
        )
    if torch.cuda.is_available() and cuda_requested:
        torch.cuda.set_device(local_rank)
        torch_device = torch.device(f"cuda:{local_rank}")
    elif str(device) == "auto" and torch.cuda.is_available():
        torch_device = torch.device("cuda")
    else:
        torch_device = torch.device(device)
    return distributed, rank, local_rank, world, torch_device


def _amp_dtype(name: str, device: torch.device):
    if device.type != "cuda":
        return None
    value = str(name).lower()
    if value in {"none", "off", "fp32", "float32"}:
        return None
    if value in {"bf16", "bfloat16"}:
        return torch.bfloat16
    if value in {"fp16", "float16"}:
        return torch.float16
    raise ValueError(f"unsupported v29 amp dtype {name!r}")


def _attention_profile_events(profiler: Any) -> List[str]:
    events = []
    for event in profiler.key_averages():
        key = str(event.key)
        lowered = key.lower()
        if "scaled_dot_product" not in lowered and "attention" not in lowered:
            continue
        cpu_us = float(getattr(event, "self_cpu_time_total", 0.0) or 0.0)
        cuda_us = float(getattr(event, "self_cuda_time_total", 0.0) or 0.0)
        events.append(f"{key} cpu_us={cpu_us:.0f} cuda_us={cuda_us:.0f}")
    return sorted(events)


def _raw_model(model: torch.nn.Module) -> torch.nn.Module:
    return model.module if isinstance(model, DDP) else model


def _broadcast_module_state(model: torch.nn.Module, source: int = 0) -> None:
    if not (dist.is_available() and dist.is_initialized()):
        return
    for tensor in model.state_dict().values():
        dist.broadcast(tensor, src=int(source))


def _dataset_contract(dataset: V29GlobalTimeDataset) -> Dict[str, Any]:
    first = dataset.stores[0].meta
    keys = (
        "raw_trace_schema", "dataset_schema", "model_input_contract",
        "feature_schema", "branch_contract", "resource_decoder_schema",
        "resource_decoder_hash", "predictor_hash", "horizons",
        "sample_period_cycles", "dimensions",
    )
    contract = {key: first[key] for key in keys}
    if dataset.stores[0].long_history_contract is not None:
        contract["long_history"] = dict(
            dataset.stores[0].long_history_contract
        )
    if dataset.stores[0].branch_feature_contract is not None:
        contract["branch_features"] = dict(
            dataset.stores[0].branch_feature_contract
        )
    if dataset.stores[0].gss_contract is not None:
        contract["gss"] = dict(dataset.stores[0].gss_contract)
    if dataset.stores[0].exposure_contract is not None:
        contract["exposure"] = dict(dataset.stores[0].exposure_contract)
    return contract


def _save_checkpoint(
    path: str,
    model: torch.nn.Module,
    optimizer: torch.optim.Optimizer,
    state: TrainState,
    config: TCSimConfig,
    contract: Mapping[str, Any],
    history: Sequence[Mapping[str, Any]],
) -> None:
    payload = {
        "checkpoint_schema": CHECKPOINT_SCHEMA_VERSION,
        "model": _raw_model(model).state_dict(),
        "optimizer": optimizer.state_dict(),
        "step": int(state.step),
        "best_validation": float(state.best_validation),
        "best_post_coverage_validation": float(
            state.best_post_coverage_validation
        ),
        "contract": dict(contract),
        "config": {
            "chunk": dict(config.chunk),
            "scheduler": dict(config.scheduler),
            "uarch": dict(config.uarch),
            "model": dict(config.model),
            "train": dict(config.train),
        },
        "history": list(history),
    }
    # The watchdog may inspect checkpoints while training is running.  Write
    # beside the destination and replace atomically so it never loads a
    # partially serialized model/optimizer state.
    temporary = f"{path}.tmp-{os.getpid()}"
    try:
        torch.save(payload, temporary)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def _torch_load(path: str, device: torch.device) -> Any:
    try:
        return torch.load(path, map_location=device, weights_only=False)
    except TypeError:  # older torch
        return torch.load(path, map_location=device)


def _load_checkpoint(
    path: str,
    model: torch.nn.Module,
    optimizer: Optional[torch.optim.Optimizer],
    device: torch.device,
    contract: Mapping[str, Any],
) -> Tuple[TrainState, List[Dict[str, Any]]]:
    payload = _torch_load(path, device)
    if payload.get("checkpoint_schema") != CHECKPOINT_SCHEMA_VERSION:
        raise RuntimeError("checkpoint is not a v29 checkpoint")
    if payload.get("contract") != dict(contract):
        raise RuntimeError("v29 checkpoint/cache contract mismatch")
    model.load_state_dict(payload["model"])
    if optimizer is not None and payload.get("optimizer"):
        optimizer.load_state_dict(payload["optimizer"])
    return TrainState(
        step=int(payload.get("step", 0)),
        best_validation=float(payload.get("best_validation", float("inf"))),
        best_post_coverage_validation=float(
            payload.get("best_post_coverage_validation", float("inf"))
        ),
    ), list(payload.get("history", []))


def _contract_without_long_history(
    contract: Mapping[str, Any],
) -> Dict[str, Any]:
    out = dict(contract)
    out.pop("long_history", None)
    return out


def _contract_without_gss(contract: Mapping[str, Any]) -> Dict[str, Any]:
    out = dict(contract)
    out.pop("gss", None)
    return out


def _contract_without_gss_or_exposure(
    contract: Mapping[str, Any],
) -> Dict[str, Any]:
    out = dict(contract)
    out.pop("gss", None)
    out.pop("exposure", None)
    return out


def _contract_without_exposure(contract: Mapping[str, Any]) -> Dict[str, Any]:
    out = dict(contract)
    out.pop("exposure", None)
    return out


def _initialize_frozen_memory_probe(
    path: str,
    model: TCSimV29Model,
    contract: Mapping[str, Any],
) -> Dict[str, Any]:
    """Load the exact E0 state while allowing only the new correction modules."""
    payload = _torch_load(path, torch.device("cpu"))
    if not isinstance(payload, Mapping):
        raise RuntimeError("v29 frozen probe initialization is not a checkpoint")
    if payload.get("checkpoint_schema") != CHECKPOINT_SCHEMA_VERSION:
        raise RuntimeError("v29 frozen probe initialization is not a v29 checkpoint")
    source_contract = payload.get("contract")
    if not isinstance(source_contract, Mapping):
        raise RuntimeError("v29 frozen probe initialization lacks a contract")
    if (
        _contract_without_long_history(source_contract)
        != _contract_without_long_history(contract)
    ):
        raise RuntimeError(
            "v29 frozen probe E0/cache base contract mismatch "
            "(long_history is the only allowed contract difference)"
        )
    incompatible = model.load_state_dict(payload["model"], strict=False)
    allowed_missing_prefixes = (
        "memory_history_projection.",
        "memory_correction_head.",
    )
    disallowed_missing = [
        key for key in incompatible.missing_keys
        if not key.startswith(allowed_missing_prefixes)
    ]
    if disallowed_missing or incompatible.unexpected_keys:
        raise RuntimeError(
            "v29 frozen probe E0 state mismatch: "
            f"missing={disallowed_missing} "
            f"unexpected={list(incompatible.unexpected_keys)}"
        )
    expected_new = {
        key for key in model.state_dict()
        if key.startswith(allowed_missing_prefixes)
    }
    observed_new = set(incompatible.missing_keys)
    if observed_new != expected_new:
        raise RuntimeError(
            "v29 frozen probe did not isolate exactly the new correction state: "
            f"missing={sorted(observed_new)} expected={sorted(expected_new)}"
        )
    metadata = {
        "source_checkpoint": os.path.abspath(path),
        "source_step": int(payload.get("step", 0)),
        "source_best_validation": float(
            payload.get("best_validation", float("nan"))
        ),
        "new_state_keys": sorted(observed_new),
    }
    del payload
    return metadata


def _configure_frozen_memory_probe(model: TCSimV29Model) -> List[str]:
    """Freeze E0 completely and expose only the opt-in correction parameters."""
    if (
        model.long_history_mode
        != LONG_HISTORY_MODE_MEMORY_GATED_CORRECTION
    ):
        raise RuntimeError(
            "frozen_memory_probe requires "
            "long_history_mode=memory_gated_timing_correction"
        )
    if (
        model.memory_history_projection is None
        or model.memory_correction_head is None
    ):
        raise RuntimeError("frozen_memory_probe correction modules are missing")
    for parameter in model.parameters():
        parameter.requires_grad_(False)
    for module in (
        model.memory_history_projection,
        model.memory_correction_head,
    ):
        for parameter in module.parameters():
            parameter.requires_grad_(True)
    names = [
        name for name, parameter in model.named_parameters()
        if parameter.requires_grad
    ]
    if not names:
        raise RuntimeError("frozen_memory_probe has no trainable parameters")
    return names


def _set_frozen_memory_probe_train_mode(model: TCSimV29Model) -> None:
    """Keep the frozen E0 backbone at inference semantics while training Corr."""
    model.static_encoder.eval()
    model.interaction.eval()
    model.gap_head.eval()
    if model.branch_head is not None:
        model.branch_head.eval()
    assert model.memory_history_projection is not None
    assert model.memory_correction_head is not None
    model.memory_history_projection.train()
    model.memory_correction_head.train()


def _initialize_frozen_gss_probe(
    path: str,
    model: TCSimV29Model,
    contract: Mapping[str, Any],
) -> Dict[str, Any]:
    """Load canonical v29 while allowing exactly the new GSS adapter state."""
    payload = _torch_load(path, torch.device("cpu"))
    if not isinstance(payload, Mapping):
        raise RuntimeError("v30 GSS probe initialization is not a checkpoint")
    if payload.get("checkpoint_schema") != CHECKPOINT_SCHEMA_VERSION:
        raise RuntimeError("v30 GSS probe initialization is not a v29 checkpoint")
    source_contract = payload.get("contract")
    if not isinstance(source_contract, Mapping):
        raise RuntimeError("v30 GSS probe initialization lacks a contract")
    if _contract_without_gss(source_contract) != _contract_without_gss(contract):
        raise RuntimeError(
            "v30 GSS probe canonical/cache base contract mismatch "
            "(GSS is the only allowed contract difference)"
        )
    incompatible = model.load_state_dict(payload["model"], strict=False)
    expected_new = {
        key for key in model.state_dict() if key.startswith("gss_adapter.")
    }
    observed_new = set(incompatible.missing_keys)
    if observed_new != expected_new or incompatible.unexpected_keys:
        raise RuntimeError(
            "v30 GSS probe did not isolate exactly the adapter state: "
            f"missing={sorted(observed_new)} expected={sorted(expected_new)} "
            f"unexpected={list(incompatible.unexpected_keys)}"
        )
    metadata = {
        "source_checkpoint": os.path.abspath(path),
        "source_step": int(payload.get("step", 0)),
        "source_best_validation": float(
            payload.get("best_validation", float("nan"))
        ),
        "new_state_keys": sorted(observed_new),
    }
    del payload
    return metadata


def _configure_frozen_gss_probe(model: TCSimV29Model) -> List[str]:
    """Freeze canonical v29 and expose only the causal G1 adapter."""
    if model.gss_mode not in GSS_ADAPTER_MODES or model.gss_adapter is None:
        raise RuntimeError(
            "frozen_gss_probe requires a causal GSS adapter mode"
        )
    for parameter in model.parameters():
        parameter.requires_grad_(False)
    for parameter in model.gss_adapter.parameters():
        parameter.requires_grad_(True)
    names = [
        name for name, parameter in model.named_parameters()
        if parameter.requires_grad
    ]
    if not names or any(not name.startswith("gss_adapter.") for name in names):
        raise RuntimeError("frozen_gss_probe trainable-state isolation failed")
    return names


def _set_frozen_gss_probe_train_mode(model: TCSimV29Model) -> None:
    """Keep canonical v29 at eval semantics while training the G1 adapter."""
    model.eval()
    assert model.gss_adapter is not None
    model.gss_adapter.train()


def _initialize_frozen_gss_gate_probe(
    path: str,
    model: TCSimV29Model,
    contract: Mapping[str, Any],
) -> Dict[str, Any]:
    """Load a trained G1 probe while allowing exactly the new gate state."""
    payload = _torch_load(path, torch.device("cpu"))
    if not isinstance(payload, Mapping):
        raise RuntimeError("v30 GSS gate initialization is not a checkpoint")
    if payload.get("checkpoint_schema") != CHECKPOINT_SCHEMA_VERSION:
        raise RuntimeError("v30 GSS gate initialization is not a v29 checkpoint")
    source_contract = payload.get("contract")
    if not isinstance(source_contract, Mapping):
        raise RuntimeError("v30 GSS gate initialization lacks a contract")
    if source_contract != dict(contract):
        raise RuntimeError("v30 GSS gate source/cache contract mismatch")
    incompatible = model.load_state_dict(payload["model"], strict=False)
    expected_new = {
        key for key in model.state_dict()
        if key.startswith("gss_exposure_gate.")
    }
    observed_new = set(incompatible.missing_keys)
    if observed_new != expected_new or incompatible.unexpected_keys:
        raise RuntimeError(
            "v30 GSS gate did not isolate exactly the gate state: "
            f"missing={sorted(observed_new)} expected={sorted(expected_new)} "
            f"unexpected={list(incompatible.unexpected_keys)}"
        )
    metadata = {
        "source_checkpoint": os.path.abspath(path),
        "source_step": int(payload.get("step", 0)),
        "source_best_validation": float(
            payload.get("best_validation", float("nan"))
        ),
        "new_state_keys": sorted(observed_new),
    }
    del payload
    return metadata


def _configure_frozen_gss_gate_probe(model: TCSimV29Model) -> List[str]:
    """Freeze v29 and the trained G1 adapter; expose only the gate."""
    if model.gss_adapter is None or model.gss_exposure_gate is None:
        raise RuntimeError(
            "frozen_gss_gate_probe requires a GSS adapter and exposure gate"
        )
    for parameter in model.parameters():
        parameter.requires_grad_(False)
    for parameter in model.gss_exposure_gate.parameters():
        parameter.requires_grad_(True)
    names = [
        name for name, parameter in model.named_parameters()
        if parameter.requires_grad
    ]
    if not names or any(
        not name.startswith("gss_exposure_gate.") for name in names
    ):
        raise RuntimeError("frozen_gss_gate_probe trainable-state isolation failed")
    return names


def _set_frozen_gss_gate_probe_train_mode(model: TCSimV29Model) -> None:
    """Keep every source module deterministic while training only the gate."""
    model.eval()
    assert model.gss_adapter is not None
    assert model.gss_exposure_gate is not None
    model.gss_adapter.eval()
    model.gss_exposure_gate.train()


def _initialize_joint_gss_v2(
    path: str,
    model: TCSimV29Model,
    contract: Mapping[str, Any],
) -> Dict[str, Any]:
    """Initialize formal joint-v2 from P1 or the canonical v29 backbone.

    A commit-clock run must not resume or relabel a ready-clock checkpoint.
    For the formal Exposure-v1 run we instead import only the canonical v29
    tensors and create a zero-output GSS adapter plus a fresh router.  This is
    parameter initialization, not checkpoint/data-contract continuation.
    """
    payload = _torch_load(path, torch.device("cpu"))
    if not isinstance(payload, Mapping):
        raise RuntimeError("v30 joint-v2 initialization is not a checkpoint")
    if payload.get("checkpoint_schema") != CHECKPOINT_SCHEMA_VERSION:
        raise RuntimeError("v30 joint-v2 source is not a v29 checkpoint")
    source_contract = payload.get("contract")
    if not isinstance(source_contract, Mapping):
        raise RuntimeError("v30 joint-v2 initialization lacks a contract")
    source_has_gss = source_contract.get("gss") is not None
    if source_has_gss:
        compatible = (
            _contract_without_exposure(source_contract)
            == _contract_without_exposure(contract)
        )
        initialization_mode = "p1_same_gss_contract"
    else:
        compatible = (
            _contract_without_gss_or_exposure(source_contract)
            == _contract_without_gss_or_exposure(contract)
        )
        initialization_mode = "canonical_v29_new_adapter_and_router"
    if not compatible:
        raise RuntimeError(
            "v30 joint-v2 source/cache base contract mismatch; ready-clock "
            "v30 checkpoints cannot initialize a commit-clock run"
        )
    if model.gss_adapter is None or model.gss_strength_router is None:
        raise RuntimeError("v30 joint-v2 requires GSS adapter and strength router")
    incompatible = model.load_state_dict(payload["model"], strict=False)
    expected_prefixes = (
        ("gss_strength_router.",)
        if source_has_gss else ("gss_adapter.", "gss_strength_router.")
    )
    expected_new = {
        key for key in model.state_dict()
        if key.startswith(expected_prefixes)
    }
    observed_new = set(incompatible.missing_keys)
    if observed_new != expected_new or incompatible.unexpected_keys:
        raise RuntimeError(
            "v30 joint-v2 source did not isolate exactly the router: "
            f"missing={sorted(observed_new)} expected={sorted(expected_new)} "
            f"unexpected={list(incompatible.unexpected_keys)}"
        )
    metadata = {
        "source_checkpoint": os.path.abspath(path),
        "source_step": int(payload.get("step", 0)),
        "source_best_validation": float(
            payload.get("best_validation", float("nan"))
        ),
        "initialization_mode": initialization_mode,
        "new_state_keys": sorted(observed_new),
    }
    del payload
    return metadata


def _configure_joint_gss_v2(model: TCSimV29Model) -> List[str]:
    """Prepare all timing parameters for scheduled joint fine-tuning."""
    if model.gss_adapter is None or model.gss_strength_router is None:
        raise RuntimeError("joint_gss_v2 requires adapter and strength router")
    if model.gss_exposure_gate is not None:
        raise RuntimeError("joint_gss_v2 cannot include the legacy exposure gate")
    for parameter in model.parameters():
        parameter.requires_grad_(True)
    # Branch supervision is deliberately absent from v30-G1-Joint-v2.  Keep
    # the inherited PMU head bit-stable while the timing path adapts.
    if model.branch_head is not None:
        for parameter in model.branch_head.parameters():
            parameter.requires_grad_(False)
    names = [
        name for name, parameter in model.named_parameters()
        if parameter.requires_grad
    ]
    if not names or not any(
        name.startswith("gss_strength_router.") for name in names
    ):
        raise RuntimeError("joint_gss_v2 trainable-state setup failed")
    return names


def _set_joint_gss_v2_train_mode(
    model: TCSimV29Model, step: int, config: TCSimConfig,
) -> str:
    """Match dropout semantics to the scheduled no-update/update stages."""
    gate_only_until = int(config.train.get("joint_v2_gate_only_steps", 2000))
    partial_until = int(config.train.get("joint_v2_partial_steps", 8000))
    model.eval()
    assert model.gss_strength_router is not None
    model.gss_strength_router.train()
    if int(step) <= gate_only_until:
        if bool(config.train.get("joint_v2_bootstrap_adapter_from_v29", False)):
            assert model.gss_adapter is not None
            model.gss_adapter.train()
            return "adapter_router_bootstrap"
        return "gate"
    assert model.gss_adapter is not None
    model.gss_adapter.train()
    model.gap_head.train()
    last_layers = max(1, int(config.train.get("joint_v2_last_layers", 2)))
    for layer in model.interaction.layers[-last_layers:]:
        layer.train()
    if int(step) <= partial_until:
        return "partial"
    model.static_encoder.train()
    model.interaction.train()
    if model.branch_head is not None:
        model.branch_head.eval()
    return "full"


def _joint_v2_optimizer_groups(
    model: TCSimV29Model, config: TCSimConfig,
) -> List[Dict[str, Any]]:
    """Build disjoint parameter groups with explicit unfreeze schedules."""
    last_layers = max(1, int(config.train.get("joint_v2_last_layers", 2)))
    layer_count = len(model.interaction.layers)
    last_start = max(0, layer_count - last_layers)
    gate_only_until = int(config.train.get("joint_v2_gate_only_steps", 2000))
    partial_until = int(config.train.get("joint_v2_partial_steps", 8000))
    specifications = {
        "router": (
            float(config.train.get("joint_v2_router_lr", 3.0e-4)), 0,
        ),
        "adapter": (
            float(config.train.get("joint_v2_adapter_lr", 1.0e-4)),
            (
                0
                if bool(config.train.get(
                    "joint_v2_bootstrap_adapter_from_v29", False,
                ))
                else gate_only_until
            ),
        ),
        "timing_head": (
            float(config.train.get("joint_v2_timing_head_lr", 3.0e-5)),
            gate_only_until,
        ),
        "last_qkvr": (
            float(config.train.get("joint_v2_last_qkvr_lr", 1.0e-5)),
            gate_only_until,
        ),
        "base_qkvr": (
            float(config.train.get("joint_v2_base_qkvr_lr", 5.0e-6)),
            partial_until,
        ),
        "static": (
            float(config.train.get("joint_v2_static_lr", 3.0e-6)),
            partial_until,
        ),
    }
    buckets: Dict[str, List[torch.nn.Parameter]] = {
        name: [] for name in specifications
    }
    assigned = set()
    for name, parameter in model.named_parameters():
        if not parameter.requires_grad:
            continue
        if name.startswith("gss_strength_router."):
            group = "router"
        elif name.startswith("gss_adapter."):
            group = "adapter"
        elif name.startswith("gap_head."):
            group = "timing_head"
        elif name.startswith("static_encoder."):
            group = "static"
        elif name.startswith("interaction.layers."):
            parts = name.split(".")
            layer_index = int(parts[2])
            group = "last_qkvr" if layer_index >= last_start else "base_qkvr"
        elif name.startswith("interaction."):
            group = "base_qkvr"
        else:
            raise RuntimeError(f"joint_gss_v2 parameter is ungrouped: {name}")
        buckets[group].append(parameter)
        assigned.add(id(parameter))
    expected = {
        id(parameter) for parameter in model.parameters()
        if parameter.requires_grad
    }
    if assigned != expected:
        raise RuntimeError("joint_gss_v2 optimizer grouping is incomplete")
    weight_decay = float(config.train.get("weight_decay", 1.0e-2))
    groups = []
    for name, (base_lr, unfreeze_step) in specifications.items():
        if not buckets[name]:
            raise RuntimeError(f"joint_gss_v2 optimizer group {name} is empty")
        groups.append({
            "params": buckets[name],
            "lr": 0.0,
            "base_lr": float(base_lr),
            "unfreeze_step": int(unfreeze_step),
            "group_name": name,
            "weight_decay": weight_decay,
        })
    return groups


def _update_joint_v2_learning_rates(
    optimizer: torch.optim.Optimizer,
    step: int,
    config: TCSimConfig,
    coverage_steps: int,
) -> Dict[str, float]:
    warmup = max(1, int(config.train.get("joint_v2_warmup_steps", 500)))
    target_steps = max(
        1, int(config.train.get("joint_v2_target_steps", 60000)),
    )
    partial_until = int(config.train.get("joint_v2_partial_steps", 8000))
    full_coverage = int(coverage_steps) + partial_until
    decay_floor = float(config.train.get("joint_v2_lr_decay_floor", 0.1))
    out: Dict[str, float] = {}
    for group in optimizer.param_groups:
        name = str(group.get("group_name", "unnamed"))
        base_lr = float(group.get("base_lr", group.get("lr", 0.0)))
        unfreeze = int(group.get("unfreeze_step", 0))
        active_steps = int(step) - unfreeze
        if active_steps <= 0:
            factor = 0.0
        elif active_steps < warmup:
            factor = active_steps / warmup
        else:
            decay_start = (
                int(coverage_steps)
                if name in {"router", "adapter", "timing_head", "last_qkvr"}
                else full_coverage
            )
            if int(step) <= decay_start:
                factor = 1.0
            else:
                span = max(1, target_steps - decay_start)
                progress = min(1.0, (int(step) - decay_start) / span)
                cosine = 0.5 * (1.0 + math.cos(math.pi * progress))
                factor = decay_floor + (1.0 - decay_floor) * cosine
        group["lr"] = base_lr * factor
        out[name] = float(group["lr"])
    return out


def _clear_zero_lr_gradients(optimizer: torch.optim.Optimizer) -> None:
    """Keep scheduled-zero groups free of stale Adam moments."""
    for group in optimizer.param_groups:
        if float(group.get("lr", 0.0)) != 0.0:
            continue
        for parameter in group["params"]:
            parameter.grad = None


def _gss_gate_auxiliary_losses(
    predictions: Mapping[str, torch.Tensor],
    batch: Mapping[str, torch.Tensor],
    config: TCSimConfig,
) -> Tuple[torch.Tensor, torch.Tensor]:
    """Return per-token v29 regret and low-weight local-gap supervision."""
    required = (
        "commit_time", "retirement_gap", "gss_reference_commit_time",
    )
    missing = [key for key in required if key not in predictions]
    if missing:
        raise RuntimeError(f"GSS gate auxiliary losses lack outputs {missing}")
    valid = batch["valid_uop_mask"].bool()
    true_time = batch["commit_time_target"].clamp(min=0.0).float()
    candidate_error = (
        torch.log1p(predictions["commit_time"].clamp(min=0.0).float())
        - torch.log1p(true_time)
    ).abs()
    reference_error = (
        torch.log1p(
            predictions["gss_reference_commit_time"].clamp(min=0.0).float()
        ) - torch.log1p(true_time)
    ).abs()
    margin = float(config.train.get("gss_gate_regret_margin", 0.0))
    regret_element = F.relu(candidate_error - reference_error - margin)
    weight = valid.to(regret_element.dtype)
    regret = (regret_element * weight).sum() / weight.sum().clamp(min=1.0)

    true_gap = torch.cat([
        true_time[:, :1],
        (true_time[:, 1:] - true_time[:, :-1]).clamp(min=0.0),
    ], dim=1)
    predicted_gap = predictions["retirement_gap"].clamp(min=0.0).float()
    gap_element = F.smooth_l1_loss(
        torch.log1p(predicted_gap),
        torch.log1p(true_gap),
        beta=float(config.train.get("gss_gate_gap_huber_beta", 0.2)),
        reduction="none",
    )
    gap = (gap_element * weight).sum() / weight.sum().clamp(min=1.0)
    return regret, gap


def _joint_v2_auxiliary_losses(
    predictions: Mapping[str, torch.Tensor],
    batch: Mapping[str, torch.Tensor],
    config: TCSimConfig,
) -> Dict[str, torch.Tensor]:
    """Ground-truth local timing and direct counterfactual routing."""
    required = (
        "retirement_gap", "commit_time", "gss_anchor_gaps",
        "gss_anchor_commit_time", "gss_router_logits",
        "gss_router_weights",
    )
    missing = [key for key in required if key not in predictions]
    if missing:
        raise RuntimeError(f"joint_gss_v2 predictions lack outputs {missing}")
    valid = batch["valid_uop_mask"].bool()
    weight = valid.float()
    denominator = weight.sum().clamp(min=1.0)
    true_time = batch["commit_time_target"].clamp(min=0.0).float()
    true_gap = torch.cat([
        true_time[:, :1],
        (true_time[:, 1:] - true_time[:, :-1]).clamp(min=0.0),
    ], dim=1)

    predicted_gap = predictions["retirement_gap"].clamp(min=0.0).float()
    gap_element = F.smooth_l1_loss(
        torch.log1p(predicted_gap),
        torch.log1p(true_gap),
        beta=float(config.train.get("joint_v2_gap_huber_beta", 0.2)),
        reduction="none",
    )
    local_gap = (gap_element * weight).sum() / denominator

    anchor_gap = predictions["gss_anchor_gaps"].clamp(min=0.0).float()
    anchor_time = predictions["gss_anchor_commit_time"].clamp(min=0.0).float()
    local_error = (
        torch.log1p(anchor_gap)
        - torch.log1p(true_gap).unsqueeze(-1)
    ).abs()
    prefix_error = (
        torch.log1p(anchor_time)
        - torch.log1p(true_time).unsqueeze(-1)
    ).abs()
    local_fraction = float(
        config.train.get("joint_v2_rank_local_fraction", 0.7)
    )
    expert_error = (
        local_fraction * local_error
        + (1.0 - local_fraction) * prefix_error
    )
    spread = expert_error.max(dim=-1).values - expert_error.min(dim=-1).values
    decisive = valid & (
        spread >= float(config.train.get("joint_v2_rank_margin", 0.01))
    )
    temperature = max(
        1.0e-4, float(config.train.get("joint_v2_rank_temperature", 0.05)),
    )
    target = torch.softmax(-expert_error.detach() / temperature, dim=-1)
    rank_element = -(
        target
        * F.log_softmax(predictions["gss_router_logits"].float(), dim=-1)
    ).sum(dim=-1)
    rank_weight = decisive.float()
    counterfactual_rank = (
        (rank_element * rank_weight).sum()
        / rank_weight.sum().clamp(min=1.0)
    )

    router_weights = predictions["gss_router_weights"].float()
    entropy = -(
        router_weights.clamp(min=1.0e-8).log() * router_weights
    ).sum(dim=-1)
    router_entropy = (entropy * weight).sum() / denominator
    decisive_fraction = (decisive.float() * weight).sum() / denominator
    return {
        "local_gap": local_gap,
        "counterfactual_rank": counterfactual_rank,
        "router_entropy": router_entropy,
        "decisive_fraction": decisive_fraction,
    }


def _loss_kwargs(config: TCSimConfig) -> Dict[str, Any]:
    return {
        "weights": dict(config.train.get("loss_weights", {})),
        "time_beta": float(config.train.get("time_huber_beta", 0.2)),
        "progress_count_beta": float(config.train.get("progress_count_beta", 8.0)),
        "branch_count_beta": float(config.train.get("branch_count_beta", 1.0)),
    }


def _balanced_validation_indices(
    dataset: V29GlobalTimeDataset,
    maximum: int,
) -> List[int]:
    """Select deterministic, time-spread, trace-equal validation sequences."""
    total = len(dataset)
    maximum = int(maximum)
    if maximum <= 0 or total <= maximum:
        return list(range(total))
    by_trace: Dict[str, List[int]] = {}
    for index, trace_id in enumerate(dataset.sample_trace_ids):
        by_trace.setdefault(str(trace_id), []).append(index)
    names = sorted(by_trace)
    # Never silently omit a validation trace merely because a cap was set too
    # low.  The effective cap is at least one sequence per trace.
    budget = min(total, max(maximum, len(names)))
    allocation = {name: 0 for name in names}
    allocated = 0
    while allocated < budget:
        progressed = False
        for name in names:
            if allocated >= budget:
                break
            if allocation[name] >= len(by_trace[name]):
                continue
            allocation[name] += 1
            allocated += 1
            progressed = True
        if not progressed:
            break

    selected_by_trace: Dict[str, List[int]] = {}
    for name in names:
        indices = by_trace[name]
        count = allocation[name]
        if count <= 0:
            selected_by_trace[name] = []
        elif count >= len(indices):
            selected_by_trace[name] = list(indices)
        elif count == 1:
            selected_by_trace[name] = [indices[len(indices) // 2]]
        else:
            positions = [
                int(round(position * (len(indices) - 1) / (count - 1)))
                for position in range(count)
            ]
            selected_by_trace[name] = [indices[position] for position in positions]

    # Interleave traces so rank-strided DDP partitioning remains balanced.
    out: List[int] = []
    for position in range(max(allocation.values(), default=0)):
        for name in names:
            values = selected_by_trace[name]
            if position < len(values):
                out.append(values[position])
    if len(out) != budget or len(set(out)) != len(out):
        raise RuntimeError("invalid v29 balanced validation subset")
    return out


@torch.no_grad()
def evaluate(
    model: torch.nn.Module,
    loader: DataLoader,
    device: torch.device,
    config: TCSimConfig,
) -> Dict[str, float]:
    model.eval()
    # Validation ranks may own one different number of sequences.  Calling the
    # DDP wrapper would broadcast buffers on every forward and can deadlock
    # when one rank reaches the final all-reduce first.  Parameters are already
    # synchronized after training; local validation uses the raw module and
    # reduces only the final metric accumulators.
    evaluation_model = _raw_model(model)
    amp = _amp_dtype(str(config.train.get("amp_dtype", "none")), device)
    totals = torch.zeros(10, dtype=torch.float64, device=device)
    for batch in loader:
        batch = _to_device(batch, device)
        with torch.autocast(
            device_type=device.type,
            dtype=amp,
            enabled=amp is not None,
        ):
            predictions = evaluation_model(batch)
            losses = compute_v29_losses(predictions, batch, **_loss_kwargs(config))
        totals += torch.tensor([
            float(losses.total),
            float(losses.commit_time),
            float(losses.prefix_bce),
            float(losses.progress_count),
            float(losses.cumulative),
            float(losses.branch_token),
            float(losses.branch_count),
            float(losses.commit_log_mae),
            float(losses.progress_mae),
            1.0,
        ], dtype=torch.float64, device=device)
        if losses.monotonic_violations:
            raise RuntimeError("v29 model produced a monotonicity violation")
    if dist.is_available() and dist.is_initialized():
        dist.all_reduce(totals, op=dist.ReduceOp.SUM)
    count = max(1.0, float(totals[-1]))
    names = (
        "total", "commit_time", "prefix_bce", "progress_count", "cumulative",
        "branch_token", "branch_count", "commit_log_mae", "progress_mae",
    )
    return {f"val_{name}": float(totals[index]) / count for index, name in enumerate(names)}


def train_one_run(
    train_sources: Sequence[Any],
    validation_sources: Sequence[Any],
    out_dir: str,
    config: TCSimConfig,
    *,
    device: str = "auto",
    max_steps: Optional[int] = None,
    resume: Optional[str] = None,
    init_checkpoint: Optional[str] = None,
) -> Dict[str, Any]:
    distributed, rank, local_rank, world, torch_device = _setup_distributed(device)
    seed = int(config.train.get("seed", 1234))
    random.seed(seed + rank)
    torch.manual_seed(seed + rank)
    sequence_length = int(config.chunk.get("sequence_length", 4))
    sequence_stride = int(config.chunk.get("sequence_stride", sequence_length))
    train_data = V29GlobalTimeDataset(
        train_sources,
        sequence_length=sequence_length,
        sequence_stride=sequence_stride,
    )
    validation_data = V29GlobalTimeDataset(
        validation_sources,
        sequence_length=sequence_length,
        sequence_stride=sequence_stride,
    ) if validation_sources else None
    contract = _dataset_contract(train_data)
    if validation_data is not None and _dataset_contract(validation_data) != contract:
        raise RuntimeError("v29 training/validation contracts differ")
    configured_horizons = tuple(float(value) for value in config.chunk.get("horizons", []))
    if configured_horizons and configured_horizons != tuple(contract["horizons"]):
        raise RuntimeError("v29 config/cache horizon mismatch")
    configured_history_dim = int(config.model.get("long_history_dim", 0))
    cached_history_dim = int(
        contract.get("long_history", {}).get("output_dim", 0)
    )
    if configured_history_dim != cached_history_dim:
        raise RuntimeError(
            "v29 config/cache long-history dimension mismatch: "
            f"{configured_history_dim} != {cached_history_dim}"
        )
    branch_mode = str(
        config.model.get("branch_mode", BRANCH_MODE_NEURAL_HEAD)
    ).strip().lower()
    cached_branch_contract = contract.get("branch_features")
    if branch_mode in BRANCH_MODES_WITHOUT_HEAD and cached_branch_contract is None:
        raise RuntimeError(
            f"v29 branch_mode={branch_mode} requires branch replay sidecars"
        )
    branch_weights = dict(config.train.get("loss_weights", {}))
    if branch_mode in BRANCH_MODES_WITHOUT_HEAD and (
        float(branch_weights.get("branch_token", 0.0)) != 0.0
        or float(branch_weights.get("branch_count", 0.0)) != 0.0
    ):
        raise RuntimeError(
            f"v29 branch_mode={branch_mode} requires zero branch_token/branch_count loss"
        )
    gss_mode = str(config.model.get("gss_mode", GSS_MODE_NONE)).strip().lower()
    cached_gss_contract = contract.get("gss")
    cached_exposure_contract = contract.get("exposure")
    if gss_mode in GSS_ADAPTER_MODES and cached_gss_contract is None:
        raise RuntimeError(
            f"v30 gss_mode={gss_mode} requires GSS sidecars"
        )
    if gss_mode == GSS_MODE_NONE and cached_gss_contract is not None:
        raise RuntimeError(
            "GSS sidecars are present but model.gss_mode=none; use the canonical "
            "v29 manifest for non-GSS training"
        )
    exposure_enabled = bool(config.model.get("gss_exposure_features", False))
    if exposure_enabled and cached_exposure_contract is None:
        raise RuntimeError(
            "model.gss_exposure_features=true requires Exposure-v1 sidecars"
        )
    if not exposure_enabled and cached_exposure_contract is not None:
        raise RuntimeError(
            "Exposure-v1 sidecars are present but model.gss_exposure_features=false"
        )

    batch_size = int(config.train.get("batch_samples", 1))
    sampler = None
    shuffle = True
    drop_last = False
    if bool(config.train.get("trace_balanced_sampling", True)):
        if bool(config.train.get("coverage_first_sampling", False)):
            sampler = CoverageFirstTraceBalancedSampler(
                train_data.sample_trace_ids,
                num_replicas=world if distributed else 1,
                rank=rank if distributed else 0,
                seed=seed,
                coverage_epochs=int(
                    config.train.get("coverage_first_epochs", 1)
                ),
            )
        else:
            weights = [
                1.0 / max(1, train_data.trace_sample_counts[trace_id])
                for trace_id in train_data.sample_trace_ids
            ]
            sampler = WeightedRandomSampler(
                weights,
                num_samples=(
                    math.ceil(len(train_data) / world)
                    if distributed else len(train_data)
                ),
                replacement=True,
                generator=torch.Generator().manual_seed(seed + rank),
            )
        shuffle = False
        drop_last = distributed
    elif distributed:
        sampler = DistributedSampler(
            train_data, num_replicas=world, rank=rank, shuffle=True, drop_last=True,
        )
        shuffle = False
        drop_last = True
    train_loader = DataLoader(
        train_data,
        batch_size=batch_size,
        shuffle=shuffle and sampler is None,
        sampler=sampler,
        collate_fn=collate_v29_sequences,
        num_workers=int(config.train.get("num_workers", 0)),
        pin_memory=torch_device.type == "cuda",
        drop_last=drop_last,
    )
    validation_loader = None
    validation_indices: List[int] = []
    if validation_data is not None:
        validation_indices = _balanced_validation_indices(
            validation_data,
            int(config.train.get("validation_max_sequences", 0)),
        )
        validation_loader = DataLoader(
            validation_data,
            batch_size=batch_size,
            shuffle=False,
            sampler=(
                validation_indices[rank::world]
                if distributed else validation_indices
            ),
            collate_fn=collate_v29_sequences,
            num_workers=int(config.train.get("num_workers", 0)),
            pin_memory=torch_device.type == "cuda",
        )

    if resume and init_checkpoint:
        raise RuntimeError("v29 --resume and --init-checkpoint are mutually exclusive")
    frozen_memory_probe = bool(config.train.get("frozen_memory_probe", False))
    frozen_gss_probe = bool(config.train.get("frozen_gss_probe", False))
    frozen_gss_gate_probe = bool(
        config.train.get("frozen_gss_gate_probe", False)
    )
    joint_gss_v2 = bool(config.train.get("joint_gss_v2", False))
    if sum(map(int, (
        frozen_memory_probe, frozen_gss_probe, frozen_gss_gate_probe,
        joint_gss_v2,
    ))) > 1:
        raise RuntimeError("v29/v30 probe/joint modes are mutually exclusive")
    frozen_probe = (
        frozen_memory_probe or frozen_gss_probe or frozen_gss_gate_probe
    )
    if init_checkpoint and not (frozen_probe or joint_gss_v2):
        raise RuntimeError(
            "--init-checkpoint is restricted to a frozen probe or joint_gss_v2"
        )
    if frozen_probe and not resume and not init_checkpoint:
        raise RuntimeError(
            "a fresh frozen probe requires --init-checkpoint"
        )
    if joint_gss_v2 and not resume and not init_checkpoint:
        raise RuntimeError("a fresh joint_gss_v2 run requires --init-checkpoint")
    model = build_model(config.model, contract["horizons"]).to(torch_device)
    initialization: Optional[Dict[str, Any]] = None
    if init_checkpoint and (rank == 0 or not distributed):
        if joint_gss_v2:
            initialization = _initialize_joint_gss_v2(
                init_checkpoint, model, contract,
            )
        elif frozen_gss_gate_probe:
            initialization = _initialize_frozen_gss_gate_probe(
                init_checkpoint, model, contract,
            )
        elif frozen_gss_probe:
            initialization = _initialize_frozen_gss_probe(
                init_checkpoint, model, contract,
            )
        else:
            initialization = _initialize_frozen_memory_probe(
                init_checkpoint, model, contract,
            )
        print(
            "[v29/v30 init] "
            f"source={initialization['source_checkpoint']} "
            f"source_ckpt_step={initialization['source_step']} "
            f"mode={initialization.get('initialization_mode', 'legacy')} "
            f"new_state_keys={len(initialization['new_state_keys'])}",
            flush=True,
        )
    trainable_names: List[str] = []
    if joint_gss_v2:
        trainable_names = _configure_joint_gss_v2(model)
        if init_checkpoint:
            config.train = {
                **config.train,
                "init_checkpoint": os.path.abspath(init_checkpoint),
            }
    elif frozen_probe:
        if frozen_gss_gate_probe:
            trainable_names = _configure_frozen_gss_gate_probe(model)
        elif frozen_gss_probe:
            trainable_names = _configure_frozen_gss_probe(model)
        else:
            trainable_names = _configure_frozen_memory_probe(model)
        if init_checkpoint:
            config.train = {
                **config.train,
                "init_checkpoint": os.path.abspath(init_checkpoint),
            }
    if distributed:
        model = DDP(
            model,
            device_ids=[local_rank] if torch_device.type == "cuda" else None,
        )
    trainable_parameters = [
        parameter for parameter in model.parameters()
        if parameter.requires_grad
    ]
    if not trainable_parameters:
        raise RuntimeError("v29 training has no trainable parameters")
    if joint_gss_v2:
        optimizer = torch.optim.AdamW(
            _joint_v2_optimizer_groups(_raw_model(model), config),
        )
    else:
        optimizer = torch.optim.AdamW(
            trainable_parameters,
            lr=float(config.train.get("lr", 1e-4)),
            weight_decay=float(config.train.get("weight_decay", 5e-2)),
        )
    state = TrainState()
    history: List[Dict[str, Any]] = []
    if resume:
        state, history = _load_checkpoint(
            resume, _raw_model(model), optimizer, torch_device, contract,
        )
    sampler_steps_per_epoch = (
        sampler.num_samples
        if isinstance(sampler, CoverageFirstTraceBalancedSampler)
        else len(train_loader)
    )
    start_epoch = (
        state.step // sampler_steps_per_epoch
        if isinstance(sampler, CoverageFirstTraceBalancedSampler) else 0
    )
    start_epoch_offset = (
        state.step % sampler_steps_per_epoch
        if isinstance(sampler, CoverageFirstTraceBalancedSampler) else 0
    )
    joint_lrs: Dict[str, float] = {}
    if joint_gss_v2:
        joint_lrs = _update_joint_v2_learning_rates(
            optimizer, state.step, config, sampler_steps_per_epoch,
        )
    os.makedirs(out_dir, exist_ok=True)
    amp = _amp_dtype(str(config.train.get("amp_dtype", "none")), torch_device)
    scaler = torch.amp.GradScaler(
        "cuda", enabled=torch_device.type == "cuda" and amp == torch.float16,
    )
    profile_attention = bool(config.train.get("profile_attention", False))
    profile_attention_done = False
    epochs = int(config.train.get("epochs", 100))
    log_every = int(config.train.get("log_every", 20))
    eval_every = int(config.train.get("eval_every", 1000))
    save_every = int(config.train.get("save_every", 500))
    milestone_steps = {
        int(value) for value in config.train.get("milestone_steps", [])
        if int(value) > 0
    }
    if joint_gss_v2:
        milestone_steps.add(
            sampler_steps_per_epoch
            + int(config.train.get("joint_v2_partial_steps", 8000))
        )
    gradient_clip = float(config.train.get("gradient_clip", 5.0))
    sampling_name = (
        COVERAGE_FIRST_TRACE_BALANCED_SAMPLER
        if isinstance(sampler, CoverageFirstTraceBalancedSampler)
        else type(sampler).__name__ if sampler is not None else "shuffle"
    )
    if rank == 0:
        parameter_count = sum(
            parameter.numel() for parameter in _raw_model(model).parameters()
        )
        trainable_count = sum(
            parameter.numel() for parameter in _raw_model(model).parameters()
            if parameter.requires_grad
        )
        print(
            f"[v29 train] train_sequences={len(train_data)} "
            f"val_sequences={len(validation_indices)}"
            f"/{len(validation_data) if validation_data else 0} "
            f"ddp={distributed} rank={rank}/{world} device={torch_device} "
            f"params={parameter_count:,} trainable={trainable_count:,} "
            f"frozen_memory_probe={frozen_memory_probe} "
            f"frozen_gss_probe={frozen_gss_probe} "
            f"frozen_gss_gate_probe={frozen_gss_gate_probe} "
            f"joint_gss_v2={joint_gss_v2} "
            f"start={state.step} target={max_steps} "
            f"sdpa={config.model.get('sdpa_backend', 'auto')} "
            f"amp={config.train.get('amp_dtype', 'none')} "
            f"profile_attention={profile_attention} "
            f"sampling={sampling_name} "
            f"steps_per_epoch={sampler_steps_per_epoch} "
            f"start_epoch={start_epoch} offset={start_epoch_offset}",
            flush=True,
        )
        if frozen_probe:
            print(
                "[frozen-probe params] " + ",".join(trainable_names),
                flush=True,
            )
        if joint_gss_v2:
            print(
                "[joint-gss-v2 groups] "
                + " ".join(
                    f"{group['group_name']}="
                    f"{sum(parameter.numel() for parameter in group['params']):,}"
                    f"@{float(group['base_lr']):.3e}/step{int(group['unfreeze_step'])}"
                    for group in optimizer.param_groups
                ),
                flush=True,
            )
            print(
                "[joint-gss-v2 objective] "
                "teacher=none parameter_anchor=none "
                f"initial_lrs={joint_lrs}",
                flush=True,
            )
    run_start_step = int(state.step)
    started = time.time()
    if (
        validation_loader is not None
        and bool(config.train.get("evaluate_at_start", False))
        and state.step == 0
    ):
        if distributed:
            dist.barrier()
        metrics = evaluate(model, validation_loader, torch_device, config)
        if rank == 0:
            row = {
                "epoch": -1,
                "step": 0,
                "elapsed_s": time.time() - started,
                **metrics,
            }
            history.append(row)
            state.best_validation = metrics["val_total"]
            _save_checkpoint(
                os.path.join(out_dir, "best.pt"), model, optimizer,
                state, config, contract, history,
            )
            dump_json(os.path.join(out_dir, "metrics.json"), history)
            print(f"[v29 eval step=0] {metrics}", flush=True)
        if distributed:
            dist.barrier()
    for epoch in range(start_epoch, epochs):
        if isinstance(sampler, CoverageFirstTraceBalancedSampler):
            sampler.set_epoch(
                epoch,
                start_offset=(
                    start_epoch_offset if epoch == start_epoch else 0
                ),
            )
        elif isinstance(sampler, DistributedSampler):
            sampler.set_epoch(epoch)
        model.train()
        if joint_gss_v2:
            _set_joint_gss_v2_train_mode(
                _raw_model(model), state.step, config,
            )
        elif frozen_gss_gate_probe:
            _set_frozen_gss_gate_probe_train_mode(_raw_model(model))
        elif frozen_gss_probe:
            _set_frozen_gss_probe_train_mode(_raw_model(model))
        elif frozen_memory_probe:
            _set_frozen_memory_probe_train_mode(_raw_model(model))
        for batch in train_loader:
            if max_steps is not None and state.step >= int(max_steps):
                break
            state.step += 1
            joint_stage = ""
            if joint_gss_v2:
                joint_stage = _set_joint_gss_v2_train_mode(
                    _raw_model(model), state.step, config,
                )
                joint_lrs = _update_joint_v2_learning_rates(
                    optimizer, state.step, config, sampler_steps_per_epoch,
                )
            batch = _to_device(batch, torch_device)
            optimizer.zero_grad(set_to_none=True)
            should_profile = bool(
                profile_attention
                and not profile_attention_done
                and rank == 0
                and torch_device.type == "cuda"
            )
            profile_context = (
                torch.profiler.profile(
                    activities=[
                        torch.profiler.ProfilerActivity.CPU,
                        torch.profiler.ProfilerActivity.CUDA,
                    ],
                    record_shapes=True,
                    profile_memory=True,
                ) if should_profile else nullcontext()
            )
            with profile_context as profiler:
                with torch.autocast(
                    device_type=torch_device.type,
                    dtype=amp,
                    enabled=amp is not None,
                ):
                    predictions = model(batch)
                    losses = compute_v29_losses(
                        predictions, batch, **_loss_kwargs(config),
                    )
                    gate_regret = losses.total * 0.0
                    gate_gap = losses.total * 0.0
                    joint_aux: Dict[str, torch.Tensor] = {}
                    optimization_total = losses.total
                    if joint_gss_v2:
                        joint_aux = _joint_v2_auxiliary_losses(
                            predictions,
                            batch,
                            config,
                        )
                        optimization_total = (
                            losses.total
                            + float(config.train.get(
                                "joint_v2_gap_weight", 0.20,
                            )) * joint_aux["local_gap"]
                            + float(config.train.get(
                                "joint_v2_rank_weight", 0.05,
                            )) * joint_aux["counterfactual_rank"]
                        )
                    elif frozen_gss_gate_probe:
                        gate_regret, gate_gap = _gss_gate_auxiliary_losses(
                            predictions, batch, config,
                        )
                        optimization_total = (
                            losses.total
                            + float(config.train.get(
                                "gss_gate_regret_weight", 0.25,
                            )) * gate_regret
                            + float(config.train.get(
                                "gss_gate_gap_weight", 0.1,
                            )) * gate_gap
                        )
                scaler.scale(optimization_total).backward()
                scaler.unscale_(optimizer)
                if joint_gss_v2:
                    _clear_zero_lr_gradients(optimizer)
                torch.nn.utils.clip_grad_norm_(
                    trainable_parameters, gradient_clip,
                )
                scaler.step(optimizer)
                scaler.update()
            if should_profile:
                profile_attention_done = True
                print(
                    "[v29 attention-profile] "
                    f"sdpa_backend={config.model.get('sdpa_backend', 'auto')} "
                    f"amp_dtype={config.train.get('amp_dtype', 'none')}",
                    flush=True,
                )
                for event in _attention_profile_events(profiler):
                    print(f"[v29 attention-profile] {event}", flush=True)
            if losses.monotonic_violations:
                raise RuntimeError("v29 monotonic head invariant failed")
            if rank == 0 and state.step % log_every == 0:
                correction_summary = ""
                if (
                    "memory_correction_logit" in predictions
                    and "memory_correction_mask" in predictions
                ):
                    correction = predictions["memory_correction_logit"]
                    correction_mask = predictions["memory_correction_mask"].bool()
                    selected = correction[correction_mask]
                    if selected.numel():
                        correction_summary = (
                            f" corr_mean={float(selected.mean()):.5f}"
                            f" corr_abs={float(selected.abs().mean()):.5f}"
                            f" corr_pos={float((selected > 0).float().mean()):.3f}"
                            f" corr_neg={float((selected < 0).float().mean()):.3f}"
                            f" base_logit={float(predictions['base_gap_logit'][correction_mask].mean()):.5f}"
                        )
                if "gss_adapter_delta" in predictions:
                    delta = predictions["gss_adapter_delta"]
                    correction_summary += (
                        f" gss_delta_abs={float(delta.detach().abs().mean()):.6f}"
                        f" gss_delta_rms={float(delta.detach().float().square().mean().sqrt()):.6f}"
                    )
                if "gss_exposure_gate" in predictions:
                    gate = predictions["gss_exposure_gate"].detach()
                    gate_valid = batch["valid_uop_mask"].bool()
                    selected_gate = gate[gate_valid]
                    correction_summary += (
                        f" gate_mean={float(selected_gate.mean()):.4f}"
                        f" gate_min={float(selected_gate.min()):.4f}"
                        f" gate_max={float(selected_gate.max()):.4f}"
                        f" regret={float(gate_regret.detach()):.5f}"
                        f" gap_aux={float(gate_gap.detach()):.5f}"
                    )
                if "gss_router_weights" in predictions:
                    router = predictions["gss_router_weights"].detach()
                    router_valid = batch["valid_uop_mask"].bool()
                    selected_router = router[router_valid]
                    mean_router = selected_router.mean(dim=0)
                    correction_summary += (
                        f" router={float(mean_router[0]):.3f}/"
                        f"{float(mean_router[1]):.3f}/"
                        f"{float(mean_router[2]):.3f}"
                        f" stage={joint_stage}"
                        f" gap_aux={float(joint_aux['local_gap'].detach()):.5f}"
                        f" rank={float(joint_aux['counterfactual_rank'].detach()):.5f}"
                        f" decisive={float(joint_aux['decisive_fraction'].detach()):.3f}"
                        f" entropy={float(joint_aux['router_entropy'].detach()):.3f}"
                        f" lrs=" + "/".join(
                            f"{name}:{value:.1e}"
                            for name, value in joint_lrs.items()
                        )
                    )
                print(
                    f"[v29 ep={epoch} step={state.step}] "
                    f"total={float(losses.total.detach()):.5f} "
                    f"opt={float(optimization_total.detach()):.5f} "
                    f"time={float(losses.commit_time.detach()):.5f} "
                    f"prefix={float(losses.prefix_bce.detach()):.5f} "
                    f"count={float(losses.progress_count.detach()):.5f} "
                    f"cum={float(losses.cumulative.detach()):.5f} "
                    f"branch={float(losses.branch_token.detach()):.5f}/"
                    f"{float(losses.branch_count.detach()):.5f} "
                    f"progress_mae={float(losses.progress_mae.detach()):.3f}"
                    f"{correction_summary}",
                    flush=True,
                )
            if validation_loader is not None and eval_every > 0 and state.step % eval_every == 0:
                if distributed:
                    dist.barrier()
                metrics = evaluate(model, validation_loader, torch_device, config)
                if rank == 0:
                    row = {
                        "epoch": epoch,
                        "step": state.step,
                        "elapsed_s": time.time() - started,
                        **metrics,
                    }
                    history.append(row)
                    save_best = metrics["val_total"] < state.best_validation
                    save_post_coverage = (
                        isinstance(
                            sampler, CoverageFirstTraceBalancedSampler,
                        )
                        and state.step >= sampler_steps_per_epoch
                        and metrics["val_total"]
                        < state.best_post_coverage_validation
                    )
                    if save_best:
                        state.best_validation = metrics["val_total"]
                    if save_post_coverage:
                        state.best_post_coverage_validation = metrics["val_total"]
                    if save_best:
                        _save_checkpoint(
                            os.path.join(out_dir, "best.pt"), model, optimizer,
                            state, config, contract, history,
                        )
                    if save_post_coverage:
                        _save_checkpoint(
                            os.path.join(
                                out_dir, "best_post_coverage.pt",
                            ),
                            model, optimizer, state, config, contract, history,
                        )
                        print(
                            "[v29 checkpoint] "
                            f"best_post_coverage step={state.step} "
                            f"val_total={metrics['val_total']:.8f} "
                            f"coverage_step={sampler_steps_per_epoch}",
                            flush=True,
                        )
                    dump_json(os.path.join(out_dir, "metrics.json"), history)
                    print(f"[v29 eval step={state.step}] {metrics}", flush=True)
                if distributed:
                    dist.barrier()
                model.train()
                if joint_gss_v2:
                    _set_joint_gss_v2_train_mode(
                        _raw_model(model), state.step, config,
                    )
                elif frozen_gss_gate_probe:
                    _set_frozen_gss_gate_probe_train_mode(_raw_model(model))
                elif frozen_gss_probe:
                    _set_frozen_gss_probe_train_mode(_raw_model(model))
                elif frozen_memory_probe:
                    _set_frozen_memory_probe_train_mode(_raw_model(model))
            # Save the resumable state after validation so last.pt contains
            # any best/best-post-coverage updates made at this same step.
            if rank == 0 and save_every > 0 and state.step % save_every == 0:
                _save_checkpoint(
                    os.path.join(out_dir, "last.pt"), model, optimizer, state,
                    config, contract, history,
                )
            if rank == 0 and state.step in milestone_steps:
                milestone_path = os.path.join(
                    out_dir, f"step_{state.step}.pt",
                )
                _save_checkpoint(
                    milestone_path, model, optimizer, state,
                    config, contract, history,
                )
                print(
                    f"[v29 checkpoint] milestone step={state.step} "
                    f"path={milestone_path}",
                    flush=True,
                )
        if max_steps is not None and state.step >= int(max_steps):
            break
    if rank == 0:
        _save_checkpoint(
            os.path.join(out_dir, "last.pt"), model, optimizer, state,
            config, contract, history,
        )
        if validation_loader is None:
            _save_checkpoint(
                os.path.join(out_dir, "best.pt"), model, optimizer, state,
                config, contract, history,
            )
    if distributed:
        dist.barrier()
    elapsed_seconds = time.time() - started
    completed_steps = max(0, int(state.step) - run_start_step)
    return {
        "steps": state.step,
        "elapsed_s": elapsed_seconds,
        "steps_per_s": completed_steps / max(1.0e-9, elapsed_seconds),
        "best_validation": state.best_validation,
        "best_post_coverage_validation": (
            state.best_post_coverage_validation
        ),
        "validation_sequences": len(validation_indices),
        "validation_sequences_total": len(validation_data) if validation_data else 0,
        "contract": contract,
        "frozen_memory_probe": frozen_memory_probe,
        "frozen_gss_probe": frozen_gss_probe,
        "frozen_gss_gate_probe": frozen_gss_gate_probe,
        "joint_gss_v2": joint_gss_v2,
        "trainable_parameters": sum(
            parameter.numel() for parameter in _raw_model(model).parameters()
            if parameter.requires_grad
        ),
    }
