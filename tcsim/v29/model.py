"""Full-QKVR v29 model with monotonic per-UOP retirement times."""
from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F

from ..model.tcsim_model import FunctionalInteractionBlock
from .contracts import (
    CHUNK_SUMMARY_NAMES,
    DYNAMIC_FIELD_SIZES,
    FIELD_INDEX,
    FIELD_GROUP_INDICES,
    FIELD_SIZES,
    RELATION_FEATURE_NAMES,
    STATE_FEATURE_NAMES,
    UARCH_FEATURE_NAMES,
    normalized_horizons,
)
from .branch_features import (
    BRANCH_EVENT_CARDINALITIES,
    BRANCH_HISTORY_CARDINALITIES,
)
from ..v30.model import (
    CausalGSSResidualAdapter,
    GSSExposureGate,
    GSSStrengthRouter,
    GSS_ADAPTER_MODES,
    GSS_CONTENT_G1,
    GSS_CONTENT_MASK_ONLY,
    GSS_MODE_CAUSAL_ADAPTER,
    GSS_MODE_CAUSAL_MASK_ONLY_ADAPTER,
    GSS_MODE_NONE,
    normalize_gss_mode,
)
from ..v30.exposure import EXPOSURE_FIELDS


TIMING_ACCUMULATION_CONTRACT = "fp64_prefix_v1"
LONG_HISTORY_MODE_NONE = "none"
LONG_HISTORY_MODE_GLOBAL_RESIDUAL = "global_residual"
LONG_HISTORY_MODE_MEMORY_GATED_CORRECTION = "memory_gated_timing_correction"
LONG_HISTORY_MODES = {
    LONG_HISTORY_MODE_NONE,
    LONG_HISTORY_MODE_GLOBAL_RESIDUAL,
    LONG_HISTORY_MODE_MEMORY_GATED_CORRECTION,
}
BRANCH_MODE_NEURAL_HEAD = "neural_head"
BRANCH_MODE_HEADLESS = "headless"
BRANCH_MODE_REPLAY_EVENT = "replay_event"
BRANCH_MODE_REPLAY_EVENT_HISTORY = "replay_event_history"
BRANCH_MODES = {
    BRANCH_MODE_NEURAL_HEAD,
    BRANCH_MODE_HEADLESS,
    BRANCH_MODE_REPLAY_EVENT,
    BRANCH_MODE_REPLAY_EVENT_HISTORY,
}
BRANCH_MODES_WITHOUT_HEAD = {
    BRANCH_MODE_HEADLESS,
    BRANCH_MODE_REPLAY_EVENT,
    BRANCH_MODE_REPLAY_EVENT_HISTORY,
}


def _normalize_branch_mode(value: Optional[str]) -> str:
    mode = str(
        BRANCH_MODE_NEURAL_HEAD if value is None else value
    ).strip().lower()
    if mode not in BRANCH_MODES:
        raise ValueError(
            f"unsupported v29 branch_mode {value!r}; "
            f"expected one of {sorted(BRANCH_MODES)}"
        )
    return mode


def _normalize_long_history_mode(value: Optional[str], dimension: int) -> str:
    """Preserve the pre-probe default while making new routing opt-in."""
    if value is None:
        return (
            LONG_HISTORY_MODE_GLOBAL_RESIDUAL
            if int(dimension) > 0 else LONG_HISTORY_MODE_NONE
        )
    mode = str(value).strip().lower()
    if mode not in LONG_HISTORY_MODES:
        raise ValueError(
            f"unsupported v29 long_history_mode {value!r}; "
            f"expected one of {sorted(LONG_HISTORY_MODES)}"
        )
    if int(dimension) <= 0 and mode != LONG_HISTORY_MODE_NONE:
        raise ValueError(
            f"v29 long_history_mode={mode} requires long_history_dim > 0"
        )
    if int(dimension) > 0 and mode == LONG_HISTORY_MODE_NONE:
        raise ValueError(
            "v29 long_history_dim > 0 cannot use long_history_mode=none"
        )
    return mode


