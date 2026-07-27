"""Frozen-macro semantic bridge for the TCSim v29 full-QKVR model.

The Qwen backbone is deliberately absent from this online model.  B2-frozen
receives a compact table of unique, offline-cached macro embeddings plus one
index per UOP.  E2-null runs the exact same trainable bridge with every valid
UOP mapped to one shared ``NO_SEM`` parameter.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Sequence

import torch
import torch.nn as nn
import torch.nn.functional as F

from tcsim.v29.model import TCSimV29Model


SEMANTIC_EXPERIMENT_SCHEMA = "tcsim-v29-b2e2-frozen-semantic-1"
SUPPORTED_VARIANTS = frozenset({"e2-null", "b2-frozen", "b2-lora"})
B2_VARIANTS = frozenset({"b2-frozen", "b2-lora"})
LEGACY_FUSION_ARCHITECTURE = "legacy-static-postnorm-v1"
RESIDUAL_FUSION_ARCHITECTURE = "pre-qkvr-zero-init-residual-v1"
BOUNDED_RESIDUAL_FUSION_ARCHITECTURE = (
    "pre-qkvr-bounded-gated-residual-v2"
)
MULTISLOT_BOUNDED_RESIDUAL_FUSION_ARCHITECTURE = (
    "pre-qkvr-multislot-uop-attn-bounded-residual-v1"
)
RESIDUAL_FUSION_ARCHITECTURES = frozenset({
    RESIDUAL_FUSION_ARCHITECTURE,
    BOUNDED_RESIDUAL_FUSION_ARCHITECTURE,
    MULTISLOT_BOUNDED_RESIDUAL_FUSION_ARCHITECTURE,
})
BOUNDED_RESIDUAL_FUSION_ARCHITECTURES = frozenset({
    BOUNDED_RESIDUAL_FUSION_ARCHITECTURE,
    MULTISLOT_BOUNDED_RESIDUAL_FUSION_ARCHITECTURE,
})
SUPPORTED_FUSION_ARCHITECTURES = frozenset({
    LEGACY_FUSION_ARCHITECTURE,
    *RESIDUAL_FUSION_ARCHITECTURES,
})


class FrozenMacroSemanticBridge(nn.Module):
    """Project cached macro semantics and gate them into v29 static tokens."""

    def __init__(
        self,
        *,
        semantic_dim: int,
        static_dim: int,
        gate_bias: float = -2.0,
    ) -> None:
        super().__init__()
        self.semantic_dim = int(semantic_dim)
        self.static_dim = int(static_dim)
        if self.semantic_dim <= 0 or self.static_dim <= 0:
            raise ValueError("semantic/static dimensions must be positive")
        # Keeping NO_SEM in both variants makes parameter shapes and seeded
        # initialization identical.  In B2 it is used only for padded UOPs.
        self.no_semantic = nn.Parameter(torch.zeros(self.semantic_dim))
        self.semantic_norm = nn.RMSNorm(self.semantic_dim)
        self.semantic_projection = nn.Linear(
            self.semantic_dim, self.static_dim, bias=False,
        )
        self.gate = nn.Linear(self.static_dim * 2, self.static_dim)
        self.output_norm = nn.LayerNorm(self.static_dim)
        nn.init.constant_(self.gate.bias, float(gate_bias))

    def _project_unique(
        self,
        semantic_values: torch.Tensor | None,
        *,
        dtype: torch.dtype,
        device: torch.device,
    ) -> torch.Tensor:
        null = self.no_semantic.to(device=device, dtype=dtype).unsqueeze(0)
        if semantic_values is None:
            values = null
        else:
            if semantic_values.ndim != 2:
                raise ValueError("semantic_values must be [U,D_semantic]")
            if int(semantic_values.shape[1]) != self.semantic_dim:
                raise ValueError(
                    f"semantic width {semantic_values.shape[1]} != "
                    f"{self.semantic_dim}"
                )
            values = torch.cat([
                null,
                semantic_values.to(device=device, dtype=dtype),
            ], dim=0)
        return self.semantic_projection(self.semantic_norm(values))

    def forward(
        self,
        static_tokens: torch.Tensor,
        valid_mask: torch.Tensor,
        *,
        variant: str,
        semantic_values: torch.Tensor | None,
        semantic_index: torch.Tensor | None,
    ) -> torch.Tensor:
        normalized_variant = str(variant).lower()
        if normalized_variant not in SUPPORTED_VARIANTS:
            raise ValueError(f"unsupported semantic variant {variant!r}")
        if static_tokens.ndim != 3:
            raise ValueError("static_tokens must be [N,K,D_static]")
        if tuple(valid_mask.shape) != tuple(static_tokens.shape[:2]):
            raise ValueError("semantic valid mask/static token shape mismatch")
        if int(static_tokens.shape[-1]) != self.static_dim:
            raise ValueError("semantic bridge/static token width mismatch")

        projected = self._project_unique(
            semantic_values if normalized_variant in B2_VARIANTS else None,
            dtype=static_tokens.dtype,
            device=static_tokens.device,
        )
        if normalized_variant == "e2-null":
            semantic = projected[0].view(1, 1, -1).expand_as(static_tokens)
        else:
            if semantic_values is None or semantic_index is None:
                raise ValueError(
                    "B2-frozen requires semantic_values and semantic_index"
                )
            if tuple(semantic_index.shape) != tuple(static_tokens.shape[:2]):
                raise ValueError("semantic_index must be [N,K]")
            indices = semantic_index.to(device=static_tokens.device).long()
            if torch.any(indices[valid_mask.bool()] < 0):
                raise ValueError("B2-frozen has a missing semantic index")
            if torch.any(indices >= int(semantic_values.shape[0])):
                raise ValueError("B2-frozen semantic index is out of range")
            # Table row zero is NO_SEM; cached rows therefore use index + 1.
            semantic = projected[(indices + 1).clamp(min=0)]

        mask = valid_mask.bool().unsqueeze(-1)
        semantic = semantic * mask.to(semantic.dtype)
        gate = torch.sigmoid(self.gate(torch.cat([
            static_tokens, semantic,
        ], dim=-1)))
        fused = self.output_norm(static_tokens + gate * semantic)
        return fused * mask.to(fused.dtype)


class ResidualMacroSemanticAdapter(nn.Module):
    """Zero-initialized semantic side adapter in v29 dynamic-token space.

    Only the semantic branch is normalized.  The final projection is exactly
    zero at initialization and the base hidden state is never post-normalized,
    so attaching this module preserves the frozen v29 function exactly.
    """

    def __init__(
        self,
        *,
        semantic_dim: int,
        dynamic_dim: int,
        hidden_dim: int = 1024,
        gate_bias: float = -2.0,
        max_residual_rms_ratio: float | None = None,
    ) -> None:
        super().__init__()
        self.semantic_dim = int(semantic_dim)
        self.dynamic_dim = int(dynamic_dim)
        self.hidden_dim = int(hidden_dim)
        self.max_residual_rms_ratio = (
            None
            if max_residual_rms_ratio is None
            else float(max_residual_rms_ratio)
        )
        if min(self.semantic_dim, self.dynamic_dim, self.hidden_dim) <= 0:
            raise ValueError("semantic adapter dimensions must be positive")
        if (
            self.max_residual_rms_ratio is not None
            and not 0.0 < self.max_residual_rms_ratio <= 1.0
        ):
            raise ValueError("max residual RMS ratio must be in (0,1]")
        self.no_semantic = nn.Parameter(torch.zeros(self.semantic_dim))
        self.semantic_norm = nn.RMSNorm(self.semantic_dim)
        self.semantic_up = nn.Linear(
            self.semantic_dim, self.hidden_dim, bias=False,
        )
        self.semantic_activation = nn.SiLU()
        self.semantic_projection = nn.Linear(
            self.hidden_dim, self.dynamic_dim, bias=True,
        )
        self.hidden_gate_norm = nn.LayerNorm(self.dynamic_dim)
        self.delta_gate_norm = (
            nn.LayerNorm(self.dynamic_dim)
            if self.max_residual_rms_ratio is not None else None
        )
        self.gate = nn.Linear(self.dynamic_dim * 2, self.dynamic_dim)
        nn.init.zeros_(self.semantic_projection.weight)
        nn.init.zeros_(self.semantic_projection.bias)
        nn.init.constant_(self.gate.bias, float(gate_bias))
        # This is deliberately a Python control flag rather than checkpoint
        # state.  Disabling it bypasses the complete semantic branch and is an
        # exact operational fallback to the frozen strict-v29 function.
        self.semantic_enabled = True
        self._last_residual_penalty: torch.Tensor | None = None
        self._last_residual_max_ratio: torch.Tensor | None = None

    def set_semantic_enabled(self, enabled: bool) -> None:
        self.semantic_enabled = bool(enabled)

    def residual_regularization_loss(self) -> torch.Tensor:
        if self._last_residual_penalty is None:
            raise RuntimeError("semantic adapter has not completed a forward pass")
        return self._last_residual_penalty

    def residual_max_ratio(self) -> torch.Tensor:
        if self._last_residual_max_ratio is None:
            raise RuntimeError("semantic adapter has not completed a forward pass")
        return self._last_residual_max_ratio

    def _project_unique(
        self,
        semantic_values: torch.Tensor | None,
        *,
        dtype: torch.dtype,
        device: torch.device,
    ) -> torch.Tensor:
        null = self.no_semantic.to(device=device, dtype=dtype).unsqueeze(0)
        if semantic_values is None:
            values = null
        else:
            if semantic_values.ndim != 2:
                raise ValueError("semantic_values must be [U,D_semantic]")
            if int(semantic_values.shape[1]) != self.semantic_dim:
                raise ValueError(
                    f"semantic width {semantic_values.shape[1]} != "
                    f"{self.semantic_dim}"
                )
            values = torch.cat([
                null,
                semantic_values.to(device=device, dtype=dtype),
            ], dim=0)
        hidden = self.semantic_up(self.semantic_norm(values))
        return self.semantic_projection(self.semantic_activation(hidden))

    def forward(
        self,
        base_hidden: torch.Tensor,
        valid_mask: torch.Tensor,
        *,
        variant: str,
        semantic_values: torch.Tensor | None,
        semantic_index: torch.Tensor | None,
    ) -> torch.Tensor:
        normalized_variant = str(variant).lower()
        if normalized_variant not in SUPPORTED_VARIANTS:
            raise ValueError(f"unsupported semantic variant {variant!r}")
        if base_hidden.ndim != 3:
            raise ValueError("base_hidden must be [N,K,D_dynamic]")
        if tuple(valid_mask.shape) != tuple(base_hidden.shape[:2]):
            raise ValueError("semantic valid mask/hidden shape mismatch")
        if int(base_hidden.shape[-1]) != self.dynamic_dim:
            raise ValueError("semantic adapter/dynamic token width mismatch")

        if not self.semantic_enabled:
            zero = base_hidden.sum() * 0.0
            self._last_residual_penalty = zero
            self._last_residual_max_ratio = zero.detach()
            return base_hidden

        projected = self._project_unique(
            semantic_values if normalized_variant in B2_VARIANTS else None,
            dtype=base_hidden.dtype,
            device=base_hidden.device,
        )
        if normalized_variant == "e2-null":
            delta = projected[0].view(1, 1, -1).expand_as(base_hidden)
        else:
            if semantic_values is None or semantic_index is None:
                raise ValueError(
                    "B2-frozen requires semantic_values and semantic_index"
                )
            if tuple(semantic_index.shape) != tuple(base_hidden.shape[:2]):
                raise ValueError("semantic_index must be [N,K]")
            indices = semantic_index.to(device=base_hidden.device).long()
            valid = valid_mask.bool()
            if torch.any(indices[valid] < 0):
                raise ValueError("B2-frozen has a missing semantic index")
            if torch.any(indices >= int(semantic_values.shape[0])):
                raise ValueError("B2-frozen semantic index is out of range")
            delta = projected[(indices + 1).clamp(min=0)]

        mask = valid_mask.bool().unsqueeze(-1)
        delta = delta * mask.to(delta.dtype)
        gate_delta = (
            self.delta_gate_norm(delta)
            if self.delta_gate_norm is not None else delta
        )
        gate = torch.sigmoid(self.gate(torch.cat([
            self.hidden_gate_norm(base_hidden), gate_delta,
        ], dim=-1)))
        base_rms = base_hidden.float().square().mean(
            dim=-1, keepdim=True,
        ).add(1e-12).sqrt()
        if self.max_residual_rms_ratio is not None:
            # Element-wise tanh bounds also imply the stronger per-token RMS
            # bound ||residual||_RMS <= ratio * ||base||_RMS.  Computing the
            # saturation in fp32 keeps the bound reliable under bf16 AMP.
            limit = base_rms * self.max_residual_rms_ratio
            bounded_delta = (
                limit * torch.tanh(delta.float() / limit.clamp_min(1e-12))
            ).to(delta.dtype)
        else:
            bounded_delta = delta
        applied = gate * bounded_delta * mask.to(delta.dtype)
        relative = applied.float() / base_rms.clamp_min(1e-12)
        valid_elements = (
            mask.sum().clamp(min=1).to(relative.dtype) * self.dynamic_dim
        )
        self._last_residual_penalty = relative.square().sum() / valid_elements
        self._last_residual_max_ratio = relative.abs().max().detach()
        # No normalization is allowed after this residual addition.  At
        # initialization delta is bitwise zero, preserving the v29 checkpoint.
        return base_hidden + applied


class MultiSlotResidualMacroSemanticAdapter(nn.Module):
    """UOP-conditioned attention over learned slots from one cached macro.

    Each unique cached macro vector is expanded into a small key/value bank.
    Every v29 UOP then queries the bank belonging to its macro.  The output
    projection is zero-initialized and the applied update is RMS-bounded, so
    construction is an exact strict-v29 identity and the complete side branch
    remains operationally bypassable.
    """

    def __init__(
        self,
        *,
        semantic_dim: int,
        dynamic_dim: int,
        hidden_dim: int = 1024,
        slot_count: int = 4,
        attention_heads: int = 4,
        gate_bias: float = -2.0,
        max_residual_rms_ratio: float = 0.10,
    ) -> None:
        super().__init__()
        self.semantic_dim = int(semantic_dim)
        self.dynamic_dim = int(dynamic_dim)
        self.hidden_dim = int(hidden_dim)
        self.slot_count = int(slot_count)
        self.attention_heads = int(attention_heads)
        self.max_residual_rms_ratio = float(max_residual_rms_ratio)
        if min(
            self.semantic_dim,
            self.dynamic_dim,
            self.hidden_dim,
            self.slot_count,
            self.attention_heads,
        ) <= 0:
            raise ValueError("multislot semantic dimensions must be positive")
        if self.dynamic_dim % self.attention_heads:
            raise ValueError(
                "dynamic_dim must be divisible by semantic attention heads"
            )
        if not 0.0 < self.max_residual_rms_ratio <= 1.0:
            raise ValueError("max residual RMS ratio must be in (0,1]")

        self.head_dim = self.dynamic_dim // self.attention_heads
        self.no_semantic = nn.Parameter(torch.zeros(self.semantic_dim))
        self.semantic_norm = nn.RMSNorm(self.semantic_dim)
        self.semantic_up = nn.Linear(
            self.semantic_dim, self.hidden_dim, bias=False,
        )
        self.semantic_activation = nn.SiLU()
        self.slot_projection = nn.Linear(
            self.hidden_dim,
            self.slot_count * 2 * self.dynamic_dim,
            bias=True,
        )
        self.query_norm = nn.LayerNorm(self.dynamic_dim)
        self.query_projection = nn.Linear(
            self.dynamic_dim, self.dynamic_dim, bias=False,
        )
        self.semantic_projection = nn.Linear(
            self.dynamic_dim, self.dynamic_dim, bias=True,
        )
        self.hidden_gate_norm = nn.LayerNorm(self.dynamic_dim)
        self.delta_gate_norm = nn.LayerNorm(self.dynamic_dim)
        self.gate = nn.Linear(self.dynamic_dim * 2, self.dynamic_dim)
        nn.init.zeros_(self.semantic_projection.weight)
        nn.init.zeros_(self.semantic_projection.bias)
        nn.init.constant_(self.gate.bias, float(gate_bias))
        self.semantic_enabled = True
        self._last_residual_penalty: torch.Tensor | None = None
        self._last_residual_max_ratio: torch.Tensor | None = None

    def set_semantic_enabled(self, enabled: bool) -> None:
        self.semantic_enabled = bool(enabled)

    def residual_regularization_loss(self) -> torch.Tensor:
        if self._last_residual_penalty is None:
            raise RuntimeError("semantic adapter has not completed a forward pass")
        return self._last_residual_penalty

    def residual_max_ratio(self) -> torch.Tensor:
        if self._last_residual_max_ratio is None:
            raise RuntimeError("semantic adapter has not completed a forward pass")
        return self._last_residual_max_ratio

    def _project_unique_slots(
        self,
        semantic_values: torch.Tensor | None,
        *,
        dtype: torch.dtype,
        device: torch.device,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        null = self.no_semantic.to(device=device, dtype=dtype).unsqueeze(0)
        if semantic_values is None:
            values = null
        else:
            if semantic_values.ndim != 2:
                raise ValueError("semantic_values must be [U,D_semantic]")
            if int(semantic_values.shape[1]) != self.semantic_dim:
                raise ValueError(
                    f"semantic width {semantic_values.shape[1]} != "
                    f"{self.semantic_dim}"
                )
            values = torch.cat([
                null,
                semantic_values.to(device=device, dtype=dtype),
            ], dim=0)
        hidden = self.semantic_up(self.semantic_norm(values))
        packed = self.slot_projection(self.semantic_activation(hidden))
        packed = packed.view(
            values.shape[0], self.slot_count, 2, self.dynamic_dim,
        )
        return packed[:, :, 0], packed[:, :, 1]

    def forward(
        self,
        base_hidden: torch.Tensor,
        valid_mask: torch.Tensor,
        *,
        variant: str,
        semantic_values: torch.Tensor | None,
        semantic_index: torch.Tensor | None,
    ) -> torch.Tensor:
        normalized_variant = str(variant).lower()
        if normalized_variant not in SUPPORTED_VARIANTS:
            raise ValueError(f"unsupported semantic variant {variant!r}")
        if base_hidden.ndim != 3:
            raise ValueError("base_hidden must be [N,K,D_dynamic]")
        if tuple(valid_mask.shape) != tuple(base_hidden.shape[:2]):
            raise ValueError("semantic valid mask/hidden shape mismatch")
        if int(base_hidden.shape[-1]) != self.dynamic_dim:
            raise ValueError("semantic adapter/dynamic token width mismatch")

        if not self.semantic_enabled:
            zero = base_hidden.sum() * 0.0
            self._last_residual_penalty = zero
            self._last_residual_max_ratio = zero.detach()
            return base_hidden

        unique_keys, unique_values = self._project_unique_slots(
            semantic_values if normalized_variant in B2_VARIANTS else None,
            dtype=base_hidden.dtype,
            device=base_hidden.device,
        )
        if normalized_variant == "e2-null":
            shape = (
                base_hidden.shape[0],
                base_hidden.shape[1],
                self.slot_count,
                self.dynamic_dim,
            )
            keys = unique_keys[0].view(
                1, 1, self.slot_count, self.dynamic_dim,
            ).expand(shape)
            values = unique_values[0].view(
                1, 1, self.slot_count, self.dynamic_dim,
            ).expand(shape)
        else:
            if semantic_values is None or semantic_index is None:
                raise ValueError(
                    "B2-frozen requires semantic_values and semantic_index"
                )
            if tuple(semantic_index.shape) != tuple(base_hidden.shape[:2]):
                raise ValueError("semantic_index must be [N,K]")
            indices = semantic_index.to(device=base_hidden.device).long()
            valid = valid_mask.bool()
            if torch.any(indices[valid] < 0):
                raise ValueError("B2-frozen has a missing semantic index")
            if torch.any(indices >= int(semantic_values.shape[0])):
                raise ValueError("B2-frozen semantic index is out of range")
            table_indices = (indices + 1).clamp(min=0)
            keys = unique_keys[table_indices]
            values = unique_values[table_indices]

        N, K = base_hidden.shape[:2]
        query = self.query_projection(self.query_norm(base_hidden)).view(
            N, K, self.attention_heads, self.head_dim,
        )
        keys = keys.view(
            N, K, self.slot_count, self.attention_heads, self.head_dim,
        ).permute(0, 1, 3, 2, 4)
        values = values.view(
            N, K, self.slot_count, self.attention_heads, self.head_dim,
        ).permute(0, 1, 3, 2, 4)
        scores = torch.einsum(
            "nkhd,nkhsd->nkhs", query, keys,
        ) * (self.head_dim ** -0.5)
        attention = torch.softmax(scores.float(), dim=-1).to(values.dtype)
        context = torch.einsum(
            "nkhs,nkhsd->nkhd", attention, values,
        ).reshape(N, K, self.dynamic_dim)
        delta = self.semantic_projection(context)

        mask = valid_mask.bool().unsqueeze(-1)
        delta = delta * mask.to(delta.dtype)
        gate = torch.sigmoid(self.gate(torch.cat([
            self.hidden_gate_norm(base_hidden),
            self.delta_gate_norm(delta),
        ], dim=-1)))
        base_rms = base_hidden.float().square().mean(
            dim=-1, keepdim=True,
        ).add(1e-12).sqrt()
        limit = base_rms * self.max_residual_rms_ratio
        bounded_delta = (
            limit * torch.tanh(delta.float() / limit.clamp_min(1e-12))
        ).to(delta.dtype)
        applied = gate * bounded_delta * mask.to(delta.dtype)
        relative = applied.float() / base_rms.clamp_min(1e-12)
        valid_elements = (
            mask.sum().clamp(min=1).to(relative.dtype) * self.dynamic_dim
        )
        self._last_residual_penalty = relative.square().sum() / valid_elements
        self._last_residual_max_ratio = relative.abs().max().detach()
        return base_hidden + applied


class TCSimV29SemanticModel(nn.Module):
    """TCSim v29 with a controlled pre-full-QKVR semantic injection."""

    def __init__(
        self,
        *,
        variant: str,
        semantic_dim: int,
        horizons: Sequence[float],
        model_config: Mapping[str, Any],
        fusion_architecture: str = LEGACY_FUSION_ARCHITECTURE,
        semantic_adapter_hidden_dim: int = 1024,
        semantic_max_residual_rms_ratio: float = 0.05,
        semantic_slot_count: int = 4,
        semantic_attention_heads: int = 4,
    ) -> None:
        super().__init__()
        self.variant = str(variant).lower()
        if self.variant not in SUPPORTED_VARIANTS:
            raise ValueError(f"unsupported semantic variant {variant!r}")
        self.fusion_architecture = str(fusion_architecture).lower()
        if self.fusion_architecture not in SUPPORTED_FUSION_ARCHITECTURES:
            raise ValueError(
                "unsupported semantic fusion architecture "
                f"{fusion_architecture!r}"
            )
        config = dict(model_config)
        static_dim = int(config.get("d_static", 256))
        dynamic_dim = int(config.get("d_dyn", 384))
        self.backbone = TCSimV29Model(
            horizons=horizons,
            d_field=int(config.get("d_field", 32)),
            d_dynamic_field=int(config.get("d_dynamic_field", 16)),
            d_static=static_dim,
            d_dyn=int(config.get("d_dyn", 384)),
            n_heads=int(config.get("n_dyn_heads", 8)),
            n_layers=int(config.get("n_dyn_layers", 4)),
            ffn_dim=(
                int(config["ffn_dim"])
                if config.get("ffn_dim") is not None else None
            ),
            dropout=float(config.get("dropout", 0.1)),
            max_K=int(config.get("max_K", 256)),
            cross_target_block=int(config.get("cross_target_block", 0)),
            sdpa_backend=str(config.get("sdpa_backend", "auto")),
            commit_temperature=float(config.get("commit_temperature", 4.0)),
            gap_softplus_beta=float(config.get("gap_softplus_beta", 4.0)),
        )
        if self.fusion_architecture == LEGACY_FUSION_ARCHITECTURE:
            self.semantic_bridge = FrozenMacroSemanticBridge(
                semantic_dim=int(semantic_dim),
                static_dim=static_dim,
                gate_bias=float(config.get("semantic_gate_bias", -2.0)),
            )
        elif (
            self.fusion_architecture
            == MULTISLOT_BOUNDED_RESIDUAL_FUSION_ARCHITECTURE
        ):
            self.semantic_bridge = MultiSlotResidualMacroSemanticAdapter(
                semantic_dim=int(semantic_dim),
                dynamic_dim=dynamic_dim,
                hidden_dim=int(semantic_adapter_hidden_dim),
                slot_count=int(semantic_slot_count),
                attention_heads=int(semantic_attention_heads),
                gate_bias=float(config.get("semantic_gate_bias", -2.0)),
                max_residual_rms_ratio=float(
                    semantic_max_residual_rms_ratio
                ),
            )
        else:
            self.semantic_bridge = ResidualMacroSemanticAdapter(
                semantic_dim=int(semantic_dim),
                dynamic_dim=dynamic_dim,
                hidden_dim=int(semantic_adapter_hidden_dim),
                gate_bias=float(config.get("semantic_gate_bias", -2.0)),
                max_residual_rms_ratio=(
                    float(semantic_max_residual_rms_ratio)
                    if self.fusion_architecture
                    in BOUNDED_RESIDUAL_FUSION_ARCHITECTURES
                    else None
                ),
            )

    def set_semantic_enabled(self, enabled: bool) -> None:
        """Enable semantics or exactly bypass them for strict fallback."""

        if isinstance(
            self.semantic_bridge,
            (
                ResidualMacroSemanticAdapter,
                MultiSlotResidualMacroSemanticAdapter,
            ),
        ):
            self.semantic_bridge.set_semantic_enabled(enabled)
        elif not enabled:
            raise RuntimeError("legacy semantic fusion cannot be exactly bypassed")

    @property
    def static_encoder(self) -> nn.Module:
        """Expose the upstream inference runner's static-cache interface."""

        return self.backbone.static_encoder

    def forward_from_static(
        self,
        batch: Dict[str, torch.Tensor],
        static_tokens: torch.Tensor,
        *,
        include_horizon_outputs: bool = True,
    ) -> Dict[str, torch.Tensor]:
        if self.fusion_architecture in RESIDUAL_FUSION_ARCHITECTURES:
            return self._forward_residual_adapter(
                batch,
                static_tokens,
                include_horizon_outputs=include_horizon_outputs,
            )
        fused_tokens = self.semantic_bridge(
            static_tokens,
            batch["valid_uop_mask"],
            variant=self.variant,
            semantic_values=batch.get("semantic_values"),
            semantic_index=batch.get("semantic_index"),
        )
        return self.backbone.forward_from_static(
            batch,
            fused_tokens,
            include_horizon_outputs=include_horizon_outputs,
        )

    def _forward_residual_adapter(
        self,
        batch: Dict[str, torch.Tensor],
        static_tokens: torch.Tensor,
        *,
        include_horizon_outputs: bool,
    ) -> Dict[str, torch.Tensor]:
        """Mirror authoritative v29 around one zero-init residual hook."""

        interaction = self.backbone.interaction
        dynamic = batch["dynamic_uop_fields"]
        dynamic_parts = [
            embedding(dynamic[..., index].clamp(0, size))
            for index, (embedding, size) in enumerate(zip(
                interaction.dynamic_embeddings, interaction.dynamic_sizes,
            ))
        ]
        side = torch.cat([
            batch["chunk_summary"],
            batch["relation_features"],
            batch["uarch_features"],
            batch["state_features"],
        ], dim=-1)
        hidden = interaction.token_projection(static_tokens)
        hidden = hidden + interaction.dynamic_projection(torch.cat(
            dynamic_parts, dim=-1,
        ))
        hidden = hidden + interaction.side_projection(
            interaction.side_norm(side),
        ).unsqueeze(1)
        hidden = self.semantic_bridge(
            hidden,
            batch["valid_uop_mask"],
            variant=self.variant,
            semantic_values=batch.get("semantic_values"),
            semantic_index=batch.get("semantic_index"),
        )
        mask = batch["valid_uop_mask"].bool()
        hidden = hidden * mask.unsqueeze(-1).to(hidden.dtype)
        cross_gate = torch.sigmoid(interaction.cross_gate(torch.cat([
            batch["relation_features"], batch["state_features"],
        ], dim=-1)))
        for layer in interaction.layers:
            hidden = layer(hidden, mask, batch["sample_ptr"], cross_gate)
        token = interaction.final_norm(hidden)
        token = token * mask.unsqueeze(-1).to(token.dtype)
        denominator = mask.sum(dim=1, keepdim=True).clamp(min=1).to(token.dtype)
        core = token.sum(dim=1) / denominator

        raw_gap = self.backbone.gap_head(token).squeeze(-1).float()
        gap = F.softplus(raw_gap, beta=self.backbone.gap_softplus_beta)
        gap = gap * mask.to(gap.dtype)
        commit_time = torch.cumsum(gap.to(torch.float64), dim=1).to(gap.dtype)
        branch_logit = self.backbone.branch_head(token).squeeze(-1).float()
        branch_probability = torch.sigmoid(branch_logit)
        branch_probability = branch_probability * mask.to(
            branch_probability.dtype,
        )
        output = {
            "retirement_gap": gap,
            "commit_time": commit_time,
            "branch_miss_logit": branch_logit,
            "branch_miss_probability": branch_probability,
        }
        if include_horizon_outputs:
            horizons = self.backbone.horizons.to(commit_time.dtype)
            commit_logits = (
                horizons[None, None, :] - commit_time.unsqueeze(-1)
            ) / self.backbone.commit_temperature
            commit_probability = torch.sigmoid(commit_logits)
            commit_probability = commit_probability * mask.unsqueeze(-1).to(
                commit_probability.dtype,
            )
            output.update({
                "commit_logits": commit_logits,
                "commit_probability": commit_probability,
                "progress": commit_probability.sum(dim=1),
                "hard_prefix": (
                    commit_time.unsqueeze(-1) <= horizons[None, None, :]
                ) & mask.unsqueeze(-1),
                "token_state": token,
                "core_state": core,
            })
        return output

    def forward(self, batch: Dict[str, torch.Tensor]) -> Dict[str, torch.Tensor]:
        static_tokens = self.backbone.static_encoder(batch["per_uop_fields"])
        return self.forward_from_static(batch, static_tokens)


def build_semantic_model(
    config: Mapping[str, Any],
    horizons: Sequence[float],
    *,
    variant: str,
    semantic_dim: int,
    fusion_architecture: str = LEGACY_FUSION_ARCHITECTURE,
    semantic_adapter_hidden_dim: int = 1024,
    semantic_max_residual_rms_ratio: float = 0.05,
    semantic_slot_count: int = 4,
    semantic_attention_heads: int = 4,
) -> TCSimV29SemanticModel:
    return TCSimV29SemanticModel(
        variant=variant,
        semantic_dim=semantic_dim,
        horizons=horizons,
        model_config=config,
        fusion_architecture=fusion_architecture,
        semantic_adapter_hidden_dim=semantic_adapter_hidden_dim,
        semantic_max_residual_rms_ratio=semantic_max_residual_rms_ratio,
        semantic_slot_count=semantic_slot_count,
        semantic_attention_heads=semantic_attention_heads,
    )
