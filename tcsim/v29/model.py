"""Full-QKVR v29 model with monotonic per-UOP retirement times."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F

from ..model.tcsim_model import FunctionalInteractionBlock
from .contracts import (
    CHUNK_SUMMARY_NAMES,
    DYNAMIC_FIELD_SIZES,
    FIELD_GROUP_INDICES,
    FIELD_SIZES,
    RELATION_FEATURE_NAMES,
    STATE_FEATURE_NAMES,
    UARCH_FEATURE_NAMES,
    normalized_horizons,
)


class StaticTokenEncoderV29(nn.Module):
    """Encode only categorical fields with stable cross-trace semantics."""

    def __init__(self, d_field: int, d_static: int, max_K: int = 256) -> None:
        super().__init__()
        self.field_sizes = tuple(int(value) for value in FIELD_SIZES)
        self.max_K = int(max_K)
        self.field_embeddings = nn.ModuleList([
            nn.Embedding(size + 1, d_field, padding_idx=size)
            for size in self.field_sizes
        ])
        self.group_indices = {
            name: tuple(int(index) for index in indices)
            for name, indices in FIELD_GROUP_INDICES.items()
        }
        self.group_projections = nn.ModuleDict({
            name: nn.Sequential(
                nn.Linear(d_field * len(indices), d_static),
                nn.GELU(),
                nn.Linear(d_static, d_static),
            )
            for name, indices in self.group_indices.items()
        })
        self.position = nn.Embedding(self.max_K, d_static)
        self.output = nn.Sequential(
            nn.LayerNorm(d_static),
            nn.Linear(d_static, d_static),
            nn.GELU(),
            nn.Linear(d_static, d_static),
        )

    def forward(self, fields: torch.Tensor) -> torch.Tensor:
        if fields.ndim != 3:
            raise ValueError("v29 per_uop_fields must be [N,K,F]")
        _rows, K, count = fields.shape
        if count != len(self.field_embeddings):
            raise ValueError(
                f"v29 field count {count} != {len(self.field_embeddings)}"
            )
        if K > self.max_K:
            raise ValueError(f"v29 K={K} exceeds max_K={self.max_K}")
        embedded: List[torch.Tensor] = []
        for field_index, (embedding, size) in enumerate(zip(
            self.field_embeddings, self.field_sizes,
        )):
            embedded.append(embedding(fields[..., field_index].clamp(0, size)))
        groups = [
            self.group_projections[name](
                torch.cat([embedded[index] for index in indices], dim=-1)
            )
            for name, indices in self.group_indices.items()
        ]
        hidden = torch.stack(groups, dim=0).sum(dim=0)
        hidden = hidden + self.position.weight[:K].unsqueeze(0)
        return self.output(hidden)


class FunctionalInteractionV29(nn.Module):
    """Full local-QKV and cross-core-R attention preserving token states."""

    def __init__(
        self,
        *,
        d_static: int,
        d_dyn: int,
        d_dynamic_field: int,
        n_heads: int,
        n_layers: int,
        ffn_dim: Optional[int],
        dropout: float,
        cross_target_block: int,
        sdpa_backend: str,
    ) -> None:
        super().__init__()
        self.summary_dim = len(CHUNK_SUMMARY_NAMES)
        self.relation_dim = len(RELATION_FEATURE_NAMES)
        self.uarch_dim = len(UARCH_FEATURE_NAMES)
        self.state_dim = len(STATE_FEATURE_NAMES)
        self.dynamic_sizes = tuple(int(value) for value in DYNAMIC_FIELD_SIZES)
        self.token_projection = nn.Linear(d_static, d_dyn)
        self.dynamic_embeddings = nn.ModuleList([
            nn.Embedding(size + 1, d_dynamic_field, padding_idx=size)
            for size in self.dynamic_sizes
        ])
        self.dynamic_projection = nn.Sequential(
            nn.Linear(d_dynamic_field * len(self.dynamic_sizes), d_dyn),
            nn.GELU(),
            nn.Linear(d_dyn, d_dyn),
        )
        side_dim = self.summary_dim + self.relation_dim + self.uarch_dim + self.state_dim
        self.side_norm = nn.LayerNorm(side_dim)
        self.side_projection = nn.Sequential(
            nn.Linear(side_dim, d_dyn),
            nn.GELU(),
            nn.Linear(d_dyn, d_dyn),
        )
        gate_dim = self.relation_dim + self.state_dim
        self.cross_gate = nn.Sequential(
            nn.LayerNorm(gate_dim),
            nn.Linear(gate_dim, max(64, d_dyn // 4)),
            nn.GELU(),
            nn.Linear(max(64, d_dyn // 4), d_dyn),
        )
        self.layers = nn.ModuleList([
            FunctionalInteractionBlock(
                d_dyn=d_dyn,
                n_heads=n_heads,
                dropout=dropout,
                ffn_dim=ffn_dim,
                cross_target_block=cross_target_block,
                sdpa_backend=sdpa_backend,
            )
            for _ in range(max(1, int(n_layers)))
        ])
        self.final_norm = nn.LayerNorm(d_dyn)

    def forward(
        self,
        static_tokens: torch.Tensor,
        batch: Dict[str, torch.Tensor],
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        dimensions = (
            int(batch["chunk_summary"].shape[-1]),
            int(batch["relation_features"].shape[-1]),
            int(batch["uarch_features"].shape[-1]),
            int(batch["state_features"].shape[-1]),
        )
        expected = (
            self.summary_dim, self.relation_dim, self.uarch_dim, self.state_dim,
        )
        if dimensions != expected:
            raise ValueError(f"v29 side dimensions {dimensions} != {expected}")
        dynamic = batch["dynamic_uop_fields"]
        if dynamic.ndim != 3 or int(dynamic.shape[-1]) != len(self.dynamic_embeddings):
            raise ValueError("v29 dynamic_uop_fields dimension mismatch")
        dynamic_parts = [
            embedding(dynamic[..., index].clamp(0, size))
            for index, (embedding, size) in enumerate(zip(
                self.dynamic_embeddings, self.dynamic_sizes,
            ))
        ]
        side = torch.cat([
            batch["chunk_summary"],
            batch["relation_features"],
            batch["uarch_features"],
            batch["state_features"],
        ], dim=-1)
        hidden = self.token_projection(static_tokens)
        hidden = hidden + self.dynamic_projection(torch.cat(dynamic_parts, dim=-1))
        hidden = hidden + self.side_projection(self.side_norm(side)).unsqueeze(1)
        mask = batch["valid_uop_mask"].bool()
        hidden = hidden * mask.unsqueeze(-1).to(hidden.dtype)
        gate = torch.sigmoid(self.cross_gate(torch.cat([
            batch["relation_features"], batch["state_features"],
        ], dim=-1)))
        for layer in self.layers:
            hidden = layer(hidden, mask, batch["sample_ptr"], gate)
        hidden = self.final_norm(hidden)
        hidden = hidden * mask.unsqueeze(-1).to(hidden.dtype)
        denom = mask.sum(dim=1, keepdim=True).clamp(min=1).to(hidden.dtype)
        core = hidden.sum(dim=1) / denom
        return hidden, core


class TCSimV29Model(nn.Module):
    def __init__(
        self,
        *,
        horizons: Sequence[float],
        d_field: int = 32,
        d_dynamic_field: int = 16,
        d_static: int = 256,
        d_dyn: int = 384,
        n_heads: int = 8,
        n_layers: int = 4,
        ffn_dim: Optional[int] = None,
        dropout: float = 0.1,
        max_K: int = 256,
        cross_target_block: int = 0,
        sdpa_backend: str = "auto",
        commit_temperature: float = 4.0,
        gap_softplus_beta: float = 4.0,
    ) -> None:
        super().__init__()
        horizon_values = normalized_horizons(horizons)
        self.register_buffer(
            "horizons", torch.tensor(horizon_values, dtype=torch.float32),
            persistent=True,
        )
        self.commit_temperature = float(commit_temperature)
        self.gap_softplus_beta = float(gap_softplus_beta)
        if self.commit_temperature <= 0 or self.gap_softplus_beta <= 0:
            raise ValueError("v29 temperatures must be positive")
        self.static_encoder = StaticTokenEncoderV29(d_field, d_static, max_K=max_K)
        self.interaction = FunctionalInteractionV29(
            d_static=d_static,
            d_dyn=d_dyn,
            d_dynamic_field=d_dynamic_field,
            n_heads=n_heads,
            n_layers=n_layers,
            ffn_dim=ffn_dim,
            dropout=dropout,
            cross_target_block=cross_target_block,
            sdpa_backend=sdpa_backend,
        )
        # Timing and PMU have independent heads.  They share contextual tokens
        # but cannot trade one scalar output against the other.
        self.gap_head = nn.Sequential(
            nn.Linear(d_dyn, d_dyn),
            nn.GELU(),
            nn.Linear(d_dyn, 1),
        )
        self.branch_head = nn.Sequential(
            nn.Linear(d_dyn, max(64, d_dyn // 2)),
            nn.GELU(),
            nn.Linear(max(64, d_dyn // 2), 1),
        )

    def forward_from_static(
        self,
        batch: Dict[str, torch.Tensor],
        static_tokens: torch.Tensor,
    ) -> Dict[str, torch.Tensor]:
        token, core = self.interaction(static_tokens, batch)
        mask = batch["valid_uop_mask"].bool()
        # Attention/MLP runs under BF16 autocast, but a 256-term retirement
        # prefix needs FP32 accumulation.  BF16 cumsum loses multiple cycles
        # of resolution once tau reaches O(1K), directly corrupting the target
        # semantic rather than merely changing throughput.
        raw_gap = self.gap_head(token).squeeze(-1).float()
        gap = F.softplus(raw_gap, beta=self.gap_softplus_beta)
        gap = gap * mask.to(gap.dtype)
        commit_time = torch.cumsum(gap, dim=1)
        commit_logits = (
            self.horizons.to(commit_time.dtype)[None, None, :]
            - commit_time.unsqueeze(-1)
        ) / self.commit_temperature
        commit_probability = torch.sigmoid(commit_logits)
        commit_probability = commit_probability * mask.unsqueeze(-1).to(
            commit_probability.dtype
        )
        progress = commit_probability.sum(dim=1)
        branch_logit = self.branch_head(token).squeeze(-1).float()
        branch_probability = torch.sigmoid(branch_logit)
        branch_probability = branch_probability * mask.to(branch_probability.dtype)
        hard_prefix = (
            commit_time.unsqueeze(-1)
            <= self.horizons.to(commit_time.dtype)[None, None, :]
        ) & mask.unsqueeze(-1)
        return {
            "retirement_gap": gap,
            "commit_time": commit_time,
            "commit_logits": commit_logits,
            "commit_probability": commit_probability,
            "progress": progress,
            "hard_prefix": hard_prefix,
            "branch_miss_logit": branch_logit,
            "branch_miss_probability": branch_probability,
            "token_state": token,
            "core_state": core,
        }

    def forward(self, batch: Dict[str, torch.Tensor]) -> Dict[str, torch.Tensor]:
        static_tokens = self.static_encoder(batch["per_uop_fields"])
        return self.forward_from_static(batch, static_tokens)


def build_model(config: Mapping[str, Any], horizons: Sequence[float]) -> TCSimV29Model:
    return TCSimV29Model(
        horizons=horizons,
        d_field=int(config.get("d_field", 32)),
        d_dynamic_field=int(config.get("d_dynamic_field", 16)),
        d_static=int(config.get("d_static", 256)),
        d_dyn=int(config.get("d_dyn", 384)),
        n_heads=int(config.get("n_dyn_heads", 8)),
        n_layers=int(config.get("n_dyn_layers", 4)),
        ffn_dim=(
            int(config["ffn_dim"]) if config.get("ffn_dim") is not None else None
        ),
        dropout=float(config.get("dropout", 0.1)),
        max_K=int(config.get("max_K", 256)),
        cross_target_block=int(config.get("cross_target_block", 0)),
        sdpa_backend=str(config.get("sdpa_backend", "auto")),
        commit_temperature=float(config.get("commit_temperature", 4.0)),
        gap_softplus_beta=float(config.get("gap_softplus_beta", 4.0)),
    )