def _monotonic_prefix_sum(gap: torch.Tensor) -> torch.Tensor:
    """Accumulate positive retirement gaps without FP32 scan backtracking."""
    # CUDA's parallel FP32 scan may round adjacent prefixes through different
    # reduction trees.  When a positive gap is smaller than one FP32 ULP of
    # the running total, the later prefix can then be one ULP *smaller* than
    # the earlier prefix.  Accumulating only these short K<=256 rows in FP64
    # and casting the finished prefixes back preserves non-decreasing order.
    return torch.cumsum(gap.to(torch.float64), dim=1).to(gap.dtype)


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


class BranchCategoricalEncoderV30(nn.Module):
    """Encode compact configured-replay fields before the first QKVR block."""

    def __init__(
        self,
        cardinalities: Sequence[int],
        *,
        d_field: int,
        hidden_dim: int,
        output_dim: int,
    ) -> None:
        super().__init__()
        self.cardinalities = tuple(int(value) for value in cardinalities)
        if any(value <= 0 for value in self.cardinalities):
            raise ValueError("branch feature cardinalities must be positive")
        self.embeddings = nn.ModuleList([
            nn.Embedding(value, int(d_field)) for value in self.cardinalities
        ])
        self.projection = nn.Sequential(
            nn.LayerNorm(len(self.cardinalities) * int(d_field)),
            nn.Linear(len(self.cardinalities) * int(d_field), int(hidden_dim)),
            nn.GELU(),
            nn.Linear(int(hidden_dim), int(output_dim)),
        )
        output = self.projection[-1]
        assert isinstance(output, nn.Linear)
        nn.init.zeros_(output.weight)
        nn.init.zeros_(output.bias)

    def forward(self, values: torch.Tensor) -> torch.Tensor:
        if values.ndim != 3 or int(values.shape[-1]) != len(self.cardinalities):
            raise ValueError(
                "branch feature shape mismatch: "
                f"{tuple(values.shape)} expected [N,K,{len(self.cardinalities)}]"
            )
        parts = []
        for index, embedding in enumerate(self.embeddings):
            field = values[..., index]
            parts.append(embedding(field))
        return self.projection(torch.cat(parts, dim=-1))


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
        long_history_dim: int,
        long_history_hidden: int,
        branch_mode: str,
        branch_field_dim: int,
        branch_hidden: int,
    ) -> None:
        super().__init__()
        self.summary_dim = len(CHUNK_SUMMARY_NAMES)
        self.relation_dim = len(RELATION_FEATURE_NAMES)
        self.uarch_dim = len(UARCH_FEATURE_NAMES)
        self.state_dim = len(STATE_FEATURE_NAMES)
        self.dynamic_sizes = tuple(int(value) for value in DYNAMIC_FIELD_SIZES)
        self.long_history_dim = max(0, int(long_history_dim))
        self.branch_mode = _normalize_branch_mode(branch_mode)
        # Inference-only diagnostic gains.  They are plain runtime attributes,
        # not checkpoint parameters, so existing checkpoints remain compatible
        # and the default path is numerically unchanged.
        self.branch_event_scale = 1.0
        self.branch_history_scale = 1.0
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
        self.long_history_adapter: Optional[nn.Module]
        if self.long_history_dim:
            adapter_hidden = max(1, int(long_history_hidden))
            self.long_history_adapter = nn.Sequential(
                nn.LayerNorm(self.long_history_dim),
                nn.Linear(self.long_history_dim, adapter_hidden),
                nn.GELU(),
                nn.Linear(adapter_hidden, d_dyn),
            )
            # Preserve the exact initial v29 behavior while allowing the new
            # residual branch to learn immediately through its output layer.
            output = self.long_history_adapter[-1]
            assert isinstance(output, nn.Linear)
            nn.init.zeros_(output.weight)
            nn.init.zeros_(output.bias)
        else:
            self.long_history_adapter = None
        self.branch_event_encoder: Optional[BranchCategoricalEncoderV30]
        self.branch_history_encoder: Optional[BranchCategoricalEncoderV30]
        if self.branch_mode in {
            BRANCH_MODE_REPLAY_EVENT,
            BRANCH_MODE_REPLAY_EVENT_HISTORY,
        }:
            self.branch_event_encoder = BranchCategoricalEncoderV30(
                BRANCH_EVENT_CARDINALITIES,
                d_field=int(branch_field_dim),
                hidden_dim=int(branch_hidden),
                output_dim=d_dyn,
            )
        else:
            self.branch_event_encoder = None
        if self.branch_mode == BRANCH_MODE_REPLAY_EVENT_HISTORY:
            self.branch_history_encoder = BranchCategoricalEncoderV30(
                BRANCH_HISTORY_CARDINALITIES,
                d_field=int(branch_field_dim),
                hidden_dim=int(branch_hidden),
                output_dim=d_dyn,
            )
        else:
            self.branch_history_encoder = None
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

    def set_branch_feature_scales(
        self, *, event_scale: float = 1.0, history_scale: float = 1.0,
    ) -> None:
        """Set diagnostic residual gains for v30 branch replay features."""
        event = float(event_scale)
        history = float(history_scale)
        if not math.isfinite(event) or event < 0.0:
            raise ValueError("branch event scale must be finite and non-negative")
        if not math.isfinite(history) or history < 0.0:
            raise ValueError("branch history scale must be finite and non-negative")
        self.branch_event_scale = event
        self.branch_history_scale = history

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
        if self.long_history_adapter is not None:
            history = batch.get("long_history_features")
            if (
                history is None
                or history.ndim != 2
                or int(history.shape[-1]) != self.long_history_dim
            ):
                observed = None if history is None else tuple(history.shape)
                raise ValueError(
                    "v29 long_history_features shape mismatch: "
                    f"{observed} != [C,{self.long_history_dim}]"
                )
            hidden = hidden + self.long_history_adapter(history).unsqueeze(1)
        mask = batch["valid_uop_mask"].bool()
        if self.branch_event_encoder is not None:
            event = batch.get("branch_replay_event")
            if event is None:
                raise ValueError(
                    f"v29 branch_mode={self.branch_mode} requires branch_replay_event"
                )
            event_hidden = self.branch_event_encoder(event)
            if self.branch_event_scale != 1.0:
                event_hidden = event_hidden * self.branch_event_scale
            branch_mask = batch["branch_mask"].bool() & mask
            hidden = hidden + event_hidden * branch_mask.unsqueeze(-1).to(
                event_hidden.dtype
            )
        if self.branch_history_encoder is not None:
            history = batch.get("branch_replay_history")
            if history is None:
                raise ValueError(
                    "v29 branch_mode=replay_event_history requires "
                    "branch_replay_history"
                )
            history_hidden = self.branch_history_encoder(history)
            if self.branch_history_scale != 1.0:
                history_hidden = history_hidden * self.branch_history_scale
            hidden = hidden + history_hidden * mask.unsqueeze(-1).to(
                history_hidden.dtype
            )
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
        long_history_dim: int = 0,
        long_history_hidden: int = 128,
        long_history_mode: Optional[str] = None,
        memory_correction_hidden: int = 128,
        branch_mode: str = BRANCH_MODE_NEURAL_HEAD,
        branch_field_dim: int = 16,
        branch_hidden: int = 128,
        gss_mode: str = GSS_MODE_NONE,
        gss_adapter_dim: int = 128,
        gss_adapter_heads: int = 4,
        gss_field_dim: int = 8,
        gss_exposure_gate: bool = False,
        gss_exposure_gate_hidden: int = 64,
        gss_exposure_gate_minimum: float = 0.25,
        gss_exposure_gate_initial: float = 0.95,
        gss_exposure_gate_dropout: float = 0.0,
        gss_strength_router: bool = False,
        gss_strength_router_hidden: int = 64,
        gss_strength_router_initial: Sequence[float] = (0.05, 0.90, 0.05),
        gss_strength_router_dropout: float = 0.0,
        gss_exposure_features: bool = False,
    ) -> None:
        super().__init__()
        horizon_values = normalized_horizons(horizons)
        self.register_buffer(
            "horizons", torch.tensor(horizon_values, dtype=torch.float32),
            persistent=True,
        )
        self.commit_temperature = float(commit_temperature)
        self.gap_softplus_beta = float(gap_softplus_beta)
        self.long_history_dim = max(0, int(long_history_dim))
        self.long_history_mode = _normalize_long_history_mode(
            long_history_mode, self.long_history_dim,
        )
        self.branch_mode = _normalize_branch_mode(branch_mode)
        self.gss_mode = normalize_gss_mode(gss_mode)
        self.gss_exposure_dim = (
            len(EXPOSURE_FIELDS) if bool(gss_exposure_features) else 0
        )
        self.memory_correction_enabled = True
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
            long_history_dim=(
                self.long_history_dim
                if self.long_history_mode == LONG_HISTORY_MODE_GLOBAL_RESIDUAL
                else 0
            ),
            long_history_hidden=long_history_hidden,
            branch_mode=self.branch_mode,
            branch_field_dim=int(branch_field_dim),
            branch_hidden=int(branch_hidden),
        )
        # Timing and PMU have independent heads.  They share contextual tokens
        # but cannot trade one scalar output against the other.
        self.gap_head = nn.Sequential(
            nn.Linear(d_dyn, d_dyn),
            nn.GELU(),
            nn.Linear(d_dyn, 1),
        )
        self.branch_head: Optional[nn.Module]
        if self.branch_mode == BRANCH_MODE_NEURAL_HEAD:
            self.branch_head = nn.Sequential(
                nn.Linear(d_dyn, max(64, d_dyn // 2)),
                nn.GELU(),
                nn.Linear(max(64, d_dyn // 2), 1),
            )
        else:
            self.branch_head = None
        self.memory_history_projection: Optional[nn.Module]
        self.memory_correction_head: Optional[nn.Module]
        if self.long_history_mode == LONG_HISTORY_MODE_MEMORY_GATED_CORRECTION:
            correction_hidden = max(1, int(memory_correction_hidden))
            self.memory_history_projection = nn.Sequential(
                nn.LayerNorm(self.long_history_dim),
                nn.Linear(self.long_history_dim, correction_hidden),
                nn.GELU(),
            )
            self.memory_correction_head = nn.Sequential(
                nn.Linear(d_dyn + correction_hidden, correction_hidden),
                nn.GELU(),
                nn.Linear(correction_hidden, 1),
            )
            output = self.memory_correction_head[-1]
            assert isinstance(output, nn.Linear)
            nn.init.zeros_(output.weight)
            nn.init.zeros_(output.bias)
        else:
            self.memory_history_projection = None
            self.memory_correction_head = None
        self.gss_adapter: Optional[CausalGSSResidualAdapter]
        if self.gss_mode in GSS_ADAPTER_MODES:
            self.gss_adapter = CausalGSSResidualAdapter(
                token_dim=d_dyn,
                adapter_dim=int(gss_adapter_dim),
                heads=int(gss_adapter_heads),
                field_dim=int(gss_field_dim),
                max_K=max_K,
                content_mode=(
                    GSS_CONTENT_MASK_ONLY
                    if self.gss_mode == GSS_MODE_CAUSAL_MASK_ONLY_ADAPTER
                    else GSS_CONTENT_G1
                ),
            )
        else:
            self.gss_adapter = None
        self.gss_exposure_gate: Optional[GSSExposureGate]
        if bool(gss_exposure_gate):
            if self.gss_adapter is None:
                raise ValueError("GSS exposure gate requires a GSS adapter")
            self.gss_exposure_gate = GSSExposureGate(
                token_dim=d_dyn,
                hidden_dim=int(gss_exposure_gate_hidden),
                minimum=float(gss_exposure_gate_minimum),
                initial=float(gss_exposure_gate_initial),
                dropout=float(gss_exposure_gate_dropout),
            )
        else:
            self.gss_exposure_gate = None
        self.gss_strength_router: Optional[GSSStrengthRouter]
        if bool(gss_strength_router):
            if self.gss_adapter is None:
                raise ValueError("GSS strength router requires a GSS adapter")
            if self.gss_exposure_gate is not None:
                raise ValueError(
                    "legacy GSS exposure gate and strength router are exclusive"
                )
            if (
                self.long_history_mode
                == LONG_HISTORY_MODE_MEMORY_GATED_CORRECTION
            ):
                raise ValueError(
                    "GSS strength router does not compose with memory correction"
                )
            initial = tuple(float(value) for value in gss_strength_router_initial)
            self.gss_strength_router = GSSStrengthRouter(
                token_dim=d_dyn,
                hidden_dim=int(gss_strength_router_hidden),
                initial_weights=initial,
                dropout=float(gss_strength_router_dropout),
                exposure_dim=self.gss_exposure_dim,
            )
        else:
            self.gss_strength_router = None
        if self.gss_exposure_dim and self.gss_strength_router is None:
            raise ValueError("GSS exposure features require the strength router")

    def set_memory_correction_enabled(self, enabled: bool) -> None:
        """Enable normal probe inference or the exact Base-only diagnostic."""
        self.memory_correction_enabled = bool(enabled)

    def set_branch_feature_scales(
        self, *, event_scale: float = 1.0, history_scale: float = 1.0,
    ) -> None:
        """Set inference-only gains for the v30 branch residual inputs."""
        self.interaction.set_branch_feature_scales(
            event_scale=event_scale, history_scale=history_scale,
        )

    def forward_base_timing(
        self, batch: Dict[str, torch.Tensor],
    ) -> Dict[str, torch.Tensor]:
        """Evaluate the no-GSS timing path for a fixed teacher reference."""
        static_tokens = self.static_encoder(batch["per_uop_fields"])
        base_token, _core = self.interaction(static_tokens, batch)
        mask = batch["valid_uop_mask"].bool()
        raw_gap = self.gap_head(base_token).squeeze(-1).float()
        gap = F.softplus(raw_gap, beta=self.gap_softplus_beta)
        gap = gap * mask.to(gap.dtype)
        return {
            "retirement_gap": gap,
            "commit_time": _monotonic_prefix_sum(gap),
            "base_token": base_token,
        }

    def _memory_correction(
        self,
        token: torch.Tensor,
        batch: Dict[str, torch.Tensor],
        valid: torch.Tensor,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        if (
            self.memory_history_projection is None
            or self.memory_correction_head is None
        ):
            raise RuntimeError("v29 memory correction modules are not configured")
        history = batch.get("long_history_features")
        if (
            history is None
            or history.ndim != 2
            or int(history.shape[0]) != int(token.shape[0])
            or int(history.shape[-1]) != self.long_history_dim
        ):
            observed = None if history is None else tuple(history.shape)
            raise ValueError(
                "v29 long_history_features shape mismatch for memory correction: "
                f"{observed} != [{int(token.shape[0])},{self.long_history_dim}]"
            )
        projected = self.memory_history_projection(history)
        expanded = projected.unsqueeze(1).expand(-1, int(token.shape[1]), -1)
        correction = self.memory_correction_head(
            torch.cat([token, expanded], dim=-1)
        ).squeeze(-1).float()
        mem_kind = batch["per_uop_fields"][..., FIELD_INDEX["mem_kind"]]
        memory_mask = valid & (mem_kind >= 1) & (mem_kind <= 3)
        return correction, memory_mask

    def forward_from_static(
        self,
        batch: Dict[str, torch.Tensor],
        static_tokens: torch.Tensor,
        *,
        include_horizon_outputs: bool = True,
    ) -> Dict[str, torch.Tensor]:
        base_token, core = self.interaction(static_tokens, batch)
        return self.forward_from_base(
            batch,
            base_token,
            core,
            include_horizon_outputs=include_horizon_outputs,
        )

    def provisional_timing_from_base(
        self,
        base_token: torch.Tensor,
        valid_uop_mask: torch.Tensor,
    ) -> Dict[str, torch.Tensor]:
        """Predict the deployment ordering clock without another QKVR pass.

        GSS is a pre-access input to the residual adapter, so using the final
        GSS-adjusted time to construct that same input would be circular.  The
        single-forward contract therefore uses the no-GSS timing projection of
        the already computed interaction state as the provisional commit-cycle
        clock.  ``forward_from_base`` then consumes GSS and produces the final
        scheduler clock from the same ``base_token``.
        """
        mask = valid_uop_mask.bool()
        raw_gap = self.gap_head(base_token).squeeze(-1).float()
        gap = F.softplus(raw_gap, beta=self.gap_softplus_beta)
        gap = gap * mask.to(gap.dtype)
        return {
            "raw_gap": raw_gap,
            "retirement_gap": gap,
            "commit_time": _monotonic_prefix_sum(gap),
        }

    def forward_from_base(
        self,
        batch: Dict[str, torch.Tensor],
        base_token: torch.Tensor,
        core: torch.Tensor,
        *,
        include_horizon_outputs: bool = True,
        provisional_raw_gap: Optional[torch.Tensor] = None,
        gss_ablation_mode: str = "full",
    ) -> Dict[str, torch.Tensor]:
        """Finish timing/PMU heads from one previously computed QKVR state."""
        gss_ablation_mode = str(gss_ablation_mode).strip().lower()
        if gss_ablation_mode not in {"full", "gap0"}:
            raise ValueError(
                "gss_ablation_mode must be either 'full' or 'gap0'"
            )
        mask = batch["valid_uop_mask"].bool()
        token = base_token
        gss_delta: Optional[torch.Tensor] = None
        gss_gated_delta: Optional[torch.Tensor] = None
        gss_exposure: Optional[torch.Tensor] = None
        gss_anchor_gaps: Optional[torch.Tensor] = None
        gss_anchor_commit_time: Optional[torch.Tensor] = None
        gss_router_logits: Optional[torch.Tensor] = None
        gss_router_weights: Optional[torch.Tensor] = None
        use_gss = self.gss_adapter is not None and gss_ablation_mode != "gap0"
        if use_gss:
            dense_keys = (
                "gss_uop_categorical", "gss_uop_continuous",
                "gss_memory_mask",
            )
            missing = [key for key in dense_keys if key not in batch]
            if missing:
                raise ValueError(
                    f"v30 GSS adapter requires batch fields {missing}"
                )
            compact_keys = (
                "gss_event_categorical", "gss_event_continuous",
                "gss_event_positions", "gss_event_valid",
                "gss_event_is_memory",
            )
            has_compact = all(key in batch for key in compact_keys)
            gss_delta = self.gss_adapter(
                base_token,
                batch["gss_uop_categorical"],
                batch["gss_uop_continuous"],
                batch["gss_memory_mask"],
                **({
                    "event_categorical": batch["gss_event_categorical"],
                    "event_continuous": batch["gss_event_continuous"],
                    "event_positions": batch["gss_event_positions"],
                    "event_valid": batch["gss_event_valid"],
                    "event_is_memory": batch["gss_event_is_memory"],
                } if has_compact else {}),
            )
            gss_delta = gss_delta * mask.unsqueeze(-1).to(gss_delta.dtype)
            gss_gated_delta = gss_delta
            if self.gss_exposure_gate is not None:
                gss_exposure = self.gss_exposure_gate(
                    base_token, gss_delta, batch["gss_memory_mask"],
                )
                gss_exposure = torch.where(
                    mask, gss_exposure, torch.ones_like(gss_exposure),
                )
                gss_gated_delta = gss_delta * gss_exposure.unsqueeze(-1)
            if self.gss_strength_router is None:
                token = base_token + gss_gated_delta
        # Attention/MLP runs under BF16 autocast, but a retirement prefix is a
        # semantic clock.  Even FP32 CUDA scans can backtrack by one ULP when
        # a near-zero positive gap is added to an O(1K) running total, so use
        # FP64 only for this short K<=256 prefix accumulation.
        if self.gss_strength_router is not None and use_gss:
            if gss_delta is None:
                raise RuntimeError("GSS strength router lacks adapter residual")
            rows, length, token_dim = base_token.shape
            residual_anchor_tokens = torch.stack((
                base_token + 0.25 * gss_delta,
                base_token + gss_delta,
            ), dim=0).reshape(2 * rows, length, token_dim)
            residual_anchor_raw = self.gap_head(
                residual_anchor_tokens
            ).squeeze(-1).float().reshape(2, rows, length).permute(1, 2, 0)
            if provisional_raw_gap is None:
                base_anchor_raw = self.gap_head(
                    base_token
                ).squeeze(-1).float()
            else:
                if provisional_raw_gap.shape != mask.shape:
                    raise ValueError("provisional base-gap shape mismatch")
                base_anchor_raw = provisional_raw_gap.float()
            anchor_raw = torch.cat((
                base_anchor_raw.unsqueeze(-1), residual_anchor_raw,
            ), dim=-1)
            gss_anchor_gaps = F.softplus(
                anchor_raw, beta=self.gap_softplus_beta,
            ) * mask.unsqueeze(-1).to(anchor_raw.dtype)
            gss_anchor_commit_time = _monotonic_prefix_sum(gss_anchor_gaps)
            gss_router_logits, gss_router_weights = self.gss_strength_router(
                base_token.detach(),
                gss_delta.detach(),
                batch["gss_memory_mask"],
                gss_anchor_gaps.detach(),
                batch.get("exposure_features"),
            )
            fallback = torch.zeros_like(gss_router_weights)
            fallback[..., 0] = 1.0
            gss_router_weights = torch.where(
                mask.unsqueeze(-1), gss_router_weights, fallback,
            )
            gap = (gss_anchor_gaps * gss_router_weights).sum(dim=-1)
            expected_scale = (
                0.25 * gss_router_weights[..., 1]
                + gss_router_weights[..., 2]
            )
            gss_gated_delta = gss_delta * expected_scale.unsqueeze(-1)
            token = base_token + gss_gated_delta
            base_gap_logit = anchor_raw[..., 0]
            raw_gap: Optional[torch.Tensor] = None
        else:
            base_gap_logit = self.gap_head(token).squeeze(-1).float()
            raw_gap = base_gap_logit
        correction_logit: Optional[torch.Tensor] = None
        memory_mask: Optional[torch.Tensor] = None
        if self.long_history_mode == LONG_HISTORY_MODE_MEMORY_GATED_CORRECTION:
            assert raw_gap is not None
            correction_logit, memory_mask = self._memory_correction(
                token, batch, mask,
            )
            if self.memory_correction_enabled:
                raw_gap = raw_gap + (
                    correction_logit * memory_mask.to(correction_logit.dtype)
                )
        if self.gss_strength_router is None or not use_gss:
            assert raw_gap is not None
            gap = F.softplus(raw_gap, beta=self.gap_softplus_beta)
            gap = gap * mask.to(gap.dtype)
        commit_time = _monotonic_prefix_sum(gap)
        output = {
            "retirement_gap": gap,
            "commit_time": commit_time,
        }
        if self.gss_exposure_gate is not None and self.gss_exposure_gate.training:
            # Training-only frozen reference for the per-token v29 regret
            # constraint.  Normal validation/inference does not pay for this
            # second timing-head evaluation.
            reference_raw_gap = self.gap_head(base_token).squeeze(-1).float()
            reference_gap = F.softplus(
                reference_raw_gap, beta=self.gap_softplus_beta,
            ) * mask.to(reference_raw_gap.dtype)
            output["gss_reference_commit_time"] = _monotonic_prefix_sum(
                reference_gap,
            ).detach()
        if self.branch_head is not None:
            # P1 is timing-only: GSS cannot silently perturb the frozen branch
            # classifier or receive shortcut gradients from its labels.
            branch_logit = self.branch_head(base_token).squeeze(-1).float()
            branch_probability = torch.sigmoid(branch_logit)
            branch_probability = branch_probability * mask.to(
                branch_probability.dtype
            )
            output.update({
                "branch_miss_logit": branch_logit,
                "branch_miss_probability": branch_probability,
            })
        if correction_logit is not None and memory_mask is not None:
            output.update({
                "base_gap_logit": base_gap_logit.detach(),
                "memory_correction_logit": correction_logit.detach(),
                "memory_correction_mask": memory_mask,
            })
        if gss_delta is not None:
            output.update({
                "gss_adapter_delta": gss_delta,
                "gss_adapter_memory_mask": batch["gss_memory_mask"].bool(),
            })
        if gss_gated_delta is not None and gss_exposure is not None:
            output.update({
                "gss_gated_delta": gss_gated_delta,
                "gss_exposure_gate": gss_exposure,
            })
        if (
            gss_anchor_gaps is not None
            and gss_anchor_commit_time is not None
            and gss_router_logits is not None
            and gss_router_weights is not None
        ):
            output.update({
                "gss_anchor_gaps": gss_anchor_gaps,
                "gss_anchor_commit_time": gss_anchor_commit_time,
                "gss_router_logits": gss_router_logits,
                "gss_router_weights": gss_router_weights,
            })
        if include_horizon_outputs:
            commit_logits = (
                self.horizons.to(commit_time.dtype)[None, None, :]
                - commit_time.unsqueeze(-1)
            ) / self.commit_temperature
            commit_probability = torch.sigmoid(commit_logits)
            commit_probability = commit_probability * mask.unsqueeze(-1).to(
                commit_probability.dtype
            )
            output.update({
                "commit_logits": commit_logits,
                "commit_probability": commit_probability,
                "progress": commit_probability.sum(dim=1),
                "hard_prefix": (
                    commit_time.unsqueeze(-1)
                    <= self.horizons.to(commit_time.dtype)[None, None, :]
                ) & mask.unsqueeze(-1),
                "token_state": token,
                "core_state": core,
            })
        return output

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
        long_history_dim=int(config.get("long_history_dim", 0)),
        long_history_hidden=int(config.get("long_history_hidden", 128)),
        long_history_mode=(
            str(config["long_history_mode"])
            if config.get("long_history_mode") is not None else None
        ),
        memory_correction_hidden=int(config.get("memory_correction_hidden", 128)),
        branch_mode=str(config.get("branch_mode", BRANCH_MODE_NEURAL_HEAD)),
        branch_field_dim=int(config.get("branch_field_dim", 16)),
        branch_hidden=int(config.get("branch_hidden", 128)),
        gss_mode=str(config.get("gss_mode", GSS_MODE_NONE)),
        gss_adapter_dim=int(config.get("gss_adapter_dim", 128)),
        gss_adapter_heads=int(config.get("gss_adapter_heads", 4)),
        gss_field_dim=int(config.get("gss_field_dim", 8)),
        gss_exposure_gate=bool(config.get("gss_exposure_gate", False)),
        gss_exposure_gate_hidden=int(
            config.get("gss_exposure_gate_hidden", 64)
        ),
        gss_exposure_gate_minimum=float(
            config.get("gss_exposure_gate_minimum", 0.25)
        ),
        gss_exposure_gate_initial=float(
            config.get("gss_exposure_gate_initial", 0.95)
        ),
        gss_exposure_gate_dropout=float(
            config.get("gss_exposure_gate_dropout", 0.0)
        ),
        gss_strength_router=bool(config.get("gss_strength_router", False)),
        gss_strength_router_hidden=int(
            config.get("gss_strength_router_hidden", 64)
        ),
        gss_strength_router_initial=tuple(
            float(value) for value in config.get(
                "gss_strength_router_initial", (0.05, 0.90, 0.05),
            )
        ),
        gss_strength_router_dropout=float(
            config.get("gss_strength_router_dropout", 0.0)
        ),
        gss_exposure_features=bool(
            config.get("gss_exposure_features", False)
        ),
    )
