"""Training adapter that keeps the current TCSim v29 loop authoritative."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

import numpy as np
import torch
from tcsim.v29 import train as base_train
from tcsim.utils.config import TCSimConfig

from model.tcsim_v29_semantic import (
    LEGACY_FUSION_ARCHITECTURE,
    SEMANTIC_EXPERIMENT_SCHEMA,
    build_semantic_model,
)
from train.tcsim_v29_semantic_dataset import (
    TCSimV29SemanticDataset,
    collate_tcsim_v29_semantic,
)
from train.tcsim_v29_exact_resume import (
    ExactResumeWeightedRandomSampler,
    RANK_RESUME_SCHEMA,
    RankResumeStateManager,
    checkpoint_step,
)


SEMANTIC_CHECKPOINT_SCHEMA = "tcsim-v29-b2e2-checkpoint-1"
LEGACY_FULL_REAL_INTERVENTION = {
    "mode": "full_real",
    "seed": None,
    "mapping_scope": None,
    "paired_fields": [],
    "marginal_preserved": False,
}


def _file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tcsim_source_contract() -> Dict[str, Any]:
    root = Path(base_train.__file__).resolve().parents[2]
    relative_files = (
        "tcsim/v29/model.py",
        "tcsim/v29/dataset.py",
        "tcsim/v29/train.py",
        "tcsim/v29/losses.py",
        "configs/v29_100m.yaml",
    )
    return {
        "root": str(root),
        "source_sha256": {
            name: _file_sha256(root / name) for name in relative_files
        },
    }


def _adapter_source_contract() -> Dict[str, str]:
    repo = Path(__file__).resolve().parents[1]
    relative_files = (
        "model/tcsim_v29_semantic.py",
        "train/tcsim_v29_semantic_dataset.py",
        "train/tcsim_v29_semantic_train.py",
        "train/tcsim_v29_exact_resume.py",
    )
    return {
        name: _file_sha256(repo / name) for name in relative_files
    }


def _checkpoint_load_contract(
    contract: Mapping[str, Any],
    *,
    variant: str,
    semantic_permutation_seed: int | None,
    resume_rng_policy: str,
) -> tuple[Dict[str, Any], str | None]:
    """Return the exact contract expected by a narrowly supported legacy run.

    B2-real step-10000 was trained before ``semantic_intervention=full_real``
    was added as explicit provenance for the shuffled control.  The missing
    field changes no tensor or lookup.  Strict equality is retained for every
    other field and for all shuffled checkpoints.
    """

    current = dict(contract)
    if not (
        str(variant).lower() == "b2-frozen"
        and semantic_permutation_seed is None
        and str(resume_rng_policy) == "aligned-reseed"
    ):
        return current, None
    semantic = dict(current.get("semantic", {}))
    intervention = semantic.get("semantic_intervention")
    if intervention != LEGACY_FULL_REAL_INTERVENTION:
        raise RuntimeError(
            "refusing legacy B2-real compatibility: current full-real "
            "semantic intervention contract is unexpected"
        )
    semantic.pop("semantic_intervention")
    current["semantic"] = semantic
    return current, "legacy-b2-full-real-missing-intervention-v1"


def train_one_semantic_run(
    train_sources: Sequence[Any],
    validation_sources: Sequence[Any],
    out_dir: str,
    config: TCSimConfig,
    *,
    variant: str,
    semantic_dim: int,
    semantic_permutation_seed: int | None,
    semantic_cache_root: str | Path | None,
    static_manifest: str | Path | None,
    semantic_sidecar_root: str | Path | None = None,
    device: str = "auto",
    max_steps: Optional[int] = None,
    resume: Optional[str] = None,
    resume_rng_policy: str = "exact",
) -> Dict[str, Any]:
    """Run the upstream loop with narrowly bound dataset/model factories.

    The optimizer, loss, validation selection, DDP behavior and checkpoint
    cadence remain the current TCSim v29 implementation.  Only the dataset
    adapter, model factory and checkpoint contract are replaced.
    """

    normalized_variant = str(variant).lower()
    semantic_dim = int(semantic_dim)
    static_manifest_path = (
        None if static_manifest is None else str(Path(static_manifest).resolve())
    )
    cache_root_path = (
        None
        if semantic_cache_root is None
        else str(Path(semantic_cache_root).resolve())
    )
    sidecar_root_path = (
        None
        if semantic_sidecar_root is None
        else str(Path(semantic_sidecar_root).resolve())
    )
    config.train = {
        **config.train,
        "resume_rng_policy": str(resume_rng_policy),
        "rank_resume_schema": RANK_RESUME_SCHEMA,
    }
    resume_step = checkpoint_step(resume)
    if (
        resume is not None
        and max_steps is not None
        and int(max_steps) <= resume_step
    ):
        raise ValueError(
            f"resume target must exceed checkpoint step: "
            f"{max_steps} <= {resume_step}"
        )
    rank = int(os.environ.get("RANK", "0"))
    world_size = int(os.environ.get("WORLD_SIZE", "1"))
    train_seed = int(config.train.get("seed", 1234))
    batch_size = int(config.train.get("batch_samples", 1))
    trace_balanced = bool(
        config.train.get("trace_balanced_sampling", True)
    )
    if not trace_balanced:
        raise RuntimeError(
            "exact resume currently requires trace_balanced_sampling=true; "
            "DistributedSampler cursor recovery is not implemented"
        )
    manager = RankResumeStateManager(
        output_dir=out_dir,
        resume_checkpoint=resume,
        resume_step=resume_step,
        rank=rank,
        world_size=world_size,
        train_seed=train_seed,
        rng_policy=resume_rng_policy,
        save_every=int(config.train.get("save_every", 500)),
        eval_every=int(config.train.get("eval_every", 1000)),
        max_steps=max_steps,
    )
    # The upstream loop seeds Python and Torch itself, but not NumPy.  Keep all
    # three rank-local streams deterministic for a fresh exact-resume lineage.
    np.random.seed((train_seed + rank) % (2**32))

    class BoundSemanticDataset(TCSimV29SemanticDataset):
        def __init__(
            self,
            sources: Sequence[Any],
            *,
            sequence_length: int = 4,
            sequence_stride: int | None = None,
        ) -> None:
            super().__init__(
                sources,
                variant=normalized_variant,
                semantic_cache_root=cache_root_path,
                static_manifest=static_manifest_path,
                semantic_dim=semantic_dim,
                semantic_permutation_seed=semantic_permutation_seed,
                semantic_sidecar_root=sidecar_root_path,
                sequence_length=sequence_length,
                sequence_stride=sequence_stride,
            )

    def bound_build_model(
        model_config: Mapping[str, Any], horizons: Sequence[float],
    ):
        return build_semantic_model(
            model_config,
            horizons,
            variant=normalized_variant,
            semantic_dim=semantic_dim,
        )

    original_contract = base_train._dataset_contract

    def bound_dataset_contract(dataset: TCSimV29SemanticDataset) -> Dict[str, Any]:
        contract = dict(original_contract(dataset))
        contract.update({
            "semantic_experiment_schema": SEMANTIC_EXPERIMENT_SCHEMA,
            "semantic": dataset.semantic_contract,
            "semantic_fusion": {
                "architecture": LEGACY_FUSION_ARCHITECTURE,
                "location": "before_tcsim_v29_full_qkvr",
                "mapping": "macro_cache_to_each_uop_by_macro_pc",
                "transport": "per_sequence_unique_table_plus_uop_index",
                "projection": f"RMSNorm({semantic_dim})->Linear({semantic_dim},"
                              f"{int(config.model.get('d_static', 256))})",
                "gate": "sigmoid_linear_residual",
                "gate_bias": float(
                    config.model.get("semantic_gate_bias", -2.0)
                ),
            },
            "tcsim_source": _tcsim_source_contract(),
        })
        if static_manifest_path is not None:
            contract["static_manifest"] = {
                "path": static_manifest_path,
                "sha256": _file_sha256(static_manifest_path),
            }
        resume_train_config = {
            key: value for key, value in config.train.items()
            if key not in {"resume_rng_policy", "rank_resume_schema"}
        }
        manager.register_contract({
            "dataset_contract": contract,
            "execution_config": {
                "chunk": dict(config.chunk),
                "scheduler": dict(config.scheduler),
                "uarch": dict(config.uarch),
                "model": dict(config.model),
                "train": resume_train_config,
            },
            "adapter_source_sha256": _adapter_source_contract(),
        })
        return contract

    class BoundExactWeightedRandomSampler(ExactResumeWeightedRandomSampler):
        def __init__(
            self,
            weights,
            num_samples: int,
            replacement: bool = True,
            generator: torch.Generator | None = None,
        ) -> None:
            super().__init__(
                weights,
                num_samples,
                replacement=replacement,
                generator=generator,
                resume_step=resume_step,
                batch_size=batch_size,
                drop_last=world_size > 1,
            )
            manager.register_sampler(self)

    original_setup_distributed = base_train._setup_distributed

    def bound_setup_distributed(requested_device: str):
        result = original_setup_distributed(requested_device)
        _, actual_rank, _, actual_world, torch_device = result
        if actual_rank != rank or actual_world != world_size:
            raise RuntimeError("distributed environment changed during setup")
        manager.torch_device = torch_device
        return result

    original_data_loader = base_train.DataLoader
    loader_index = 0

    class BoundDataLoader(original_data_loader):
        """Keep iterator bookkeeping from consuming the model CPU RNG."""

        def __init__(self, *args, **kwargs) -> None:
            nonlocal loader_index
            if kwargs.get("generator") is None:
                loader_generator = torch.Generator()
                loader_generator.manual_seed(
                    train_seed + rank + 10_000_019 * (loader_index + 1)
                )
                kwargs["generator"] = loader_generator
            loader_index += 1
            super().__init__(*args, **kwargs)

    original_load_checkpoint = base_train._load_checkpoint

    def bound_load_checkpoint(
        path: str,
        model: torch.nn.Module,
        optimizer: Optional[torch.optim.Optimizer],
        torch_device: torch.device,
        contract: Mapping[str, Any],
    ):
        load_contract, compatibility = _checkpoint_load_contract(
            contract,
            variant=normalized_variant,
            semantic_permutation_seed=semantic_permutation_seed,
            resume_rng_policy=resume_rng_policy,
        )
        state, history = original_load_checkpoint(
            path, model, optimizer, torch_device, load_contract,
        )
        if int(state.step) != resume_step:
            raise RuntimeError(
                f"checkpoint step changed while loading: "
                f"{state.step} != {resume_step}"
            )
        if compatibility is not None and rank == 0:
            print(
                f"[v29 resume-contract] compatibility={compatibility}",
                flush=True,
            )
        report = manager.restore(torch_device)
        if rank == 0:
            print(f"[v29 exact-resume] {report}", flush=True)
        return state, history

    original_grad_scaler = base_train.torch.amp.GradScaler

    class BoundGradScaler(original_grad_scaler):
        def update(self, *args, **kwargs):
            result = super().update(*args, **kwargs)
            torch_device = getattr(manager, "torch_device", None)
            if torch_device is None:
                raise RuntimeError("exact-resume device was not initialized")
            # update() is invoked exactly once per consumed batch, including
            # fp16 overflow steps where optimizer.step() is intentionally
            # skipped.  The sampler cursor follows batches/global steps.
            manager.after_optimizer_step(torch_device)
            return result

    original_evaluate = base_train.evaluate

    def bound_evaluate(
        model: torch.nn.Module,
        loader,
        torch_device: torch.device,
        eval_config: TCSimConfig,
    ) -> Dict[str, float]:
        metrics = original_evaluate(
            model, loader, torch_device, eval_config,
        )
        # A best checkpoint is written after validation.  Overwrite the
        # post-optimizer sidecar with the actual post-validation RNG state.
        manager.capture(torch_device, phase="post_validation")
        return metrics

    originals = {
        "V29GlobalTimeDataset": base_train.V29GlobalTimeDataset,
        "collate_v29_sequences": base_train.collate_v29_sequences,
        "build_model": base_train.build_model,
        "_dataset_contract": base_train._dataset_contract,
        "_setup_distributed": base_train._setup_distributed,
        "DataLoader": base_train.DataLoader,
        "WeightedRandomSampler": base_train.WeightedRandomSampler,
        "_load_checkpoint": base_train._load_checkpoint,
        "GradScaler": base_train.torch.amp.GradScaler,
        "evaluate": base_train.evaluate,
        "CHECKPOINT_SCHEMA_VERSION": base_train.CHECKPOINT_SCHEMA_VERSION,
    }
    base_train.V29GlobalTimeDataset = BoundSemanticDataset
    base_train.collate_v29_sequences = collate_tcsim_v29_semantic
    base_train.build_model = bound_build_model
    base_train._dataset_contract = bound_dataset_contract
    base_train._setup_distributed = bound_setup_distributed
    base_train.DataLoader = BoundDataLoader
    base_train.WeightedRandomSampler = BoundExactWeightedRandomSampler
    base_train._load_checkpoint = bound_load_checkpoint
    base_train.torch.amp.GradScaler = BoundGradScaler
    base_train.evaluate = bound_evaluate
    base_train.CHECKPOINT_SCHEMA_VERSION = SEMANTIC_CHECKPOINT_SCHEMA
    try:
        result = base_train.train_one_run(
            train_sources,
            validation_sources,
            out_dir,
            config,
            device=device,
            max_steps=max_steps,
            resume=resume,
        )
    finally:
        # GradScaler is a nested torch attribute, not a train-module symbol.
        base_train.torch.amp.GradScaler = originals.pop("GradScaler")
        for name, value in originals.items():
            setattr(base_train, name, value)
    result["resume"] = dict(manager.restore_report)
    result["resume"]["rank_resume_schema"] = RANK_RESUME_SCHEMA
    return result
