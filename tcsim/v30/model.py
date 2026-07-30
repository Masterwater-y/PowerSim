"""Neural modules for the v30 cache-only GSS probes."""
from __future__ import annotations

import math
from typing import Optional, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F

from .gss import (
    GSS_CATEGORICAL_CARDINALITIES,
    GSS_CATEGORICAL_FIELDS,
    GSS_CONTINUOUS_FIELDS,
    GSS_G1_CONTINUOUS_FIELDS,
)


GSS_MODE_NONE = "none"
GSS_MODE_CAUSAL_ADAPTER = "causal_adapter"
GSS_MODE_CAUSAL_MASK_ONLY_ADAPTER = "causal_mask_only_adapter"
GSS_ADAPTER_MODES = {
    GSS_MODE_CAUSAL_ADAPTER,
    GSS_MODE_CAUSAL_MASK_ONLY_ADAPTER,
}
GSS_MODES = {GSS_MODE_NONE, *GSS_ADAPTER_MODES}
GSS_CONTENT_G1 = "g1"
GSS_CONTENT_MASK_ONLY = "mask_only"
GSS_CONTENT_MODES = {GSS_CONTENT_G1, GSS_CONTENT_MASK_ONLY}
G1_CONTINUOUS_INDICES: Tuple[int, ...] = tuple(
    GSS_CONTINUOUS_FIELDS.index(name) for name in GSS_G1_CONTINUOUS_FIELDS
)


def normalize_gss_mode(value: str | None) -> str:
    mode = str(GSS_MODE_NONE if value is None else value).strip().lower()
    if mode not in GSS_MODES:
        raise ValueError(
            f"unsupported v30 GSS mode {value!r}; expected {sorted(GSS_MODES)}"
        )
    return mode


