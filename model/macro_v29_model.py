"""Macro-native timing model for the LLMSim v29 redesign."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence

import torch
from torch import nn
import torch.nn.functional as F
from torch.utils.checkpoint import checkpoint

from train.macro_v29_dataset import V29_FIELD_SIZES


@dataclass
class MacroV29Config:
    d_llm: int
    d_model: int = 384
    d_field: int = 8
    n_heads: int = 8
    dropout: float = 0.0
    cross_ffn_multiplier: int = 4
    cross_target_block: int = 0
    cross_gate_init: float = -2.0
    backbone_core_chunk_size: int = 0
    backbone_chunk_checkpoint: bool = False
    core_mixer_mode: str = "macro_cross_attention"
    dynamic_field_sizes: Sequence[int] = (8, 8, 8, 8, 8, 8, 8, 8)
    chunk_summary_width: int = 38
    relation_width: int = 22
    state_width: int = 5
    uarch_width: int = 29
    horizons: Sequence[float] = (
        16.0, 32.0, 64.0, 128.0, 256.0, 512.0, 1024.0,
    )
    commit_temperature: float = 8.0
    freeze_backbone: bool = False
    semantic_mode: str = "fusion"
    semantic_input_mode: str = "native_token"
    semantic_dim: int | None = None
    anchor_policy: str = "mean_native_input_embedding"
    semantic_gate_init: float = -3.0
    online_backbone_type: str = "qwen_lora"
    semantic_source: str = "real_cache"
    online_transformer_layers: int = 5
    online_transformer_heads: int = 8
    online_transformer_ffn_multiplier: int = 4


class StructuredMacroEncoder(nn.Module):
    """Encode structured per-UOP fields without turning them into text."""

    def __init__(self, config: MacroV29Config):
        super().__init__()
        self.field_sizes = tuple(int(value) for value in V29_FIELD_SIZES)
        self.field_embeddings = nn.ModuleList([
            nn.Embedding(size + 1, config.d_field, padding_idx=size)
            for size in self.field_sizes
        ])
        self.access_embedding = nn.Embedding(256, config.d_field)
        self.semantic_embedding = nn.Embedding(256, config.d_field)
        self.dynamic_sizes = tuple(int(value) for value in config.dynamic_field_sizes)
        self.dynamic_embeddings = nn.ModuleList([
            nn.Embedding(size + 1, config.d_field, padding_idx=size)
            for size in self.dynamic_sizes
        ])
        input_width = (
            len(self.field_sizes) + len(self.dynamic_sizes) + 2
        ) * config.d_field
        self.uop_projection = nn.Sequential(
            nn.LayerNorm(input_width),
            nn.Linear(input_width, config.d_model),
            nn.GELU(),
            nn.Linear(config.d_model, config.d_model),
        )
        self.uop_count_projection = nn.Sequential(
            nn.Linear(1, config.d_model),
            nn.GELU(),
            nn.Linear(config.d_model, config.d_model),
        )
        self.output_norm = nn.LayerNorm(config.d_model)

    def forward(
        self,
        uop_fields: torch.Tensor,
        uop_valid_mask: torch.Tensor,
        uop_to_macro: torch.Tensor,
        uop_access: torch.Tensor,
        uop_semantic_flags: torch.Tensor,
        uop_count: torch.Tensor,
        dynamic_uop_fields: torch.Tensor,
    ) -> torch.Tensor:
        if uop_fields.ndim != 3 or uop_fields.shape[-1] != len(self.field_sizes):
            raise ValueError("uop_fields must be ragged-padded [R,N_uop,26]")
        if uop_valid_mask.shape != uop_fields.shape[:-1]:
            raise ValueError("uop_valid_mask shape mismatch")
        if uop_to_macro.shape != uop_fields.shape[:-1]:
            raise ValueError("uop_to_macro shape mismatch")
        pieces = []
        for index, (embedding, size) in enumerate(zip(
            self.field_embeddings, self.field_sizes,
        )):
            values = uop_fields[..., index].long()
            if torch.any(values < 0) or torch.any(values > size):
                raise ValueError(f"field {index} is outside [0,{size}]")
            pieces.append(embedding(values))
        pieces.append(self.access_embedding(uop_access.long()))
        pieces.append(self.semantic_embedding(uop_semantic_flags.long()))
        if dynamic_uop_fields.shape[:-1] != uop_fields.shape[:-1]:
            raise ValueError("dynamic_uop_fields shape mismatch")
        if dynamic_uop_fields.shape[-1] != len(self.dynamic_sizes):
            raise ValueError("dynamic field count mismatch")
        for index, (embedding, size) in enumerate(zip(
            self.dynamic_embeddings, self.dynamic_sizes,
        )):
            values = dynamic_uop_fields[..., index].long()
            if torch.any(values < 0) or torch.any(values > size):
                raise ValueError(f"dynamic field {index} is outside [0,{size}]")
            pieces.append(embedding(values))
        encoded_uop = self.uop_projection(torch.cat(pieces, dim=-1))
        if uop_count.ndim != 2 or uop_count.shape[0] != uop_fields.shape[0]:
            raise ValueError("uop_count must be [R,M]")
        macros = int(uop_count.shape[1])
        valid = uop_valid_mask.bool()
        if torch.any(valid & ((uop_to_macro < 0) | (uop_to_macro >= macros))):
            raise ValueError("valid UOP has an invalid macro segment index")
        if torch.any((~valid) & (uop_to_macro != -1)):
            raise ValueError("padded UOP segment index must be -1")
        safe_index = uop_to_macro.clamp(0, macros - 1).long()
        mask = valid.to(encoded_uop.dtype).unsqueeze(-1)
        summed = torch.zeros(
            uop_fields.shape[0], macros, encoded_uop.shape[-1],
            dtype=encoded_uop.dtype, device=encoded_uop.device,
        )
        summed.scatter_add_(
            1,
            safe_index.unsqueeze(-1).expand(-1, -1, encoded_uop.shape[-1]),
            encoded_uop * mask,
        )
        segment_count = torch.zeros(
            uop_fields.shape[0], macros, 1,
            dtype=encoded_uop.dtype, device=encoded_uop.device,
        )
        segment_count.scatter_add_(1, safe_index.unsqueeze(-1), mask)
        counts = uop_count.long()
        if torch.any(counts < 0):
            raise ValueError("uop_count must be non-negative")
        if not torch.equal(segment_count.squeeze(-1).long(), counts):
            raise ValueError("ragged segment counts do not match uop_count")
        pooled = summed / segment_count.clamp_min(1.0)
        count_feature = torch.log1p(counts.float()).unsqueeze(-1) / 4.0
        return self.output_norm(
            pooled + self.uop_count_projection(count_feature)
        )


class PermutationEquivariantCoreMixer(nn.Module):
    """Legacy one-summary-per-core mixer retained for old checkpoints."""

    def __init__(
        self,
        d_model: int,
        n_heads: int,
        dropout: float,
    ) -> None:
        super().__init__()
        self.attention = nn.MultiheadAttention(
            d_model,
            n_heads,
            dropout=dropout,
            batch_first=True,
        )
        self.norm1 = nn.LayerNorm(d_model)
        self.ffn = nn.Sequential(
            nn.Linear(d_model, 4 * d_model),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(4 * d_model, d_model),
        )
        self.norm2 = nn.LayerNorm(d_model)

    def forward(
        self,
        core_state: torch.Tensor,
        sample_ptr: torch.Tensor,
    ) -> torch.Tensor:
        if core_state.ndim != 2:
            raise ValueError("legacy core state must be [R,D]")
        pointers = [int(value) for value in sample_ptr.detach().cpu().tolist()]
        if not pointers or pointers[0] != 0 or pointers[-1] != core_state.shape[0]:
            raise ValueError("sample_ptr does not cover all core rows")
        if any(end <= begin for begin, end in zip(pointers, pointers[1:])):
            raise ValueError("sample_ptr groups must be non-empty")
        outputs: List[torch.Tensor] = []
        for begin, end in zip(pointers, pointers[1:]):
            current = core_state[begin:end].unsqueeze(0)
            attended, _weights = self.attention(
                current,
                current,
                current,
                need_weights=False,
            )
            current = self.norm1(current + attended)
            current = self.norm2(current + self.ffn(current))
            outputs.append(current.squeeze(0))
        return torch.cat(outputs, dim=0)


class MacroCrossCoreMixer(nn.Module):
    """TCSim-style cross-core attention that preserves every macro state.

    The online Qwen has already modeled the 256-position sequence within each
    core.  This block therefore performs only the missing cross-core part:
    every valid macro queries all valid macros on every *other* core in the
    same scheduler context.  Contexts with the same active-core count are
    bucketed, and ``target_block`` bounds temporary K/V expansion without
    changing the attention math.
    """

    def __init__(
        self,
        d_model: int,
        n_heads: int,
        dropout: float,
        *,
        relation_width: int,
        state_width: int,
        ffn_multiplier: int = 4,
        target_block: int = 0,
        gate_init: float = -2.0,
    ) -> None:
        super().__init__()
        if int(d_model) % int(n_heads) != 0:
            raise ValueError("d_model must be divisible by n_heads")
        if int(ffn_multiplier) <= 0:
            raise ValueError("cross ffn multiplier must be positive")
        if int(target_block) < 0:
            raise ValueError("cross target block must be non-negative")
        self.d_model = int(d_model)
        self.n_heads = int(n_heads)
        self.head_dim = self.d_model // self.n_heads
        self.dropout = float(dropout)
        self.target_block = int(target_block)
        self.attn_norm = nn.LayerNorm(self.d_model)
        self.r_proj = nn.Linear(self.d_model, self.d_model, bias=False)
        self.k_proj = nn.Linear(self.d_model, self.d_model, bias=False)
        self.v_proj = nn.Linear(self.d_model, self.d_model, bias=False)
        self.out_proj = nn.Linear(self.d_model, self.d_model, bias=False)
        gate_width = int(relation_width) + int(state_width)
        gate_hidden = max(64, self.d_model // 4)
        self.cross_gate = nn.Sequential(
            nn.LayerNorm(gate_width),
            nn.Linear(gate_width, gate_hidden),
            nn.GELU(),
            nn.Linear(gate_hidden, self.d_model),
        )
        nn.init.zeros_(self.cross_gate[-1].weight)
        nn.init.constant_(self.cross_gate[-1].bias, float(gate_init))
        self.attn_dropout = nn.Dropout(self.dropout)
        self.ffn_norm = nn.LayerNorm(self.d_model)
        hidden = int(ffn_multiplier) * self.d_model
        self.ffn = nn.Sequential(
            nn.Linear(self.d_model, hidden),
            nn.GELU(),
            nn.Dropout(self.dropout),
            nn.Linear(hidden, self.d_model),
            nn.Dropout(self.dropout),
        )

    def _split_heads(self, value: torch.Tensor) -> torch.Tensor:
        batch, length, width = value.shape
        if int(width) != self.d_model:
            raise ValueError(
                f"cross mixer expected width={self.d_model}, got {width}"
            )
        return value.view(
            batch, length, self.n_heads, self.head_dim,
        ).transpose(1, 2)

    def _merge_heads(self, value: torch.Tensor) -> torch.Tensor:
        batch, heads, length, width = value.shape
        return value.transpose(1, 2).contiguous().view(
            batch, length, heads * width,
        )

    def _attend(
        self,
        query: torch.Tensor,
        key: torch.Tensor,
        value: torch.Tensor,
        key_mask: torch.Tensor,
    ) -> torch.Tensor:
        attended = F.scaled_dot_product_attention(
            self._split_heads(query),
            self._split_heads(key),
            self._split_heads(value),
            attn_mask=key_mask.bool()[:, None, None, :],
            dropout_p=self.dropout if self.training else 0.0,
            is_causal=False,
        )
        return self._merge_heads(attended)

    @staticmethod
    def _sample_ranges(
        sample_ptr: torch.Tensor,
        rows: int,
    ) -> List[tuple[int, int]]:
        if sample_ptr.ndim != 1 or int(sample_ptr.numel()) < 2:
            raise ValueError(
                "sample_ptr must be a 1-D prefix with at least two entries"
            )
        ptr = [int(value) for value in sample_ptr.detach().cpu().tolist()]
        if ptr[0] != 0 or ptr[-1] != int(rows):
            raise ValueError(
                f"sample_ptr must span [0,{rows}], got [{ptr[0]},{ptr[-1]}]"
            )
        if any(end <= begin for begin, end in zip(ptr, ptr[1:])):
            raise ValueError("sample_ptr contains an empty context")
        return list(zip(ptr, ptr[1:]))

    def _cross_attention(
        self,
        query: torch.Tensor,
        key: torch.Tensor,
        value: torch.Tensor,
        valid_macro_mask: torch.Tensor,
        sample_ptr: torch.Tensor,
    ) -> torch.Tensor:
        rows, macros, width = query.shape
        cross = torch.zeros_like(query)
        buckets: Dict[int, List[int]] = {}
        for begin, end in self._sample_ranges(sample_ptr, rows):
            buckets.setdefault(end - begin, []).append(begin)

        for core_count, starts_cpu in buckets.items():
            starts = torch.tensor(
                starts_cpu, dtype=torch.long, device=query.device,
            )
            offsets = torch.arange(
                core_count, dtype=torch.long, device=query.device,
            )
            row_index = (starts[:, None] + offsets[None, :]).reshape(-1)
            contexts = len(starts_cpu)
            query_group = query.index_select(0, row_index).reshape(
                contexts, core_count, macros, width,
            )
            key_group = key.index_select(0, row_index).reshape(
                contexts, core_count, macros, width,
            )
            value_group = value.index_select(0, row_index).reshape_as(
                key_group
            )
            mask_group = valid_macro_mask.index_select(
                0, row_index,
            ).reshape(contexts, core_count, macros)
            if core_count == 1:
                # There is no other core to attend to, so the mathematical
                # result is exactly zero.  Keep a zero-valued autograd edge to
                # every Q/K/V projection: in a mixed-core DDP run one rank can
                # receive c1 while another receives c32, and DDP requires the
                # same parameter hooks to fire on every rank.
                cross_group = (
                    query_group + key_group + value_group
                ) * 0.0
            else:
                core_ids = torch.arange(core_count, device=query.device)
                other_core_ids = core_ids.repeat(core_count, 1)[
                    ~torch.eye(
                        core_count, dtype=torch.bool, device=query.device,
                    )
                ].reshape(core_count, core_count - 1)
                parts: List[torch.Tensor] = []
                target_block = self.target_block or core_count
                for target_begin in range(0, core_count, target_block):
                    target_end = min(core_count, target_begin + target_block)
                    targets = target_end - target_begin
                    other = other_core_ids[target_begin:target_end]
                    key_other = key_group[:, other].reshape(
                        contexts * targets,
                        (core_count - 1) * macros,
                        width,
                    )
                    value_other = value_group[:, other].reshape_as(key_other)
                    mask_other = mask_group[:, other].reshape(
                        contexts * targets,
                        (core_count - 1) * macros,
                    )
                    query_target = query_group[
                        :, target_begin:target_end,
                    ].reshape(contexts * targets, macros, width)
                    parts.append(self._attend(
                        query_target, key_other, value_other, mask_other,
                    ).reshape(contexts, targets, macros, width))
                cross_group = torch.cat(parts, dim=1)
            cross = cross.index_copy(
                0, row_index, cross_group.reshape(-1, macros, width),
            )
        return cross

    def forward(
        self,
        macro_state: torch.Tensor,
        valid_macro_mask: torch.Tensor,
        sample_ptr: torch.Tensor,
        relation_features: torch.Tensor,
        state_features: torch.Tensor,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        if macro_state.ndim != 3:
            raise ValueError("macro_state must be [R,M,D]")
        if valid_macro_mask.shape != macro_state.shape[:2]:
            raise ValueError("valid macro mask does not match macro_state")
        if relation_features.shape[0] != macro_state.shape[0]:
            raise ValueError("relation features do not cover all core rows")
        if state_features.shape[0] != macro_state.shape[0]:
            raise ValueError("state features do not cover all core rows")
        normalized = self.attn_norm(macro_state)
        query = self.r_proj(normalized)
        key = self.k_proj(normalized)
        value = self.v_proj(normalized)
        cross_context = self._cross_attention(
            query, key, value, valid_macro_mask.bool(), sample_ptr,
        )
        gate_input = torch.cat([
            relation_features.float(), state_features.float(),
        ], dim=-1)
        gate_parameter = next(self.cross_gate.parameters())
        gate = torch.sigmoid(self.cross_gate(
            gate_input.to(dtype=gate_parameter.dtype)
        )).unsqueeze(1)
        mixed = macro_state + self.attn_dropout(
            gate.to(cross_context.dtype) * self.out_proj(cross_context)
        )
        mixed = mixed + self.ffn(self.ffn_norm(mixed))
        valid = valid_macro_mask.unsqueeze(-1).to(mixed.dtype)
        return mixed * valid, cross_context * valid


class CausalMacroTransformerBlock(nn.Module):
    """Pre-norm causal self-attention over one core's macro sequence."""

    def __init__(
        self,
        d_model: int,
        n_heads: int,
        ffn_multiplier: int,
        dropout: float,
    ) -> None:
        super().__init__()
        if int(d_model) % int(n_heads) != 0:
            raise ValueError("online Transformer width must divide its head count")
        if int(ffn_multiplier) <= 0:
            raise ValueError("online Transformer FFN multiplier must be positive")
        self.d_model = int(d_model)
        self.n_heads = int(n_heads)
        self.head_dim = self.d_model // self.n_heads
        self.dropout = float(dropout)
        self.attn_norm = nn.LayerNorm(self.d_model)
        self.q_proj = nn.Linear(self.d_model, self.d_model, bias=False)
        self.k_proj = nn.Linear(self.d_model, self.d_model, bias=False)
        self.v_proj = nn.Linear(self.d_model, self.d_model, bias=False)
        self.out_proj = nn.Linear(self.d_model, self.d_model, bias=False)
        self.attn_dropout = nn.Dropout(self.dropout)
        self.ffn_norm = nn.LayerNorm(self.d_model)
        hidden = int(ffn_multiplier) * self.d_model
        self.ffn = nn.Sequential(
            nn.Linear(self.d_model, hidden),
            nn.GELU(),
            nn.Dropout(self.dropout),
            nn.Linear(hidden, self.d_model),
            nn.Dropout(self.dropout),
        )

    def _split_heads(self, value: torch.Tensor) -> torch.Tensor:
        batch, length, _width = value.shape
        return value.view(
            batch, length, self.n_heads, self.head_dim,
        ).transpose(1, 2)

    def _merge_heads(self, value: torch.Tensor) -> torch.Tensor:
        batch, heads, length, width = value.shape
        return value.transpose(1, 2).contiguous().view(
            batch, length, heads * width,
        )

    def forward(
        self,
        macro_state: torch.Tensor,
        valid_macro_mask: torch.Tensor,
    ) -> torch.Tensor:
        normalized = self.attn_norm(macro_state)
        query = self._split_heads(self.q_proj(normalized))
        key = self._split_heads(self.k_proj(normalized))
        value = self._split_heads(self.v_proj(normalized))
        length = int(macro_state.shape[1])
        causal = torch.ones(
            length, length, dtype=torch.bool, device=macro_state.device,
        ).tril()
        attention_mask = (
            causal[None, None, :, :]
            & valid_macro_mask.bool()[:, None, None, :]
        )
        attended = F.scaled_dot_product_attention(
            query,
            key,
            value,
            attn_mask=attention_mask,
            dropout_p=self.dropout if self.training else 0.0,
            is_causal=False,
        )
        mixed = macro_state + self.attn_dropout(
            self.out_proj(self._merge_heads(attended))
        )
        mixed = mixed + self.ffn(self.ffn_norm(mixed))
        return mixed * valid_macro_mask.unsqueeze(-1).to(mixed.dtype)


