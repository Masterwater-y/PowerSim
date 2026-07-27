"""Exact rank-local RNG and weighted-sampler resume support for TCSim v29.

The upstream v29 checkpoint contains model/optimizer/global-step state, but its
weighted sampler is recreated at cursor zero and rank-local RNG states are not
stored.  This module supplies the missing state without changing the upstream
loss, optimizer, model, or data contract.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import random
from typing import Any, Dict, Iterator, Mapping, Sequence

import numpy as np
import torch
from torch.utils.data import WeightedRandomSampler


RANK_RESUME_SCHEMA = "tcsim-v29-rank-resume-state-1"
SUPPORTED_RNG_POLICIES = frozenset({"exact", "aligned-reseed"})


def _torch_load(path: str | Path, map_location: Any = "cpu") -> Any:
    try:
        return torch.load(
            str(path), map_location=map_location, weights_only=False,
            mmap=True,
        )
    except (TypeError, RuntimeError):
        # mmap is unavailable for old torch/legacy serialization.
        try:
            return torch.load(
                str(path), map_location=map_location, weights_only=False,
            )
        except TypeError:
            return torch.load(str(path), map_location=map_location)


def checkpoint_step(path: str | Path | None) -> int:
    if path is None:
        return 0
    checkpoint = Path(path)
    if not checkpoint.is_file():
        raise FileNotFoundError(checkpoint)
    payload = _torch_load(checkpoint, "cpu")
    if not isinstance(payload, Mapping):
        raise RuntimeError(f"invalid resume checkpoint: {checkpoint}")
    step = int(payload.get("step", -1))
    if step < 0:
        raise RuntimeError(f"resume checkpoint lacks a valid step: {checkpoint}")
    del payload
    return step


def _contract_fingerprint(contract: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(contract), sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ExactResumeWeightedRandomSampler(WeightedRandomSampler):
    """Reconstruct the original rank-local weighted stream at a step cursor.

    PyTorch's sampler draws the complete epoch with one ``multinomial`` call
    when its iterator is created.  Replaying completed epoch draws and slicing
    the current draw therefore reconstructs the uninterrupted sequence exactly
    without loading or decoding skipped dataset examples.
    """

    def __init__(
        self,
        weights: Sequence[float],
        num_samples: int,
        replacement: bool = True,
        generator: torch.Generator | None = None,
        *,
        resume_step: int = 0,
        batch_size: int = 1,
        drop_last: bool = False,
    ) -> None:
        super().__init__(
            weights,
            num_samples,
            replacement=replacement,
            generator=generator,
        )
        self.resume_step = int(resume_step)
        self.batch_size = int(batch_size)
        self.drop_last = bool(drop_last)
        if self.resume_step < 0 or self.batch_size <= 0:
            raise ValueError("invalid exact-resume sampler cursor")
        if self.generator is None:
            raise ValueError("exact-resume weighted sampler requires a generator")
        self.steps_per_epoch = (
            self.num_samples // self.batch_size
            if self.drop_last
            else (self.num_samples + self.batch_size - 1) // self.batch_size
        )
        if self.steps_per_epoch <= 0:
            raise ValueError("weighted sampler has no complete training steps")
        self.completed_epochs = self.resume_step // self.steps_per_epoch
        self.step_in_epoch = self.resume_step % self.steps_per_epoch
        self.sample_offset = self.step_in_epoch * self.batch_size
        if self.sample_offset > self.num_samples:
            raise ValueError("exact-resume sampler offset exceeds epoch")
        self._first_iteration = True

    @property
    def resume_contract(self) -> Dict[str, Any]:
        return {
            "sampler": "weighted_random_replacement",
            "num_samples_per_rank": int(self.num_samples),
            "batch_size_per_rank": self.batch_size,
            "drop_last": self.drop_last,
            "steps_per_epoch": self.steps_per_epoch,
            "resume_step": self.resume_step,
            "completed_sampler_epochs": self.completed_epochs,
            "step_in_sampler_epoch": self.step_in_epoch,
            "sample_offset_in_rank_epoch": self.sample_offset,
        }

    def _draw_epoch(self) -> list[int]:
        return torch.multinomial(
            self.weights,
            self.num_samples,
            self.replacement,
            generator=self.generator,
        ).tolist()

    def __iter__(self) -> Iterator[int]:
        if self._first_iteration:
            self._first_iteration = False
            for _ in range(self.completed_epochs):
                self._draw_epoch()
            indices = self._draw_epoch()
            yield from indices[self.sample_offset:]
            return
        yield from self._draw_epoch()

    def __len__(self) -> int:
        if self._first_iteration:
            return self.num_samples - self.sample_offset
        return self.num_samples


class RankResumeStateManager:
    """Save and restore rank-local RNG state beside ordinary checkpoints."""

    def __init__(
        self,
        *,
        output_dir: str | Path,
        resume_checkpoint: str | Path | None,
        resume_step: int,
        rank: int,
        world_size: int,
        train_seed: int,
        rng_policy: str,
        save_every: int,
        eval_every: int,
        max_steps: int | None,
    ) -> None:
        self.output_dir = Path(output_dir).resolve()
        self.resume_checkpoint = (
            None if resume_checkpoint is None else Path(resume_checkpoint).resolve()
        )
        self.resume_step = int(resume_step)
        self.current_step = int(resume_step)
        self.rank = int(rank)
        self.world_size = int(world_size)
        self.train_seed = int(train_seed)
        self.rng_policy = str(rng_policy)
        self.save_every = int(save_every)
        self.eval_every = int(eval_every)
        self.max_steps = None if max_steps is None else int(max_steps)
        if self.rng_policy not in SUPPORTED_RNG_POLICIES:
            raise ValueError(f"unsupported resume RNG policy {rng_policy!r}")
        self.sampler_contract: Dict[str, Any] | None = None
        self.resume_contract: Dict[str, Any] | None = None
        self.contract_fingerprint: str | None = None
        self.restore_report: Dict[str, Any] = {
            "mode": "fresh",
            "resume_step": self.resume_step,
        }

    def register_sampler(self, sampler: ExactResumeWeightedRandomSampler) -> None:
        contract = sampler.resume_contract
        if int(contract["resume_step"]) != self.resume_step:
            raise RuntimeError("sampler and checkpoint resume steps differ")
        self.sampler_contract = contract

    def register_contract(self, contract: Mapping[str, Any]) -> None:
        fingerprint = _contract_fingerprint(contract)
        if (
            self.contract_fingerprint is not None
            and self.contract_fingerprint != fingerprint
        ):
            raise RuntimeError(
                "training and validation exact-resume contracts differ"
            )
        self.resume_contract = dict(contract)
        self.contract_fingerprint = fingerprint

    def _state_path(
        self, *, step: int, checkpoint_parent: Path | None = None,
    ) -> Path:
        parent = (
            checkpoint_parent
            if checkpoint_parent is not None
            else self.output_dir
        )
        return (
            parent / "resume_state" / f"step_{int(step):08d}"
            / f"rank_{self.rank:05d}.pt"
        )

    def _aligned_seed(self) -> int:
        # Variant-independent by design: E2/real/shuffled receive the same
        # post-legacy rank-local dropout stream at the same global step.
        return int(
            (self.train_seed * 1_000_003 + self.resume_step * 97 + self.rank)
            % (2**31 - 1)
        )

    def restore(self, device: torch.device) -> Dict[str, Any]:
        if self.resume_checkpoint is None:
            self.restore_report = {
                "mode": "fresh",
                "resume_step": 0,
                "rank": self.rank,
                "world_size": self.world_size,
            }
            return self.restore_report
        if self.sampler_contract is None or self.contract_fingerprint is None:
            raise RuntimeError("resume manager was not fully initialized")
        path = self._state_path(
            step=self.resume_step,
            checkpoint_parent=self.resume_checkpoint.parent,
        )
        if path.is_file():
            payload = _torch_load(path, "cpu")
            expected = {
                "schema_version": RANK_RESUME_SCHEMA,
                "step": self.resume_step,
                "rank": self.rank,
                "world_size": self.world_size,
                "train_seed": self.train_seed,
                "contract_fingerprint": self.contract_fingerprint,
            }
            for key, value in expected.items():
                if payload.get(key) != value:
                    raise RuntimeError(
                        f"rank resume state mismatch for {key}: "
                        f"{payload.get(key)!r} != {value!r}"
                    )
            stored_sampler = dict(payload.get("sampler_contract", {}))
            current_sampler = dict(self.sampler_contract)
            # The state was captured during the original fresh run.  Its
            # cursor is zero; structural fields must match the resumed run.
            structural = (
                "sampler", "num_samples_per_rank", "batch_size_per_rank",
                "drop_last", "steps_per_epoch",
            )
            for key in structural:
                if stored_sampler.get(key) != current_sampler.get(key):
                    raise RuntimeError(
                        f"rank resume sampler mismatch for {key}"
                    )
            random.setstate(payload["python_rng_state"])
            np.random.set_state(payload["numpy_rng_state"])
            torch.set_rng_state(payload["torch_cpu_rng_state"])
            if device.type == "cuda":
                torch.cuda.set_rng_state(payload["torch_cuda_rng_state"], device)
            self.restore_report = {
                "mode": "exact",
                "resume_step": self.resume_step,
                "rank": self.rank,
                "world_size": self.world_size,
                "state_path": str(path),
                "sampler": current_sampler,
            }
            return self.restore_report

        if self.rng_policy == "exact":
            raise RuntimeError(
                "exact RNG resume state is missing for this legacy checkpoint: "
                f"{path}. Exact recovery is information-theoretically impossible; "
                "use --resume-rng-policy aligned-reseed once to create a new "
                "exact-resumable checkpoint lineage."
            )
        seed = self._aligned_seed()
        random.seed(seed)
        np.random.seed(seed % (2**32))
        torch.manual_seed(seed)
        if device.type == "cuda":
            torch.cuda.manual_seed(seed)
        self.restore_report = {
            "mode": "aligned-reseed",
            "resume_step": self.resume_step,
            "rank": self.rank,
            "world_size": self.world_size,
            "seed": seed,
            "missing_legacy_state": str(path),
            "sampler": dict(self.sampler_contract),
        }
        return self.restore_report

    def _should_capture(self, step: int) -> bool:
        return bool(
            (self.save_every > 0 and step % self.save_every == 0)
            or (self.eval_every > 0 and step % self.eval_every == 0)
            or (self.max_steps is not None and step == self.max_steps)
        )

    def after_optimizer_step(self, device: torch.device) -> None:
        self.current_step += 1
        if self._should_capture(self.current_step):
            self.capture(device, phase="post_optimizer")

    def capture(self, device: torch.device, *, phase: str) -> Path:
        if self.sampler_contract is None or self.contract_fingerprint is None:
            raise RuntimeError("cannot capture incomplete rank resume state")
        path = self._state_path(step=self.current_step)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": RANK_RESUME_SCHEMA,
            "step": self.current_step,
            "rank": self.rank,
            "world_size": self.world_size,
            "train_seed": self.train_seed,
            "phase": str(phase),
            "contract_fingerprint": self.contract_fingerprint,
            "resume_contract": self.resume_contract,
            "sampler_contract": dict(self.sampler_contract),
            "restore_report": dict(self.restore_report),
            "python_rng_state": random.getstate(),
            "numpy_rng_state": np.random.get_state(),
            "torch_cpu_rng_state": torch.get_rng_state(),
            "torch_cuda_rng_state": (
                torch.cuda.get_rng_state(device).cpu()
                if device.type == "cuda" else None
            ),
        }
        temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
        try:
            torch.save(payload, temporary)
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
        return path