class CausalGSSResidualAdapter(nn.Module):
    """Attend from frozen v29 tokens to the causal G1 memory-event stream.

    The output projection is exactly zero-initialized.  A freshly constructed
    adapter therefore preserves every canonical-v29 timing prediction bit for
    bit, while still allowing gradients to train the residual branch.
    """

    def __init__(
        self,
        token_dim: int,
        adapter_dim: int = 128,
        heads: int = 4,
        field_dim: int = 8,
        max_K: int = 256,
        content_mode: str = GSS_CONTENT_G1,
    ) -> None:
        super().__init__()
        token_dim = int(token_dim)
        adapter_dim = int(adapter_dim)
        heads = int(heads)
        field_dim = int(field_dim)
        if adapter_dim <= 0 or heads <= 0 or adapter_dim % heads:
            raise ValueError("GSS adapter_dim must be positive and divisible by heads")
        if field_dim <= 0:
            raise ValueError("GSS field_dim must be positive")
        content_mode = str(content_mode).strip().lower()
        if content_mode not in GSS_CONTENT_MODES:
            raise ValueError(
                f"unsupported GSS content mode {content_mode!r}; "
                f"expected {sorted(GSS_CONTENT_MODES)}"
            )
        self.heads = heads
        self.head_dim = adapter_dim // heads
        self.max_K = int(max_K)
        self.content_mode = content_mode
        self.embeddings = nn.ModuleList([
            nn.Embedding(int(cardinality), field_dim)
            for cardinality in GSS_CATEGORICAL_CARDINALITIES
        ])
        encoded_dim = (
            len(GSS_CATEGORICAL_FIELDS) * field_dim
            + len(GSS_G1_CONTINUOUS_FIELDS)
        )
        self.gss_encoder = nn.Sequential(
            nn.LayerNorm(encoded_dim),
            nn.Linear(encoded_dim, adapter_dim),
            nn.GELU(),
            nn.Linear(adapter_dim, adapter_dim),
        )
        self.token_norm = nn.LayerNorm(token_dim)
        self.q_projection = nn.Linear(token_dim, adapter_dim, bias=False)
        self.k_projection = nn.Linear(adapter_dim, adapter_dim, bias=False)
        self.v_projection = nn.Linear(adapter_dim, adapter_dim, bias=False)
        # No bias: with no causal memory key the attended vector is exactly
        # zero, so the adapter cannot learn an unconditional timing shortcut.
        self.output = nn.Linear(adapter_dim, token_dim, bias=False)
        self.register_buffer(
            "causal_mask",
            torch.ones((self.max_K, self.max_K), dtype=torch.bool).tril(),
            persistent=False,
        )
        nn.init.zeros_(self.output.weight)

    def _validate_inputs(
        self,
        token: torch.Tensor,
        categorical: torch.Tensor,
        continuous: torch.Tensor,
        memory_mask: torch.Tensor,
    ) -> None:
        if token.ndim != 3:
            raise ValueError("GSS token input must be [N,K,D]")
        rows, length, _ = token.shape
        if length > self.max_K:
            raise ValueError(f"GSS K={length} exceeds max_K={self.max_K}")
        if categorical.shape != (
            rows, length, len(GSS_CATEGORICAL_FIELDS),
        ):
            raise ValueError(
                f"GSS categorical shape mismatch: {tuple(categorical.shape)}"
            )
        if continuous.shape != (
            rows, length, len(GSS_CONTINUOUS_FIELDS),
        ):
            raise ValueError(
                f"GSS continuous shape mismatch: {tuple(continuous.shape)}"
            )
        if memory_mask.shape != (rows, length):
            raise ValueError(f"GSS memory mask shape mismatch: {tuple(memory_mask.shape)}")
        # Value ranges are part of the sidecar loader's data contract.  Do not
        # add per-forward GPU synchronizations here; embedding lookup remains
        # a final fail-closed guard if a malformed batch bypasses that loader.

    @staticmethod
    def _mask_only_features(
        memory_mask: torch.Tensor,
        *,
        dtype: torch.dtype,
    ) -> torch.Tensor:
        """Encode only the causal geometry of a binary memory-event mask."""
        rows, length = memory_mask.shape
        positions = torch.arange(
            length, device=memory_mask.device, dtype=dtype,
        ).view(1, length).expand(rows, -1)
        event_number = memory_mask.cumsum(dim=1).to(dtype)
        event_positions = torch.where(
            memory_mask,
            positions,
            torch.full_like(positions, -1.0),
        )
        last_event = torch.cummax(event_positions, dim=1).values
        previous_event = torch.cat([
            torch.full_like(last_event[:, :1], -1.0),
            last_event[:, :-1],
        ], dim=1)
        position_denominator = positions + 1.0
        length_denominator = float(max(1, length))
        log_denominator = torch.log1p(torch.tensor(
            length_denominator, device=memory_mask.device, dtype=dtype,
        ))
        features = torch.stack([
            position_denominator / length_denominator,
            torch.log1p(event_number) / log_denominator,
            event_number / position_denominator,
            (positions - previous_event).clamp(min=0.0) / length_denominator,
        ], dim=-1)
        return features * memory_mask.unsqueeze(-1).to(dtype)

    def forward(
        self,
        token: torch.Tensor,
        categorical: torch.Tensor,
        continuous: torch.Tensor,
        memory_mask: torch.Tensor,
        *,
        event_categorical: Optional[torch.Tensor] = None,
        event_continuous: Optional[torch.Tensor] = None,
        event_positions: Optional[torch.Tensor] = None,
        event_valid: Optional[torch.Tensor] = None,
        event_is_memory: Optional[torch.Tensor] = None,
    ) -> torch.Tensor:
        self._validate_inputs(token, categorical, continuous, memory_mask)
        memory_mask = memory_mask.bool()
        packed_values = (
            event_categorical, event_continuous, event_positions,
            event_valid, event_is_memory,
        )
        packed = all(value is not None for value in packed_values)
        if any(value is not None for value in packed_values) and not packed:
            raise ValueError("GSS packed event fields must be provided together")
        rows, length, _ = token.shape
        if packed:
            assert event_categorical is not None
            assert event_continuous is not None
            assert event_positions is not None
            assert event_valid is not None
            assert event_is_memory is not None
            event_length = int(event_positions.shape[1])
            if event_categorical.shape != (
                rows, event_length, len(GSS_CATEGORICAL_FIELDS),
            ):
                raise ValueError("GSS packed categorical shape mismatch")
            if event_continuous.shape != (
                rows, event_length, len(GSS_CONTINUOUS_FIELDS),
            ):
                raise ValueError("GSS packed continuous shape mismatch")
            if event_valid.shape != (rows, event_length):
                raise ValueError("GSS packed valid shape mismatch")
            if event_is_memory.shape != (rows, event_length):
                raise ValueError("GSS packed memory shape mismatch")
            categorical_source = event_categorical
            continuous_source = event_continuous
            memory_source = event_is_memory.bool()
        else:
            categorical_source = categorical
            continuous_source = continuous
            memory_source = memory_mask
            event_positions = None
            event_valid = None
        if self.content_mode == GSS_CONTENT_MASK_ONLY:
            # Preserve only causal event position/order/density/gap.  These
            # are derived from the binary mask itself; no cache-state value is
            # retained.  Keep the same modules so nominal capacity is equal.
            categorical_source = torch.zeros_like(categorical_source)
            dense_mask_features = self._mask_only_features(
                memory_mask, dtype=continuous.dtype,
            )
            if packed:
                assert event_positions is not None
                gather_index = event_positions.clamp(0, length - 1).unsqueeze(-1)
                g1_continuous = torch.gather(
                    dense_mask_features, 1,
                    gather_index.expand(-1, -1, dense_mask_features.shape[-1]),
                )
                g1_continuous = g1_continuous * memory_source.unsqueeze(-1).to(
                    g1_continuous.dtype
                )
            else:
                g1_continuous = dense_mask_features
        else:
            g1_continuous = continuous_source[..., list(G1_CONTINUOUS_INDICES)]
        parts = [
            embedding(categorical_source[..., index])
            for index, embedding in enumerate(self.embeddings)
        ]
        gss = self.gss_encoder(torch.cat(parts + [g1_continuous], dim=-1))
        gss = gss * memory_source.unsqueeze(-1).to(gss.dtype)
        q = self.q_projection(self.token_norm(token))
        k = self.k_projection(gss)
        v = self.v_projection(gss)
        q = q.view(rows, length, self.heads, self.head_dim).transpose(1, 2)
        key_length = int(gss.shape[1])
        k = k.view(rows, key_length, self.heads, self.head_dim).transpose(1, 2)
        v = v.view(rows, key_length, self.heads, self.head_dim).transpose(1, 2)
        if packed:
            assert event_positions is not None and event_valid is not None
            query_positions = torch.arange(
                length, device=token.device, dtype=event_positions.dtype,
            ).view(1, 1, length, 1)
            allowed = (
                event_positions.view(rows, 1, 1, key_length) <= query_positions
            ) & event_valid.bool().view(rows, 1, 1, key_length)
        else:
            # Position zero acts as a zero sentinel when the prefix has no
            # memory access.  If it is a memory access it is the real key.
            key_mask = memory_mask.clone()
            key_mask[:, 0] = True
            causal = self.causal_mask[:length, :length]
            allowed = (
                causal.view(1, 1, length, length)
                & key_mask.view(rows, 1, 1, length)
            )
        attended = F.scaled_dot_product_attention(q, k, v, attn_mask=allowed)
        attended = attended.transpose(1, 2).reshape(rows, length, -1)
        return self.output(attended)