class CausalMacroTransformer(nn.Module):
    """Capacity-matched non-LLM online backbone used by A/B/E controls."""

    def __init__(
        self,
        input_dim: int,
        d_model: int,
        n_heads: int,
        n_layers: int,
        ffn_multiplier: int,
        dropout: float,
        *,
        max_macros: int = 256,
    ) -> None:
        super().__init__()
        if int(n_layers) <= 0:
            raise ValueError("online Transformer layer count must be positive")
        self.input_norm = nn.LayerNorm(int(input_dim))
        self.input_projection = nn.Linear(int(input_dim), int(d_model))
        self.position_embedding = nn.Embedding(int(max_macros), int(d_model))
        self.layers = nn.ModuleList([
            CausalMacroTransformerBlock(
                int(d_model), int(n_heads), int(ffn_multiplier), float(dropout),
            )
            for _ in range(int(n_layers))
        ])
        self.output_norm = nn.LayerNorm(int(d_model))

    def forward(
        self,
        soft_macro: torch.Tensor,
        valid_macro_mask: torch.Tensor,
    ) -> torch.Tensor:
        if soft_macro.ndim != 3:
            raise ValueError("soft macro input must be [R,M,D]")
        if tuple(valid_macro_mask.shape) != tuple(soft_macro.shape[:2]):
            raise ValueError("soft macro valid mask shape mismatch")
        macros = int(soft_macro.shape[1])
        if macros > int(self.position_embedding.num_embeddings):
            raise ValueError("soft macro sequence exceeds position table")
        parameter = self.input_projection.weight
        state = self.input_projection(
            self.input_norm(soft_macro.to(dtype=parameter.dtype))
        )
        positions = self.position_embedding(
            torch.arange(macros, device=soft_macro.device)
        ).unsqueeze(0)
        state = state + positions.to(dtype=state.dtype)
        state = state * valid_macro_mask.unsqueeze(-1).to(state.dtype)
        for layer in self.layers:
            state = layer(state, valid_macro_mask)
        state = self.output_norm(state)
        return state * valid_macro_mask.unsqueeze(-1).to(state.dtype)


