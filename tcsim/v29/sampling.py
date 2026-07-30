"""Deterministic coverage-first sampling for v29 DDP training."""
from __future__ import annotations

import math
from collections import Counter
from typing import Iterator, Sequence

import torch
from torch.utils.data import Sampler


COVERAGE_FIRST_TRACE_BALANCED_SAMPLER = (
    "v29-coverage-first-then-trace-balanced-v1"
)


class CoverageFirstTraceBalancedSampler(Sampler[int]):
    """Cover every sequence once globally, then use trace-balanced sampling.

    Epoch zero is one shared deterministic permutation, sharded across ranks.
    At most ``world_size - 1`` leading indices are repeated only to make all
    ranks execute the same number of DDP steps.  Epochs one and later retain
    the established inverse-trace-size weighted replacement policy.

    ``start_offset`` makes checkpoint resume exact: the sampler can restart at
    the next local item implied by the checkpoint's global optimizer step.
    """

    def __init__(
        self,
        trace_ids: Sequence[str],
        *,
        num_replicas: int = 1,
        rank: int = 0,
        seed: int = 0,
        coverage_epochs: int = 1,
    ) -> None:
        if not trace_ids:
            raise ValueError("coverage-first sampler requires at least one item")
        self.trace_ids = tuple(str(value) for value in trace_ids)
        self.dataset_size = len(self.trace_ids)
        self.num_replicas = max(1, int(num_replicas))
        self.rank = int(rank)
        if not 0 <= self.rank < self.num_replicas:
            raise ValueError("coverage-first sampler rank outside world size")
        self.seed = int(seed)
        self.coverage_epochs = max(1, int(coverage_epochs))
        self.num_samples = int(math.ceil(
            self.dataset_size / self.num_replicas
        ))
        self.total_size = self.num_samples * self.num_replicas
        counts = Counter(self.trace_ids)
        self.weights = torch.tensor(
            [1.0 / max(1, counts[trace_id]) for trace_id in self.trace_ids],
            dtype=torch.double,
        )
        self.epoch = 0
        self.start_offset = 0

    @property
    def padding_items(self) -> int:
        return self.total_size - self.dataset_size

    def set_epoch(self, epoch: int, *, start_offset: int = 0) -> None:
        epoch = int(epoch)
        start_offset = int(start_offset)
        if epoch < 0:
            raise ValueError("coverage-first sampler epoch must be non-negative")
        if not 0 <= start_offset <= self.num_samples:
            raise ValueError("coverage-first sampler offset outside epoch")
        self.epoch = epoch
        self.start_offset = start_offset

    def _coverage_indices(self) -> torch.Tensor:
        generator = torch.Generator().manual_seed(self.seed)
        indices = torch.randperm(
            self.dataset_size, generator=generator, dtype=torch.int64,
        )
        padding = self.padding_items
        if padding:
            repeats = int(math.ceil(padding / self.dataset_size))
            indices = torch.cat(
                (indices, indices.repeat(repeats)[:padding]), dim=0,
            )
        if int(indices.numel()) != self.total_size:
            raise RuntimeError("invalid coverage-first global permutation")
        return indices[self.rank:self.total_size:self.num_replicas]

    def _balanced_indices(self) -> torch.Tensor:
        # Epoch- and rank-addressed seeds make a resumed iterator identical to
        # the corresponding suffix without serializing a mutable RNG state.
        generator = torch.Generator().manual_seed(
            self.seed + 1_000_003 * self.epoch + 97 * self.rank
        )
        return torch.multinomial(
            self.weights,
            self.num_samples,
            replacement=True,
            generator=generator,
        )

    def epoch_indices(self) -> torch.Tensor:
        indices = (
            self._coverage_indices()
            if self.epoch < self.coverage_epochs else self._balanced_indices()
        )
        if int(indices.numel()) != self.num_samples:
            raise RuntimeError("invalid coverage-first local sampler length")
        return indices

    def __iter__(self) -> Iterator[int]:
        indices = self.epoch_indices()[self.start_offset:]
        return iter(int(value) for value in indices.tolist())

    def __len__(self) -> int:
        return self.num_samples - self.start_offset