class GSSExposureGate(nn.Module):
    """Bound the learned GSS residual strength independently at every UOP.

    The gate deliberately has no workload/trace identifier.  It sees the
    frozen canonical token, the frozen GSS residual, and three causal local
    signals derived from the memory-event prefix.  A lower bound preserves the
    strong alpha=0.25 scale-sweep baseline while allowing useful contexts to
    retain the full trained G1 residual.
    """

    def __init__(
        self,
        token_dim: int,
        hidden_dim: int = 64,
        minimum: float = 0.25,
        initial: float = 0.95,
        dropout: float = 0.0,
    ) -> None:
        super().__init__()
        token_dim = int(token_dim)
        hidden_dim = int(hidden_dim)
        self.minimum = float(minimum)
        initial = float(initial)
        if token_dim <= 0 or hidden_dim <= 0:
            raise ValueError("GSS exposure-gate dimensions must be positive")
        if not 0.0 <= self.minimum < 1.0:
            raise ValueError("GSS exposure-gate minimum must be in [0,1)")
        if not self.minimum < initial < 1.0:
            raise ValueError(
                "GSS exposure-gate initial value must be strictly between "
                "minimum and 1"
            )
        self.token_norm = nn.LayerNorm(token_dim)
        self.delta_norm = nn.LayerNorm(token_dim)
        self.token_projection = nn.Linear(token_dim, hidden_dim)
        self.delta_projection = nn.Linear(token_dim, hidden_dim)
        self.mlp = nn.Sequential(
            nn.LayerNorm(2 * hidden_dim + 3),
            nn.Linear(2 * hidden_dim + 3, hidden_dim),
            nn.GELU(),
            nn.Dropout(float(dropout)),
            nn.Linear(hidden_dim, 1),
        )
        output = self.mlp[-1]
        assert isinstance(output, nn.Linear)
        # Parameterize attenuation rather than an unconstrained multiplier:
        # g = 1 - (1-minimum)*sigmoid(score).  Starting near one reproduces the
        # trained G1 probe closely but still gives the scalar head a gradient.
        initial_suppression = (1.0 - initial) / (1.0 - self.minimum)
        nn.init.zeros_(output.weight)
        nn.init.constant_(
            output.bias,
            math.log(initial_suppression / (1.0 - initial_suppression)),
        )

    def forward(
        self,
        token: torch.Tensor,
        delta: torch.Tensor,
        memory_mask: torch.Tensor,
    ) -> torch.Tensor:
        if token.ndim != 3 or delta.shape != token.shape:
            raise ValueError("GSS exposure gate expects equal [N,K,D] states")
        if memory_mask.shape != token.shape[:2]:
            raise ValueError("GSS exposure-gate memory mask shape mismatch")
        memory = memory_mask.bool()
        positions = torch.arange(
            token.shape[1], device=token.device, dtype=torch.float32,
        ).view(1, -1)
        prefix_density = memory.cumsum(dim=1).float() / (positions + 1.0)
        delta_rms = torch.sqrt(
            delta.float().square().mean(dim=-1).clamp(min=1.0e-12)
        )
        token_feature = self.token_projection(self.token_norm(token))
        delta_feature = self.delta_projection(self.delta_norm(delta))
        scalars = torch.stack([
            memory.float(),
            prefix_density,
            torch.log1p(delta_rms),
        ], dim=-1).to(token_feature.dtype)
        score = self.mlp(torch.cat([
            token_feature, delta_feature, scalars,
        ], dim=-1)).squeeze(-1).float()
        return 1.0 - (1.0 - self.minimum) * torch.sigmoid(score)