def _span_pool(
    hidden: torch.Tensor,
    starts: torch.Tensor,
    ends: torch.Tensor,
    valid_macro_mask: torch.Tensor,
) -> torch.Tensor:
    """Mean pool half-open token spans with an O(R*L) prefix sum."""

    if hidden.ndim != 3:
        raise ValueError("LLM hidden must be [R,L,D]")
    if starts.shape != ends.shape or starts.shape != valid_macro_mask.shape:
        raise ValueError("macro span shapes do not match")
    length = hidden.shape[1]
    valid = valid_macro_mask.bool()
    if torch.any(valid & ((starts < 0) | (ends <= starts) | (ends > length))):
        raise ValueError("invalid token span for a valid macro")
    safe_start = starts.clamp(0, length).long()
    safe_end = ends.clamp(0, length).long()
    accumulation = hidden.float()
    prefix = torch.cat([
        torch.zeros(
            hidden.shape[0], 1, hidden.shape[2],
            dtype=accumulation.dtype, device=hidden.device,
        ),
        accumulation.cumsum(dim=1),
    ], dim=1)
    gather_start = safe_start.unsqueeze(-1).expand(-1, -1, hidden.shape[-1])
    gather_end = safe_end.unsqueeze(-1).expand(-1, -1, hidden.shape[-1])
    summed = torch.gather(prefix, 1, gather_end) - torch.gather(
        prefix, 1, gather_start,
    )
    count = (safe_end - safe_start).clamp_min(1).unsqueeze(-1).to(hidden.dtype)
    return (summed / count) * valid.unsqueeze(-1).to(hidden.dtype)