class GSSStrengthRouter(nn.Module):
    """Route every UOP among base, conservative, and full GSS gap anchors.

    Unlike :class:`GSSExposureGate`, this module does not receive the current
    memory-event bit and does not multiply a hidden residual.  It observes a
    strict-prefix memory density and the frozen/stop-gradient timing response
    of three already evaluated anchors.  The final model mixes positive gaps,
    so the router action and its counterfactual supervision have identical
    semantics even though the timing head is nonlinear.
    """

    ANCHORS: Tuple[float, float, float] = (0.0, 0.25, 1.0)

    def __init__(
        self,
        token_dim: int,
        hidden_dim: int = 64,
        initial_weights: Tuple[float, float, float] = (0.05, 0.90, 0.05),
        dropout: float = 0.0,
        exposure_dim: int = 0,
    ) -> None:
        super().__init__()
        token_dim = int(token_dim)
        hidden_dim = int(hidden_dim)
        self.exposure_dim = max(0, int(exposure_dim))
        if token_dim <= 0 or hidden_dim <= 0:
            raise ValueError("GSS strength-router dimensions must be positive")
        if len(initial_weights) != len(self.ANCHORS):
            raise ValueError("GSS strength-router requires three initial weights")
        initial = tuple(float(value) for value in initial_weights)
        if any(not math.isfinite(value) or value <= 0.0 for value in initial):
            raise ValueError("GSS strength-router initial weights must be positive")
        total = sum(initial)
        initial = tuple(value / total for value in initial)
        self.token_norm = nn.LayerNorm(token_dim)
        self.delta_norm = nn.LayerNorm(token_dim)
        self.token_projection = nn.Linear(token_dim, hidden_dim)
        self.delta_projection = nn.Linear(token_dim, hidden_dim)
        # density, delta RMS, two signed sensitivities, their magnitudes, and
        # two strict-prefix mean sensitivities.  No current-memory scalar is
        # present, by construction.
        scalar_dim = 8 + self.exposure_dim
        self.mlp = nn.Sequential(
            nn.LayerNorm(2 * hidden_dim + scalar_dim),
            nn.Linear(2 * hidden_dim + scalar_dim, hidden_dim),
            nn.GELU(),
            nn.Dropout(float(dropout)),
            nn.Linear(hidden_dim, len(self.ANCHORS)),
        )
        output = self.mlp[-1]
        assert isinstance(output, nn.Linear)
        nn.init.zeros_(output.weight)
        with torch.no_grad():
            output.bias.copy_(
                torch.tensor(
                    [math.log(value) for value in initial],
                    dtype=output.bias.dtype,
                )
            )

    def forward(
        self,
        token: torch.Tensor,
        delta: torch.Tensor,
        memory_mask: torch.Tensor,
        anchor_gaps: torch.Tensor,
        exposure_features: Optional[torch.Tensor] = None,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        if token.ndim != 3 or delta.shape != token.shape:
            raise ValueError("GSS strength router expects equal [N,K,D] states")
        if memory_mask.shape != token.shape[:2]:
            raise ValueError("GSS strength-router memory mask shape mismatch")
        if anchor_gaps.shape != (*token.shape[:2], len(self.ANCHORS)):
            raise ValueError("GSS strength-router anchor gap shape mismatch")
        if self.exposure_dim:
            if exposure_features is None or exposure_features.shape != (
                *token.shape[:2], self.exposure_dim,
            ):
                raise ValueError(
                    "GSS strength-router exposure feature shape mismatch"
                )
        elif exposure_features is not None:
            raise ValueError("legacy GSS strength router received exposure features")
        memory = memory_mask.bool()
        positions = torch.arange(
            token.shape[1], device=token.device, dtype=torch.float32,
        ).view(1, -1)
        strict_count = memory.cumsum(dim=1).float() - memory.float()
        strict_denominator = positions.clamp(min=1.0)
        prefix_density = strict_count / strict_denominator
        prefix_density = torch.where(
            positions > 0.0, prefix_density, torch.zeros_like(prefix_density),
        )
        delta_rms = torch.sqrt(
            delta.float().square().mean(dim=-1).clamp(min=1.0e-12)
        )
        log_gap = torch.log1p(anchor_gaps.float().clamp(min=0.0))
        sensitivity_025 = log_gap[..., 1] - log_gap[..., 0]
        sensitivity_1 = log_gap[..., 2] - log_gap[..., 1]

        def strict_prefix_mean(values: torch.Tensor) -> torch.Tensor:
            prefix = values.cumsum(dim=1) - values
            mean = prefix / strict_denominator
            return torch.where(
                positions > 0.0, mean, torch.zeros_like(mean),
            )

        token_feature = self.token_projection(self.token_norm(token))
        delta_feature = self.delta_projection(self.delta_norm(delta))
        scalars = torch.stack([
            prefix_density,
            torch.log1p(delta_rms),
            sensitivity_025,
            sensitivity_025.abs(),
            sensitivity_1,
            sensitivity_1.abs(),
            strict_prefix_mean(sensitivity_025),
            strict_prefix_mean(sensitivity_1),
        ], dim=-1).to(token_feature.dtype)
        if exposure_features is not None:
            scalars = torch.cat((
                scalars,
                exposure_features.to(token_feature.dtype),
            ), dim=-1)
        logits = self.mlp(torch.cat([
            token_feature, delta_feature, scalars,
        ], dim=-1)).float()
        return logits, torch.softmax(logits, dim=-1)