class MacroV29TimingModel(nn.Module):
    """Coding-LLM local backbone plus structured side and macro timing heads."""

    def __init__(self, backbone: nn.Module | None, config: MacroV29Config):
        super().__init__()
        if config.semantic_mode not in {"fusion", "side_only", "llm_only"}:
            raise ValueError(
                "semantic_mode must be fusion, side_only, or llm_only"
            )
        if config.semantic_input_mode not in {
            "native_token", "cached_macro_soft_token",
            "learned_null_macro_token",
        }:
            raise ValueError(
                "semantic_input_mode must be native_token, "
                "cached_macro_soft_token, or learned_null_macro_token"
            )
        if config.online_backbone_type not in {
            "qwen_lora", "causal_transformer",
        }:
            raise ValueError(
                "online_backbone_type must be qwen_lora or causal_transformer"
            )
        if config.semantic_source not in {"real_cache", "learned_null"}:
            raise ValueError(
                "semantic_source must be real_cache or learned_null"
            )
        if (
            config.semantic_source == "learned_null"
            and config.semantic_input_mode != "learned_null_macro_token"
        ):
            raise ValueError(
                "learned_null source requires learned_null_macro_token input"
            )
        if (
            config.semantic_input_mode == "learned_null_macro_token"
            and config.semantic_source != "learned_null"
        ):
            raise ValueError(
                "learned_null_macro_token input requires learned_null source"
            )
        if (
            config.online_backbone_type == "causal_transformer"
            and config.semantic_input_mode == "native_token"
        ):
            raise ValueError(
                "causal_transformer requires one macro soft input position"
            )
        if (
            config.online_backbone_type == "qwen_lora"
            and config.semantic_source == "learned_null"
        ):
            raise ValueError("A/B/E learned-null control uses causal_transformer")
        if config.core_mixer_mode not in {
            "summary", "macro_cross_attention",
        }:
            raise ValueError(
                "core_mixer_mode must be summary or macro_cross_attention"
            )
        if (
            config.semantic_input_mode == "cached_macro_soft_token"
            and (config.semantic_dim is None or int(config.semantic_dim) <= 0)
        ):
            raise ValueError("cached macro soft tokens require semantic_dim")
        if (
            config.semantic_input_mode == "learned_null_macro_token"
            and (config.semantic_dim is None or int(config.semantic_dim) <= 0)
        ):
            raise ValueError("learned-null macro tokens require semantic_dim")
        if config.online_backbone_type == "qwen_lora" and backbone is None:
            raise ValueError("qwen_lora requires a Qwen-compatible backbone")
        if config.online_backbone_type == "causal_transformer" and backbone is not None:
            raise ValueError("causal_transformer must not retain a Qwen backbone")
        self.backbone = backbone
        self.config = config
        if config.freeze_backbone and self.backbone is not None:
            for parameter in self.backbone.parameters():
                parameter.requires_grad = False
        if config.online_backbone_type == "qwen_lora":
            self.asm_projection: nn.Module = nn.Sequential(
                nn.LayerNorm(config.d_llm),
                nn.Linear(config.d_llm, config.d_model),
            )
            self.online_transformer: CausalMacroTransformer | None = None
        else:
            self.asm_projection = nn.Identity()
            self.online_transformer = CausalMacroTransformer(
                config.d_llm,
                config.d_model,
                config.online_transformer_heads,
                config.online_transformer_layers,
                config.online_transformer_ffn_multiplier,
                config.dropout,
            )
        if config.semantic_input_mode in {
            "cached_macro_soft_token", "learned_null_macro_token",
        }:
            self.semantic_adapter: nn.Module | None = nn.Sequential(
                nn.LayerNorm(int(config.semantic_dim)),
                nn.Linear(int(config.semantic_dim), config.d_llm),
                nn.GELU(),
                nn.Linear(config.d_llm, config.d_llm),
            )
            self.semantic_input_norm: nn.Module | None = nn.LayerNorm(
                config.d_llm
            )
            self.semantic_gate: nn.Parameter | None = nn.Parameter(
                torch.tensor(float(config.semantic_gate_init))
            )
            if config.semantic_source == "learned_null":
                self.null_semantic: nn.Parameter | None = nn.Parameter(
                    torch.linspace(
                        -0.02, 0.02, steps=int(config.semantic_dim),
                    )
                )
                self.null_anchor: nn.Parameter | None = nn.Parameter(
                    torch.zeros(config.d_llm)
                )
            else:
                self.register_parameter("null_semantic", None)
                self.register_parameter("null_anchor", None)
        else:
            self.semantic_adapter = None
            self.semantic_input_norm = None
            self.register_parameter("semantic_gate", None)
            self.register_parameter("null_semantic", None)
            self.register_parameter("null_anchor", None)
        self.numeric_encoder = StructuredMacroEncoder(config)
        side_width = (
            int(config.chunk_summary_width)
            + int(config.relation_width)
            + int(config.state_width)
            + int(config.uarch_width)
        )
        self.side_projection = nn.Sequential(
            nn.LayerNorm(side_width),
            nn.Linear(side_width, config.d_model),
            nn.GELU(),
            nn.Linear(config.d_model, config.d_model),
        )
        self.position_embedding = nn.Embedding(256, config.d_model)
        self.fusion_norm = nn.LayerNorm(config.d_model)
        if config.core_mixer_mode == "summary":
            self.core_mixer: nn.Module = PermutationEquivariantCoreMixer(
                config.d_model,
                config.n_heads,
                config.dropout,
            )
            self.core_gate: nn.Parameter | None = nn.Parameter(
                torch.tensor(-2.0)
            )
        else:
            self.core_mixer = MacroCrossCoreMixer(
                config.d_model,
                config.n_heads,
                config.dropout,
                relation_width=config.relation_width,
                state_width=config.state_width,
                ffn_multiplier=config.cross_ffn_multiplier,
                target_block=config.cross_target_block,
                gate_init=config.cross_gate_init,
            )
            self.register_parameter("core_gate", None)
        self.gap_head = nn.Sequential(
            nn.Linear(config.d_model, config.d_model),
            nn.GELU(),
            nn.Linear(config.d_model, 1),
        )
        self.branch_head = nn.Sequential(
            nn.Linear(config.d_model, config.d_model // 2),
            nn.GELU(),
            nn.Linear(config.d_model // 2, 1),
        )
        horizons = torch.tensor(tuple(config.horizons), dtype=torch.float32)
        if horizons.ndim != 1 or not torch.all(horizons > 0):
            raise ValueError("horizons must be positive")
        self.register_buffer("horizons", horizons, persistent=True)
        # Evaluation-only diagnostics are opt-in so training retains its
        # original graph and runtime.  The rollout entry point enables this
        # after strict checkpoint loading for post-hoc semantic interventions.
        self.collect_activation_diagnostics = False

    @staticmethod
    def _masked_activation_statistics(
        value: torch.Tensor,
        valid: torch.Tensor,
    ) -> Dict[str, torch.Tensor]:
        if value.ndim < 2 or tuple(value.shape[:2]) != tuple(valid.shape):
            raise ValueError(
                f"activation shape {tuple(value.shape)} does not begin with "
                f"valid shape {tuple(valid.shape)}"
            )
        mask = valid
        for _ in range(value.ndim - 2):
            mask = mask.unsqueeze(-1)
        expanded = mask.expand_as(value)
        selected = value.detach().float().masked_select(expanded)
        return {
            "sum_squares": selected.square().sum(dtype=torch.float64),
            "count": torch.tensor(
                int(selected.numel()), dtype=torch.int64, device=value.device,
            ),
        }

    def _backbone_hidden(
        self,
        input_ids: torch.Tensor,
        attention_mask: torch.Tensor,
        *,
        chunk_size: int = 256,
    ) -> torch.Tensor:
        if self.backbone is None:
            raise RuntimeError("native-token hidden path requires Qwen backbone")
        rows = int(input_ids.shape[0])
        if rows <= chunk_size:
            output = self.backbone(
                input_ids=input_ids,
                attention_mask=attention_mask,
                output_hidden_states=False,
                use_cache=False,
            )
            if getattr(output, "last_hidden_state", None) is not None:
                return output.last_hidden_state
            if getattr(output, "hidden_states", None) is not None:
                return output.hidden_states[-1]
            raise RuntimeError("backbone returned no hidden states")
        chunks: List[torch.Tensor] = []
        for start in range(0, rows, int(chunk_size)):
            end = min(start + int(chunk_size), rows)
            output = self.backbone(
                input_ids=input_ids[start:end],
                attention_mask=attention_mask[start:end],
                output_hidden_states=False,
                use_cache=False,
            )
            if getattr(output, "last_hidden_state", None) is not None:
                chunks.append(output.last_hidden_state)
            elif getattr(output, "hidden_states", None) is not None:
                chunks.append(output.hidden_states[-1])
            else:
                raise RuntimeError("backbone returned no hidden states")
        return torch.cat(chunks, dim=0)

    def _backbone_hidden_embeds(
        self,
        inputs_embeds: torch.Tensor,
        attention_mask: torch.Tensor,
    ) -> torch.Tensor:
        """One batched online-Qwen call over macro positions.

        There is deliberately no per-core loop here: rows are independent
        batch elements and are encoded together as ``[R,256,D_qwen]``.
        """

        if self.backbone is None:
            raise RuntimeError("soft-embed hidden path requires Qwen backbone")
        output = self.backbone(
            inputs_embeds=inputs_embeds,
            attention_mask=attention_mask,
            output_hidden_states=False,
            use_cache=False,
        )
        if getattr(output, "last_hidden_state", None) is not None:
            return output.last_hidden_state
        if getattr(output, "hidden_states", None) is not None:
            return output.hidden_states[-1]
        raise RuntimeError("backbone returned no hidden states")

    @staticmethod
    def _last_hidden(output: Any) -> torch.Tensor:
        if getattr(output, "last_hidden_state", None) is not None:
            return output.last_hidden_state
        if getattr(output, "hidden_states", None) is not None:
            return output.hidden_states[-1]
        raise RuntimeError("backbone returned no hidden states")

    def _backbone_macro_features(
        self,
        inputs_embeds: torch.Tensor,
        attention_mask: torch.Tensor,
    ) -> torch.Tensor:
        """Run exact Qwen row chunks and retain only projected macro states.

        Online-Qwen core rows are independent, so row chunking preserves the
        result.  Optional non-reentrant checkpointing recomputes a chunk in
        backward instead of retaining all of its internal Qwen activations;
        this keeps the full cross-core loss gradient into every LoRA adapter.
        """

        if self.backbone is None:
            raise RuntimeError("Qwen macro path has no backbone")
        rows = int(inputs_embeds.shape[0])
        configured = int(self.config.backbone_core_chunk_size)
        chunk_size = rows if configured <= 0 else min(configured, rows)
        use_checkpoint = bool(
            self.config.backbone_chunk_checkpoint
            and self.training
            and torch.is_grad_enabled()
        )
        projection_dtype = next(self.asm_projection.parameters()).dtype

        def forward_chunk(
            chunk_embeds: torch.Tensor,
            chunk_mask: torch.Tensor,
        ) -> torch.Tensor:
            output = self.backbone(
                inputs_embeds=chunk_embeds,
                attention_mask=chunk_mask,
                output_hidden_states=False,
                use_cache=False,
            )
            hidden = self._last_hidden(output)
            return self.asm_projection(hidden.to(dtype=projection_dtype))

        features: List[torch.Tensor] = []
        for begin in range(0, rows, chunk_size):
            end = min(rows, begin + chunk_size)
            chunk_embeds = inputs_embeds[begin:end]
            chunk_mask = attention_mask[begin:end]
            if use_checkpoint:
                features.append(checkpoint(
                    forward_chunk,
                    chunk_embeds,
                    chunk_mask,
                    use_reentrant=False,
                    preserve_rng_state=True,
                ))
            else:
                features.append(forward_chunk(chunk_embeds, chunk_mask))
        return torch.cat(features, dim=0)

    def _cached_soft_macro(
        self,
        batch: Mapping[str, torch.Tensor],
        valid: torch.Tensor,
    ) -> torch.Tensor:
        required = {"static_semantic", "static_anchor"}
        missing = required - set(batch)
        if missing:
            raise ValueError(f"cached semantic input lacks {sorted(missing)}")
        semantic = batch["static_semantic"]
        anchor = batch["static_anchor"]
        expected_semantic = (*valid.shape, int(self.config.semantic_dim))
        expected_anchor = (*valid.shape, int(self.config.d_llm))
        if tuple(semantic.shape) != expected_semantic:
            raise ValueError(
                f"static_semantic shape {tuple(semantic.shape)} != "
                f"{expected_semantic}"
            )
        if tuple(anchor.shape) != expected_anchor:
            raise ValueError(
                f"static_anchor shape {tuple(anchor.shape)} != "
                f"{expected_anchor}"
            )
        if self.semantic_adapter is None or self.semantic_input_norm is None:
            raise RuntimeError("semantic adapter was not constructed")
        adapter_parameter = next(self.semantic_adapter.parameters())
        semantic_projection = self.semantic_adapter(
            semantic.to(dtype=adapter_parameter.dtype)
        )
        soft_macro = self.semantic_input_norm(
            anchor.to(dtype=semantic_projection.dtype)
            + torch.sigmoid(self.semantic_gate) * semantic_projection
        )
        soft_macro = soft_macro * valid.unsqueeze(-1).to(soft_macro.dtype)
        if self.backbone is not None:
            try:
                input_embeddings = self.backbone.get_input_embeddings()
                backbone_dtype = input_embeddings.weight.dtype
            except (AttributeError, TypeError):
                backbone_dtype = next(self.backbone.parameters()).dtype
        elif self.online_transformer is not None:
            backbone_dtype = self.online_transformer.input_projection.weight.dtype
        else:  # pragma: no cover - constructor guarantees one online path
            raise RuntimeError("model has no online macro backbone")
        return soft_macro.to(dtype=backbone_dtype)

    def _learned_null_soft_macro(
        self,
        batch: Mapping[str, torch.Tensor],
        valid: torch.Tensor,
    ) -> torch.Tensor:
        marker = batch.get("null_semantic_marker")
        if marker is None or tuple(marker.shape) != tuple(valid.shape):
            raise ValueError(
                "learned-null input requires null_semantic_marker [R,256]"
            )
        if torch.any(marker != 0):
            raise ValueError("null_semantic_marker must contain only zeros")
        if (
            self.semantic_adapter is None
            or self.semantic_input_norm is None
            or self.null_semantic is None
            or self.null_anchor is None
        ):
            raise RuntimeError("learned-null parameters were not constructed")
        rows, macros = valid.shape
        semantic = self.null_semantic.view(1, 1, -1).expand(rows, macros, -1)
        anchor = self.null_anchor.view(1, 1, -1).expand(rows, macros, -1)
        semantic_projection = self.semantic_adapter(semantic)
        soft_macro = self.semantic_input_norm(
            anchor.to(dtype=semantic_projection.dtype)
            + torch.sigmoid(self.semantic_gate) * semantic_projection
        )
        soft_macro = soft_macro * valid.unsqueeze(-1).to(soft_macro.dtype)
        if self.online_transformer is None:
            raise RuntimeError("learned-null input requires ordinary Transformer")
        return soft_macro.to(
            dtype=self.online_transformer.input_projection.weight.dtype
        )

    def _soft_macro_input(
        self,
        batch: Mapping[str, torch.Tensor],
        valid: torch.Tensor,
    ) -> torch.Tensor:
        if self.config.semantic_source == "learned_null":
            return self._learned_null_soft_macro(batch, valid)
        return self._cached_soft_macro(batch, valid)

    def _semantic_macro_hidden(
        self,
        batch: Mapping[str, torch.Tensor],
        valid: torch.Tensor,
    ) -> torch.Tensor:
        if self.config.semantic_input_mode == "native_token":
            required = {
                "input_ids", "attention_mask", "macro_token_start",
                "macro_token_end",
            }
            missing = required - set(batch)
            if missing:
                raise ValueError(f"native semantic input lacks {sorted(missing)}")
            hidden = self._backbone_hidden(
                batch["input_ids"], batch["attention_mask"],
            )
            return _span_pool(
                hidden,
                batch["macro_token_start"],
                batch["macro_token_end"],
                valid,
            )
        soft_macro = self._soft_macro_input(batch, valid)
        return self._backbone_hidden_embeds(
            soft_macro,
            valid.to(dtype=torch.long),
        )

    def _semantic_macro_features(
        self,
        batch: Mapping[str, torch.Tensor],
        valid: torch.Tensor,
    ) -> torch.Tensor:
        if self.config.semantic_input_mode in {
            "cached_macro_soft_token", "learned_null_macro_token",
        }:
            soft_macro = self._soft_macro_input(batch, valid)
            if self.config.online_backbone_type == "qwen_lora":
                return self._backbone_macro_features(
                    soft_macro, valid.to(dtype=torch.long),
                )
            if self.online_transformer is None:
                raise RuntimeError("ordinary online Transformer is missing")
            return self.online_transformer(soft_macro, valid)
        hidden = self._semantic_macro_hidden(batch, valid)
        projection_dtype = next(self.asm_projection.parameters()).dtype
        return self.asm_projection(hidden.to(dtype=projection_dtype))

    def forward(self, batch: Mapping[str, torch.Tensor]) -> Dict[str, torch.Tensor]:
        valid = batch["valid_macro_mask"].bool()
        rows, macros = valid.shape
        if macros != 256:
            raise ValueError(f"macro contract requires M=256, got {macros}")
        asm_macro = self._semantic_macro_features(batch, valid)
        asm_macro_before_mode = asm_macro
        numeric = self.numeric_encoder(
            batch["uop_fields"],
            batch["uop_valid_mask"],
            batch["uop_to_macro"],
            batch["uop_access"],
            batch["uop_semantic_flags"],
            batch["uop_count"],
            batch["dynamic_uop_fields"],
        )
        side = self.side_projection(torch.cat([
            batch["chunk_summary"],
            batch["relation_features"],
            batch["state_features"],
            batch["uarch_features"],
        ], dim=-1).float())
        mixer_relation = batch["relation_features"]
        mixer_state = batch["state_features"]
        if self.config.semantic_mode == "side_only":
            # Keep the exact same backbone and trainable graph for a
            # capacity-matched control, but remove all semantic signal.
            asm_macro = asm_macro * 0.0
        elif self.config.semantic_mode == "llm_only":
            # Diagnostic requested by the design: no dynamic/global/uarch
            # side information, while retaining native assembly and position.
            numeric = numeric * 0.0
            side = side * 0.0
            mixer_relation = mixer_relation * 0.0
            mixer_state = mixer_state * 0.0
        positions = self.position_embedding(
            torch.arange(macros, device=valid.device)
        ).unsqueeze(0)
        expanded_positions = positions.expand(rows, -1, -1)
        expanded_side = side.unsqueeze(1).expand(-1, macros, -1)
        macro_state = self.fusion_norm(
            asm_macro + numeric + expanded_positions + expanded_side
        )
        macro_state = macro_state * valid.unsqueeze(-1).to(macro_state.dtype)
        pre_mixer_macro_state = macro_state
        sample_ptr = batch.get("sample_ptr")
        if sample_ptr is None:
            sample_ptr = torch.tensor(
                [0, rows], dtype=torch.long, device=valid.device,
            )
        if self.config.core_mixer_mode == "summary":
            weights = valid.unsqueeze(-1).to(macro_state.dtype)
            core_summary = (macro_state * weights).sum(dim=1) / (
                weights.sum(dim=1).clamp_min(1.0)
            )
            mixed_summary = self.core_mixer(core_summary, sample_ptr)
            if self.core_gate is None:
                raise RuntimeError("legacy summary mixer lacks core gate")
            core_context = (
                torch.sigmoid(self.core_gate) * mixed_summary
            ).unsqueeze(1)
            macro_state = macro_state + core_context
            output_core_state = mixed_summary
        else:
            macro_state, cross_context = self.core_mixer(
                macro_state,
                valid,
                sample_ptr,
                mixer_relation,
                mixer_state,
            )
            output_core_state = cross_context
        macro_state = self.fusion_norm(macro_state)
        macro_state = macro_state * valid.unsqueeze(-1).to(macro_state.dtype)

        raw_gap = self.gap_head(macro_state).squeeze(-1).float()
        gap = F.softplus(raw_gap) * valid.to(raw_gap.dtype)
        commit_time = torch.cumsum(gap.double(), dim=1).to(gap.dtype)
        branch_logit = self.branch_head(macro_state).squeeze(-1).float()
        branch_probability = torch.sigmoid(branch_logit) * valid.to(
            branch_logit.dtype
        )
        commit_logits = (
            self.horizons.to(commit_time.dtype)[None, None, :]
            - commit_time.unsqueeze(-1)
        ) / float(self.config.commit_temperature)
        commit_probability = torch.sigmoid(commit_logits)
        commit_probability = commit_probability * valid.unsqueeze(-1).to(
            commit_probability.dtype
        )
        result = {
            "retirement_gap_macro": gap,
            "commit_time_macro": commit_time,
            "branch_miss_logit": branch_logit,
            "branch_miss_probability": branch_probability,
            "commit_logits": commit_logits,
            "commit_probability": commit_probability,
            "progress_macro": commit_probability.sum(dim=1),
            "hard_prefix_macro": (
                commit_time.unsqueeze(-1)
                <= self.horizons.to(commit_time.dtype)[None, None, :]
            ) & valid.unsqueeze(-1),
            "macro_state": macro_state,
            "core_state": output_core_state,
        }
        if self.collect_activation_diagnostics:
            activations = {
                "llm_branch_before_intervention": asm_macro_before_mode,
                "llm_branch_used": asm_macro,
                "numeric_branch_used": numeric,
                "position_branch": expanded_positions,
                "side_branch_used": expanded_side,
                "macro_state_pre_mixer": pre_mixer_macro_state,
                "macro_state_final": macro_state,
            }
            result["activation_statistics"] = {
                name: self._masked_activation_statistics(value, valid)
                for name, value in activations.items()
            }
        return result


def macro_v29_loss(
    predictions: Mapping[str, torch.Tensor],
    batch: Mapping[str, torch.Tensor],
    *,
    weights: Optional[Mapping[str, float]] = None,
    time_beta: float = 0.2,
    progress_beta: float = 8.0,
    branch_count_beta: float = 1.0,
) -> Dict[str, torch.Tensor]:
    """Macro version of the v29 commit/prefix/progress/drift/branch objective."""

    values = dict(weights or {})
    valid = batch["valid_macro_mask"].bool()
    target_time = batch["commit_time_target_macro"].to(
        predictions["commit_time_macro"].dtype
    )
    time_element = F.smooth_l1_loss(
        torch.log1p(predictions["commit_time_macro"]),
        torch.log1p(target_time),
        beta=float(time_beta),
        reduction="none",
    )
    time_loss = (time_element * valid).sum() / valid.sum().clamp_min(1)
    prefix_mask = valid.unsqueeze(-1).expand_as(batch["prefix_target"])
    prefix_element = F.binary_cross_entropy_with_logits(
        predictions["commit_logits"],
        batch["prefix_target"].to(predictions["commit_logits"].dtype),
        reduction="none",
    )
    prefix_loss = (
        prefix_element * prefix_mask
    ).sum() / prefix_mask.sum().clamp_min(1)
    progress_loss = F.smooth_l1_loss(
        predictions["progress_macro"],
        batch["progress_target_macro"].to(predictions["progress_macro"].dtype),
        beta=float(progress_beta),
        reduction="mean",
    ) / 256.0
    branch_valid = batch["branch_mask"].bool() & valid
    if branch_valid.any():
        branch_element = F.binary_cross_entropy_with_logits(
            predictions["branch_miss_logit"],
            batch["branch_miss_target"].to(
                predictions["branch_miss_logit"].dtype
            ),
            reduction="none",
        )
        branch_loss = (
            branch_element * branch_valid
        ).sum() / branch_valid.sum().clamp_min(1)
    else:
        branch_loss = predictions["commit_time_macro"].sum() * 0.0
    branch_weight = batch["branch_mask"].to(
        predictions["commit_probability"].dtype
    ).unsqueeze(-1)
    predicted_misses = (
        predictions["commit_probability"]
        * predictions["branch_miss_probability"].unsqueeze(-1)
        * branch_weight
    ).sum(dim=1)
    true_misses = (
        batch["prefix_target"].to(predicted_misses.dtype)
        * batch["branch_miss_target"].to(predicted_misses.dtype).unsqueeze(-1)
        * branch_weight
    ).sum(dim=1)
    branch_count = F.smooth_l1_loss(
        predicted_misses,
        true_misses,
        beta=float(branch_count_beta),
        reduction="mean",
    ) / 32.0

    cumulative = predictions["progress_macro"].sum() * 0.0
    cumulative_keys = {
        "sample_period_cycles", "horizons", "row_sequence",
        "row_sequence_step", "core_slots",
    }
    if cumulative_keys.issubset(batch):
        sample_period = float(batch["sample_period_cycles"])
        horizons = batch["horizons"].to(predictions["progress_macro"].device)
        horizon_index = int(
            torch.argmin((horizons - sample_period).abs()).item()
        )
        if abs(float(horizons[horizon_index]) - sample_period) > 1.0e-4:
            raise ValueError("sample_period_cycles must be in horizons")
        predicted_progress = predictions["progress_macro"][:, horizon_index]
        target_progress = batch["progress_target_macro"][:, horizon_index]
        groups: Dict[tuple[int, int], list[tuple[int, int]]] = {}
        keys = torch.stack([
            batch["row_sequence"], batch["core_slots"],
        ], dim=1)
        steps = batch["row_sequence_step"].detach().cpu().tolist()
        for row, (pair, step) in enumerate(zip(
            keys.detach().cpu().tolist(), steps,
        )):
            groups.setdefault(
                (int(pair[0]), int(pair[1])), [],
            ).append((int(step), row))
        drift_losses = []
        for step_rows in groups.values():
            if len(step_rows) < 2:
                continue
            step_rows.sort()
            observed_steps = [step for step, _row in step_rows]
            if observed_steps != list(range(len(step_rows))):
                raise ValueError(
                    "cumulative drift rows must cover ordered contiguous steps"
                )
            rows = [row for _step, row in step_rows]
            index = torch.tensor(
                rows, dtype=torch.long, device=predicted_progress.device,
            )
            signed = (
                predicted_progress.index_select(0, index).sum()
                - target_progress.index_select(0, index).sum()
            )
            normalized = signed / max(1.0, 256.0 * len(rows))
            drift_losses.append(F.smooth_l1_loss(
                normalized,
                torch.zeros_like(normalized),
                beta=0.02,
                reduction="mean",
            ))
        if drift_losses:
            cumulative = torch.stack(drift_losses).mean()
    total = (
        float(values.get("commit_time", 1.0)) * time_loss
        + float(values.get("prefix_bce", 0.5)) * prefix_loss
        + float(values.get("progress_count", 0.5)) * progress_loss
        + float(values.get("cumulative", 0.25)) * cumulative
        + float(values.get("branch_token", 0.1)) * branch_loss
        + float(values.get("branch_count", 0.1)) * branch_count
    )
    return {
        "total": total,
        "commit_time": time_loss,
        "prefix_bce": prefix_loss,
        "progress_count": progress_loss,
        "cumulative": cumulative,
        "branch_token": branch_loss,
        "branch_count": branch_count,
    }
