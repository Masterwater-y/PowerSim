"""v28.1 four-branch functional-only fixed-chunk timing model."""
from __future__ import annotations

from collections import OrderedDict
from contextlib import nullcontext
from typing import Any, Dict, List, Optional, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.attention import SDPBackend, sdpa_kernel

try:
    from torch.nn.attention.flex_attention import (
        create_block_mask,
        flex_attention,
    )
except ImportError:  # pragma: no cover - older supported PyTorch fallback.
    create_block_mask = None  # type: ignore[assignment]
    flex_attention = None  # type: ignore[assignment]

from ..chunker.functional_features import (
    CHUNK_SUMMARY_NAMES,
    DYNAMIC_FIELD_SIZES,
    FIELD_GROUP_INDICES,
    FIELD_SIZES,
    RELATION_FEATURE_NAMES,
    UARCH_FEATURE_NAMES,
)


CROSS_ATTENTION_BACKEND_LEGACY = "legacy"
CROSS_ATTENTION_BACKEND_FLEX_SHARED_KV = "flex_shared_kv"
CROSS_ATTENTION_BACKEND_HIERARCHICAL_LATENT = "hierarchical_latent"
CROSS_ATTENTION_BACKEND_QUERY_PRESERVING_KV = "query_preserving_kv"
CROSS_ATTENTION_BACKEND_LOCAL_ONLY = "local_only"
CROSS_ATTENTION_BACKENDS = {
    CROSS_ATTENTION_BACKEND_LEGACY,
    CROSS_ATTENTION_BACKEND_FLEX_SHARED_KV,
    CROSS_ATTENTION_BACKEND_HIERARCHICAL_LATENT,
    CROSS_ATTENTION_BACKEND_QUERY_PRESERVING_KV,
    CROSS_ATTENTION_BACKEND_LOCAL_ONLY,
}
QRKV_PROJECTION_BACKEND_SEPARATE = "separate"
QRKV_PROJECTION_BACKEND_FUSED = "fused"
QRKV_PROJECTION_BACKENDS = {
    QRKV_PROJECTION_BACKEND_SEPARATE,
    QRKV_PROJECTION_BACKEND_FUSED,
}
_FLEX_BLOCK_MASK_CACHE: Dict[Tuple[str, int, int, int], Any] = {}
_COMPILED_FLEX_ATTENTION: Any = None


def _shared_kv_block_mask(
    n_core: int,
    length: int,
    device: torch.device,
) -> Any:
    """Cache the static mask that excludes a query's own core block."""
    if create_block_mask is None:
        raise RuntimeError(
            "flex_shared_kv requires torch.nn.attention.flex_attention"
        )
    device_index = -1 if device.index is None else int(device.index)
    key = (device.type, device_index, int(n_core), int(length))
    cached = _FLEX_BLOCK_MASK_CACHE.get(key)
    if cached is not None:
        return cached
    core_length = int(length)

    def different_core(_batch: Any, _head: Any, query: Any, key_value: Any) -> Any:
        return query // core_length != key_value // core_length

    sequence = int(n_core) * core_length
    mask = create_block_mask(
        different_core,
        B=None,
        H=None,
        Q_LEN=sequence,
        KV_LEN=sequence,
        device=device,
        BLOCK_SIZE=128,
    )
    _FLEX_BLOCK_MASK_CACHE[key] = mask
    return mask


def _run_flex_attention(
    query: torch.Tensor,
    key: torch.Tensor,
    value: torch.Tensor,
    block_mask: Any,
) -> torch.Tensor:
    """Use the fused compiled kernel on CUDA and the reference op on CPU."""
    if flex_attention is None:
        raise RuntimeError(
            "flex_shared_kv requires torch.nn.attention.flex_attention"
        )
    if query.device.type != "cuda":
        return flex_attention(query, key, value, block_mask=block_mask)
    global _COMPILED_FLEX_ATTENTION
    if _COMPILED_FLEX_ATTENTION is None:
        _COMPILED_FLEX_ATTENTION = torch.compile(
            flex_attention, dynamic=False,
        )
    return _COMPILED_FLEX_ATTENTION(
        query, key, value, block_mask=block_mask,
    )


class StaticChunkEncoder(nn.Module):
    """Encode base, branch and resource fields through separate branches."""

    def __init__(
        self,
        d_field: int = 16,
        d_static: int = 128,
        max_K: int = 512,
    ) -> None:
        super().__init__()
        self.field_sizes = tuple(int(x) for x in FIELD_SIZES)
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
        self.pos_embed = nn.Embedding(self.max_K, d_static)
        self.encoder = nn.Sequential(
            nn.LayerNorm(d_static),
            nn.Linear(d_static, d_static),
            nn.GELU(),
            nn.Linear(d_static, d_static),
        )

    def encode_tokens(
        self,
        per_uop_fields: torch.Tensor,
    ) -> torch.Tensor:
        # fields [N,K,F]
        if per_uop_fields.ndim != 3:
            raise ValueError(f"expected [N,K,F], got {tuple(per_uop_fields.shape)}")
        n, K, F = per_uop_fields.shape
        if F != len(self.field_embeddings):
            raise ValueError(f"field count {F} != expected {len(self.field_embeddings)}")
        if K > self.max_K:
            raise ValueError(f"chunk K={K} exceeds max_K={self.max_K}")
        embedded: List[torch.Tensor] = []
        for field_idx, (embedding, size) in enumerate(zip(self.field_embeddings, self.field_sizes)):
            values = per_uop_fields[..., field_idx].clamp(min=0, max=size)
            embedded.append(embedding(values))
        group_outputs = []
        for name, indices in self.group_indices.items():
            group_outputs.append(self.group_projections[name](
                torch.cat([embedded[index] for index in indices], dim=-1)
            ))
        h = torch.stack(group_outputs, dim=0).sum(dim=0)
        h = h + self.pos_embed.weight[:K].unsqueeze(0)
        return self.encoder(h)

    @staticmethod
    def pool_tokens(
        h: torch.Tensor,
        valid_uop_mask: torch.Tensor,
    ) -> torch.Tensor:
        mask = valid_uop_mask.bool()
        if (~mask).all(dim=1).any():
            raise ValueError("chunk with no valid UOPs")
        denom = mask.sum(dim=1, keepdim=True).clamp(min=1).to(h.dtype)
        return (h * mask.unsqueeze(-1).to(h.dtype)).sum(dim=1) / denom

    def forward(
        self,
        per_uop_fields: torch.Tensor,
        valid_uop_mask: torch.Tensor,
    ) -> torch.Tensor:
        h = self.encode_tokens(per_uop_fields)
        return self.pool_tokens(h, valid_uop_mask)


class FunctionalInteractionBlock(nn.Module):
    """One Q/K/V/R block over fixed-chunk UOP tokens.

    Q/K/V model same-chunk token context. R is a separate cross-core query that
    attends to other chunks in the same scheduler sample.
    """

    def __init__(
        self,
        d_dyn: int,
        n_heads: int = 4,
        dropout: float = 0.1,
        ffn_dim: Optional[int] = None,
        cross_target_block: int = 0,
        sdpa_backend: str = "auto",
        cross_attention_backend: str = CROSS_ATTENTION_BACKEND_LEGACY,
        qrkv_projection_backend: str = QRKV_PROJECTION_BACKEND_SEPARATE,
        cross_latent_count: int = 0,
        cross_latent_stabilization: bool = False,
        cross_latent_fp32_training: bool = True,
        cross_anchor_count: int = 0,
        cross_anchor_positional_count: int = 0,
        cross_anchor_stabilization: bool = False,
        cross_anchor_fp32_training: bool = True,
    ) -> None:
        super().__init__()
        if int(d_dyn) % int(n_heads) != 0:
            raise ValueError("d_dyn must be divisible by n_heads")
        hidden = int(ffn_dim or d_dyn * 2)
        self.d_dyn = int(d_dyn)
        self.n_heads = int(n_heads)
        self.head_dim = self.d_dyn // self.n_heads
        self.cross_target_block = int(cross_target_block)
        if self.cross_target_block < 0:
            raise ValueError("cross_target_block must be non-negative")
        self.sdpa_backend = str(sdpa_backend).lower()
        if self.sdpa_backend not in {"auto", "flash", "no_flash", "efficient", "math"}:
            raise ValueError(f"unsupported sdpa_backend={sdpa_backend}")
        self.cross_attention_backend = str(cross_attention_backend).lower()
        if self.cross_attention_backend not in CROSS_ATTENTION_BACKENDS:
            raise ValueError(
                "unsupported cross_attention_backend="
                f"{cross_attention_backend!r}; expected one of "
                f"{sorted(CROSS_ATTENTION_BACKENDS)}"
            )
        self.legacy_cross_attention_calls = 0
        self.shared_kv_cross_attention_calls = 0
        self.hierarchical_latent_cross_attention_calls = 0
        self.query_preserving_kv_cross_attention_calls = 0
        self.local_only_cross_attention_calls = 0
        self.cross_latent_count = int(cross_latent_count)
        self.cross_latent_stabilization = bool(cross_latent_stabilization)
        self.cross_latent_fp32_training = bool(cross_latent_fp32_training)
        if self.cross_latent_count < 0:
            raise ValueError("cross_latent_count must be non-negative")
        if (
            self.cross_attention_backend
            == CROSS_ATTENTION_BACKEND_HIERARCHICAL_LATENT
            and self.cross_latent_count <= 0
        ):
            raise ValueError(
                "hierarchical_latent cross attention requires "
                "cross_latent_count > 0"
            )
        if self.cross_latent_count:
            self.cross_latent_queries = nn.Parameter(torch.empty(
                self.cross_latent_count, self.d_dyn,
            ))
            nn.init.normal_(
                self.cross_latent_queries,
                mean=0.0,
                std=self.d_dyn ** -0.5,
            )
            if self.cross_latent_stabilization:
                # Each latent attention stage receives independently
                # normalized Q/K inputs. Values remain unnormalized so the
                # stages can carry magnitude without feeding it into logits.
                self.cross_latent_query_norm = nn.LayerNorm(self.d_dyn)
                self.cross_latent_state_norm = nn.LayerNorm(self.d_dyn)
                self.cross_latent_broadcast_query_norm = nn.LayerNorm(
                    self.d_dyn
                )
                self.cross_latent_broadcast_key_norm = nn.LayerNorm(
                    self.d_dyn
                )
            else:
                # Checkpoints created before latent stabilization contain only
                # cross_latent_queries. Preserve their original state schema
                # and attention math for strict historical evaluation.
                self.cross_latent_query_norm = None
                self.cross_latent_state_norm = None
                self.cross_latent_broadcast_query_norm = None
                self.cross_latent_broadcast_key_norm = None
        else:
            self.register_parameter("cross_latent_queries", None)
            self.cross_latent_query_norm = None
            self.cross_latent_state_norm = None
            self.cross_latent_broadcast_query_norm = None
            self.cross_latent_broadcast_key_norm = None
        self.cross_anchor_count = int(cross_anchor_count)
        self.cross_anchor_positional_count = int(
            cross_anchor_positional_count
        )
        self.cross_anchor_content_count = (
            self.cross_anchor_count - self.cross_anchor_positional_count
        )
        self.cross_anchor_stabilization = bool(cross_anchor_stabilization)
        self.cross_anchor_fp32_training = bool(cross_anchor_fp32_training)
        if self.cross_anchor_count < 0:
            raise ValueError("cross_anchor_count must be non-negative")
        if not 0 <= self.cross_anchor_positional_count <= self.cross_anchor_count:
            raise ValueError(
                "cross_anchor_positional_count must be in [0,cross_anchor_count]"
            )
        if (
            self.cross_attention_backend
            == CROSS_ATTENTION_BACKEND_QUERY_PRESERVING_KV
            and self.cross_anchor_count <= 0
        ):
            raise ValueError(
                "query_preserving_kv cross attention requires "
                "cross_anchor_count > 0"
            )
        if self.cross_anchor_content_count > 0:
            self.cross_anchor_queries = nn.Parameter(torch.empty(
                self.cross_anchor_content_count, self.d_dyn,
            ))
            nn.init.normal_(
                self.cross_anchor_queries,
                mean=0.0,
                std=self.d_dyn ** -0.5,
            )
        else:
            self.register_parameter("cross_anchor_queries", None)
        if self.cross_anchor_count and self.cross_anchor_stabilization:
            self.cross_anchor_query_norm = nn.LayerNorm(self.d_dyn)
            self.cross_anchor_source_key_norm = nn.LayerNorm(self.d_dyn)
            self.cross_anchor_target_query_norm = nn.LayerNorm(self.d_dyn)
            self.cross_anchor_output_key_norm = nn.LayerNorm(self.d_dyn)
        else:
            self.cross_anchor_query_norm = None
            self.cross_anchor_source_key_norm = None
            self.cross_anchor_target_query_norm = None
            self.cross_anchor_output_key_norm = None
        self.qrkv_projection_backend = str(qrkv_projection_backend).lower()
        if self.qrkv_projection_backend not in QRKV_PROJECTION_BACKENDS:
            raise ValueError(
                "unsupported qrkv_projection_backend="
                f"{qrkv_projection_backend!r}; expected one of "
                f"{sorted(QRKV_PROJECTION_BACKENDS)}"
            )
        self.separate_qrkv_projection_calls = 0
        self.fused_qrkv_projection_calls = 0
        self._fused_qrkv_weight: Optional[torch.Tensor] = None
        self._fused_qrkv_weight_key: Optional[Tuple[Any, ...]] = None
        self.attn_norm = nn.LayerNorm(d_dyn)
        self.q_proj = nn.Linear(d_dyn, d_dyn, bias=False)
        self.r_proj = nn.Linear(d_dyn, d_dyn, bias=False)
        self.k_proj = nn.Linear(d_dyn, d_dyn, bias=False)
        self.v_proj = nn.Linear(d_dyn, d_dyn, bias=False)
        self.local_o_proj = nn.Linear(d_dyn, d_dyn, bias=False)
        self.cross_o_proj = nn.Linear(d_dyn, d_dyn, bias=False)
        self.attn_drop = nn.Dropout(dropout)
        self.ffn_norm = nn.LayerNorm(d_dyn)
        self.ff = nn.Sequential(
            nn.Linear(d_dyn, hidden),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(hidden, d_dyn),
            nn.Dropout(dropout),
        )

    def _project_qrkv(
        self, h: torch.Tensor,
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        if self.qrkv_projection_backend != QRKV_PROJECTION_BACKEND_FUSED:
            self.separate_qrkv_projection_calls += 1
            return (
                self.q_proj(h),
                self.r_proj(h),
                self.k_proj(h),
                self.v_proj(h),
            )
        if self.training:
            # Training keeps the established parameterized module calls and
            # autograd behavior.  The fused path is an inference optimization.
            self.separate_qrkv_projection_calls += 1
            return (
                self.q_proj(h),
                self.r_proj(h),
                self.k_proj(h),
                self.v_proj(h),
            )
        target_dtype = h.dtype
        if h.device.type == "cuda" and torch.is_autocast_enabled("cuda"):
            target_dtype = torch.get_autocast_dtype("cuda")
        weights = (
            self.q_proj.weight,
            self.r_proj.weight,
            self.k_proj.weight,
            self.v_proj.weight,
        )
        def weight_version(weight: torch.Tensor) -> int:
            try:
                return int(weight._version)
            except RuntimeError:
                # Parameters constructed inside torch.inference_mode() do not
                # expose a version counter. They are immutable for this
                # eval-only cache, so identity is sufficient below.
                return -1

        device_index = -1 if h.device.index is None else int(h.device.index)
        cache_key = (
            h.device.type,
            device_index,
            target_dtype,
            *((id(weight), weight_version(weight)) for weight in weights),
        )
        if (
            self._fused_qrkv_weight is None
            or self._fused_qrkv_weight_key != cache_key
        ):
            self._fused_qrkv_weight = torch.cat([
                weight.detach().to(device=h.device, dtype=target_dtype)
                for weight in weights
            ], dim=0).contiguous()
            self._fused_qrkv_weight_key = cache_key
        self.fused_qrkv_projection_calls += 1
        projected = F.linear(h, self._fused_qrkv_weight)
        q, r, k, v = projected.chunk(4, dim=-1)
        return q, r, k, v

    def _split_heads(self, x: torch.Tensor) -> torch.Tensor:
        # [B,L,D] -> [B,H,L,Dh]
        B, L, D = x.shape
        if D != self.d_dyn:
            raise ValueError(f"expected dim={self.d_dyn}, got {D}")
        return x.view(B, L, self.n_heads, self.head_dim).transpose(1, 2)

    def _merge_heads(self, x: torch.Tensor) -> torch.Tensor:
        # [B,H,L,Dh] -> [B,L,D]
        B, H, L, Dh = x.shape
        return x.transpose(1, 2).contiguous().view(B, L, H * Dh)

    def _attend(
        self,
        q: torch.Tensor,
        k: torch.Tensor,
        v: torch.Tensor,
        key_mask: torch.Tensor,
    ) -> torch.Tensor:
        attn_mask = key_mask.bool()[:, None, None, :]
        with self._sdpa_context():
            out = F.scaled_dot_product_attention(
                self._split_heads(q),
                self._split_heads(k),
                self._split_heads(v),
                attn_mask=attn_mask,
                dropout_p=0.0,
                is_causal=False,
            )
        return self._merge_heads(out)

    def _sdpa_context(self):
        """Choose the PyTorch SDPA backend without changing attention math."""
        if self.sdpa_backend == "auto":
            return nullcontext()
        if self.sdpa_backend == "flash":
            return sdpa_kernel([
                SDPBackend.CUDNN_ATTENTION,
                SDPBackend.FLASH_ATTENTION,
            ], set_priority=True)
        if self.sdpa_backend in {"no_flash", "efficient"}:
            return sdpa_kernel([
                SDPBackend.EFFICIENT_ATTENTION,
                SDPBackend.MATH,
            ], set_priority=True)
        return sdpa_kernel(SDPBackend.MATH)

    @staticmethod
    def _sample_ranges(sample_ptr: torch.Tensor, n_rows: int) -> List[Tuple[int, int]]:
        """Return CPU control ranges for the flattened core axis.

        ``sample_ptr`` describes tensor layout, not model input.  Keeping this
        small control tensor on CPU avoids synchronizing CUDA once per target
        core merely to recover Python slice bounds.
        """
        if sample_ptr.ndim != 1 or int(sample_ptr.numel()) < 2:
            raise ValueError("sample_ptr must be a 1-D tensor with at least two entries")
        if sample_ptr.device.type != "cpu":
            # Direct callers may still pass a CUDA pointer.  This is one
            # bounded synchronization per batch, never one per core.
            sample_ptr = sample_ptr.detach().cpu()
        ptr = [int(value) for value in sample_ptr.tolist()]
        if ptr[0] != 0 or ptr[-1] != int(n_rows):
            raise ValueError(
                f"sample_ptr must span [0,{n_rows}], got [{ptr[0]},{ptr[-1]}]"
            )
        if any(end < start for start, end in zip(ptr, ptr[1:])):
            raise ValueError("sample_ptr must be non-decreasing")
        return list(zip(ptr, ptr[1:]))

    def _cross_attention_legacy(
        self,
        r: torch.Tensor,
        k: torch.Tensor,
        v: torch.Tensor,
        valid_uop_mask: torch.Tensor,
        sample_ptr: torch.Tensor,
    ) -> torch.Tensor:
        """Full cross-core R-attention, bucketed by active-core count.

        Chunks have a fixed K, so ragged UOP packing offers almost no benefit
        outside rare tail chunks.  Instead, samples with the same number of
        active cores are bucketed and their target cores are processed in
        SDPA batches.  Each target UOP still attends every valid UOP on every
        *other* core: this is exactly the original full-QKVR operation,
        without one Python loop, concat, and SDPA launch per target core.

        ``cross_target_block=0`` (the default) processes all target cores in
        one call.  A positive value is an optional future memory guard; it
        keeps the same attention math while bounding temporary K/V expansion.
        """
        cross = r * 0.0
        mask = valid_uop_mask.bool()
        K = int(r.shape[1])
        buckets: Dict[int, List[int]] = {}
        for start, end in self._sample_ranges(sample_ptr, int(r.shape[0])):
            n_core = end - start
            if n_core > 0:
                buckets.setdefault(n_core, []).append(start)

        for n_core, starts_cpu in buckets.items():
            starts = torch.tensor(starts_cpu, dtype=torch.long, device=r.device)
            row_offsets = torch.arange(n_core, dtype=torch.long, device=r.device)
            row_index = (starts[:, None] + row_offsets[None, :]).reshape(-1)
            n_samples = len(starts_cpu)

            r_group = r.index_select(0, row_index).reshape(
                n_samples, n_core, K, self.d_dyn,
            )
            if n_core == 1:
                # Keep r_proj in the autograd graph for single-core DDP ranks.
                cross_group = r_group * 0.0
            else:
                k_group = k.index_select(0, row_index).reshape(
                    n_samples, n_core, K, self.d_dyn,
                )
                v_group = v.index_select(0, row_index).reshape(
                    n_samples, n_core, K, self.d_dyn,
                )
                mask_group = mask.index_select(0, row_index).reshape(
                    n_samples, n_core, K,
                )
                core_ids = torch.arange(n_core, device=r.device)
                other_core_ids = core_ids.repeat(n_core, 1)[
                    ~torch.eye(n_core, dtype=torch.bool, device=r.device)
                ].reshape(n_core, n_core - 1)
                cross_parts: List[torch.Tensor] = []
                target_block = self.cross_target_block or n_core
                for target_start in range(0, n_core, target_block):
                    target_end = min(n_core, target_start + target_block)
                    n_target = target_end - target_start
                    target_other = other_core_ids[target_start:target_end]
                    k_other = k_group[:, target_other].reshape(
                        n_samples * n_target, (n_core - 1) * K, self.d_dyn,
                    )
                    v_other = v_group[:, target_other].reshape_as(k_other)
                    other_mask = mask_group[:, target_other].reshape(
                        n_samples * n_target, (n_core - 1) * K,
                    )
                    cross_parts.append(self._attend(
                        r_group[:, target_start:target_end].reshape(
                            n_samples * n_target, K, self.d_dyn,
                        ),
                        k_other,
                        v_other,
                        other_mask,
                    ).reshape(n_samples, n_target, K, self.d_dyn))
                cross_group = torch.cat(cross_parts, dim=1)
            cross = cross.index_copy(0, row_index, cross_group.reshape(-1, K, self.d_dyn))
        return cross

    def _cross_attention_flex_shared_kv(
        self,
        r: torch.Tensor,
        k: torch.Tensor,
        v: torch.Tensor,
        sample_ptr: torch.Tensor,
    ) -> torch.Tensor:
        """Exact full cross-core attention without target-wise K/V copies.

        Each scheduler sample becomes one sequence of ``n_core * K`` tokens.
        A static FlexAttention block mask removes the diagonal core blocks, so
        every query attends precisely the same other-core tokens as the legacy
        gather implementation while K/V remain shared once per sample.
        """
        cross = r * 0.0
        K = int(r.shape[1])
        buckets: Dict[int, List[int]] = {}
        for start, end in self._sample_ranges(sample_ptr, int(r.shape[0])):
            n_core = end - start
            if n_core > 0:
                buckets.setdefault(n_core, []).append(start)

        for n_core, starts_cpu in buckets.items():
            starts = torch.tensor(starts_cpu, dtype=torch.long, device=r.device)
            row_offsets = torch.arange(n_core, dtype=torch.long, device=r.device)
            row_index = (starts[:, None] + row_offsets[None, :]).reshape(-1)
            n_samples = len(starts_cpu)
            r_group = r.index_select(0, row_index).reshape(
                n_samples, n_core * K, self.d_dyn,
            )
            if n_core == 1:
                cross_group = r_group * 0.0
            else:
                k_group = k.index_select(0, row_index).reshape(
                    n_samples, n_core * K, self.d_dyn,
                )
                v_group = v.index_select(0, row_index).reshape_as(k_group)
                block_mask = _shared_kv_block_mask(n_core, K, r.device)
                attended = _run_flex_attention(
                    self._split_heads(r_group),
                    self._split_heads(k_group),
                    self._split_heads(v_group),
                    block_mask,
                )
                cross_group = self._merge_heads(attended)
            cross = cross.index_copy(
                0, row_index, cross_group.reshape(-1, K, self.d_dyn),
            )
        return cross

    def _pool_content_kv_anchors(
        self,
        query: torch.Tensor,
        score_key: torch.Tensor,
        output_key: torch.Tensor,
        output_value: torch.Tensor,
        key_mask: torch.Tensor,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """Pool K and V with one shared content-attention distribution."""
        query_heads = self._split_heads(query)
        score_key_heads = self._split_heads(score_key)
        scores = torch.matmul(
            query_heads,
            score_key_heads.transpose(-2, -1),
        ) * (self.head_dim ** -0.5)
        valid = key_mask.bool()[:, None, None, :]
        scores = scores.masked_fill(~valid, torch.finfo(scores.dtype).min)
        weights = torch.softmax(scores.float(), dim=-1).to(scores.dtype)
        weights = weights * valid.to(weights.dtype)
        weights = weights / weights.sum(dim=-1, keepdim=True).clamp(min=1.0e-8)
        key_anchor = torch.matmul(weights, self._split_heads(output_key))
        value_anchor = torch.matmul(weights, self._split_heads(output_value))
        return self._merge_heads(key_anchor), self._merge_heads(value_anchor)

    def _pool_positional_kv_anchors(
        self,
        key: torch.Tensor,
        value: torch.Tensor,
        valid_uop_mask: torch.Tensor,
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """Masked mean pooling over deterministic contiguous UOP patches."""
        count = self.cross_anchor_positional_count
        if count <= 0:
            empty = key[:, :0]
            empty_mask = valid_uop_mask[:, :0].bool()
            return empty, empty, empty_mask
        length = int(key.shape[1])
        positions = torch.arange(length, device=key.device)
        patch_ids = torch.div(
            positions * count, length, rounding_mode="floor",
        ).clamp(max=count - 1)
        assignment = F.one_hot(
            patch_ids, num_classes=count,
        ).transpose(0, 1).to(dtype=key.dtype)
        weights = assignment.unsqueeze(0) * valid_uop_mask[:, None, :].to(
            dtype=key.dtype
        )
        denominator = weights.sum(dim=-1, keepdim=True)
        anchor_valid = denominator.squeeze(-1) > 0
        weights = weights / denominator.clamp(min=1.0)
        return (
            torch.matmul(weights, key),
            torch.matmul(weights, value),
            anchor_valid,
        )

    def _cross_attention_query_preserving_kv(
        self,
        r: torch.Tensor,
        k: torch.Tensor,
        v: torch.Tensor,
        valid_uop_mask: torch.Tensor,
        sample_ptr: torch.Tensor,
    ) -> torch.Tensor:
        """Keep every target UOP query and compress only each source core's K/V.

        Each source exports deterministic positional anchors plus learned
        content anchors. Target UOPs directly query the anchors of every other
        core; there is no anchor-to-anchor exchange and no latent broadcast.
        """
        anchor_queries = self.cross_anchor_queries
        if self.cross_anchor_content_count > 0 and anchor_queries is None:
            raise RuntimeError("query-preserving K/V is missing content queries")
        norms = (
            self.cross_anchor_query_norm,
            self.cross_anchor_source_key_norm,
            self.cross_anchor_target_query_norm,
            self.cross_anchor_output_key_norm,
        )
        if self.cross_anchor_stabilization and any(norm is None for norm in norms):
            raise RuntimeError("query-preserving K/V is missing stabilization norms")
        seed_norm, source_norm, target_norm, output_key_norm = norms
        output_dtype = r.dtype
        force_fp32 = bool(
            self.cross_anchor_stabilization
            and self.training
            and self.cross_anchor_fp32_training
            and r.dtype != torch.float32
        )
        precision_context = (
            torch.autocast(device_type=r.device.type, enabled=False)
            if force_fp32 else nullcontext()
        )
        with precision_context:
            compute_dtype = torch.float32 if force_fp32 else r.dtype
            r_compute = r.to(dtype=compute_dtype)
            k_compute = k.to(dtype=compute_dtype)
            v_compute = v.to(dtype=compute_dtype)
            mask = valid_uop_mask.bool()
            rows = int(r.shape[0])
            positional_key, positional_value, positional_valid = (
                self._pool_positional_kv_anchors(
                    k_compute, v_compute, mask,
                )
            )
            anchor_key_parts = [positional_key]
            anchor_value_parts = [positional_value]
            anchor_valid_parts = [positional_valid]
            if self.cross_anchor_content_count > 0:
                assert anchor_queries is not None
                seeds = anchor_queries.to(dtype=compute_dtype)
                seeds = seed_norm(seeds) if seed_norm is not None else seeds
                seed_batch = seeds.unsqueeze(0).expand(rows, -1, -1)
                score_key = source_norm(k_compute) if source_norm is not None else k_compute
                content_key, content_value = self._pool_content_kv_anchors(
                    seed_batch, score_key, k_compute, v_compute, mask,
                )
                content_valid = mask.any(dim=-1, keepdim=True).expand(
                    -1, self.cross_anchor_content_count,
                )
                anchor_key_parts.append(content_key)
                anchor_value_parts.append(content_value)
                anchor_valid_parts.append(content_valid)
            anchor_key = torch.cat(anchor_key_parts, dim=1)
            anchor_value = torch.cat(anchor_value_parts, dim=1)
            anchor_valid = torch.cat(anchor_valid_parts, dim=1)
            if int(anchor_key.shape[1]) != self.cross_anchor_count:
                raise RuntimeError("query-preserving K/V anchor count mismatch")
            scored_anchor_key = (
                output_key_norm(anchor_key)
                if output_key_norm is not None else anchor_key
            )

            cross = r_compute * 0.0
            length = int(r.shape[1])
            anchor_count = self.cross_anchor_count
            buckets: Dict[int, List[int]] = {}
            for start, end in self._sample_ranges(sample_ptr, rows):
                n_core = end - start
                if n_core > 0:
                    buckets.setdefault(n_core, []).append(start)
            for n_core, starts_cpu in buckets.items():
                starts = torch.tensor(
                    starts_cpu, dtype=torch.long, device=r.device,
                )
                row_offsets = torch.arange(
                    n_core, dtype=torch.long, device=r.device,
                )
                row_index = (
                    starts[:, None] + row_offsets[None, :]
                ).reshape(-1)
                n_samples = len(starts_cpu)
                r_group = r_compute.index_select(0, row_index).reshape(
                    n_samples, n_core, length, self.d_dyn,
                )
                if n_core == 1:
                    zero_edge = (
                        anchor_key.index_select(0, row_index).sum() * 0.0
                        + anchor_value.index_select(0, row_index).sum() * 0.0
                    )
                    zero_edge = zero_edge + sum(
                        parameter.sum() * 0.0
                        for norm in norms
                        if norm is not None
                        for parameter in norm.parameters()
                    )
                    cross_group = r_group * 0.0 + zero_edge
                else:
                    key_group = scored_anchor_key.index_select(
                        0, row_index,
                    ).reshape(
                        n_samples, n_core, anchor_count, self.d_dyn,
                    )
                    value_group = anchor_value.index_select(
                        0, row_index,
                    ).reshape_as(key_group)
                    valid_group = anchor_valid.index_select(
                        0, row_index,
                    ).reshape(n_samples, n_core, anchor_count)
                    core_ids = torch.arange(n_core, device=r.device)
                    other_core_ids = core_ids.repeat(n_core, 1)[
                        ~torch.eye(
                            n_core, dtype=torch.bool, device=r.device,
                        )
                    ].reshape(n_core, n_core - 1)
                    query_group = target_norm(r_group) if target_norm is not None else r_group
                    cross_parts: List[torch.Tensor] = []
                    target_block = self.cross_target_block or n_core
                    for target_start in range(0, n_core, target_block):
                        target_end = min(n_core, target_start + target_block)
                        n_target = target_end - target_start
                        target_other = other_core_ids[target_start:target_end]
                        remote_key = key_group[:, target_other].reshape(
                            n_samples * n_target,
                            (n_core - 1) * anchor_count,
                            self.d_dyn,
                        )
                        remote_value = value_group[:, target_other].reshape_as(
                            remote_key
                        )
                        remote_valid = valid_group[:, target_other].reshape(
                            n_samples * n_target,
                            (n_core - 1) * anchor_count,
                        )
                        attended = self._attend(
                            query_group[:, target_start:target_end].reshape(
                                n_samples * n_target, length, self.d_dyn,
                            ),
                            remote_key,
                            remote_value,
                            remote_valid,
                        )
                        cross_parts.append(attended.reshape(
                            n_samples, n_target, length, self.d_dyn,
                        ))
                    cross_group = torch.cat(cross_parts, dim=1)
                cross = cross.index_copy(
                    0, row_index, cross_group.reshape(-1, length, self.d_dyn),
                )
        return cross.to(dtype=output_dtype)

    def _cross_attention_hierarchical_latent(
        self,
        r: torch.Tensor,
        k: torch.Tensor,
        v: torch.Tensor,
        valid_uop_mask: torch.Tensor,
        sample_ptr: torch.Tensor,
    ) -> torch.Tensor:
        """Exchange cross-core information through a small per-core latent set.

        The three attention stages are deliberately hierarchical:

        1. learned per-core queries compress K UOP tokens into M latents;
        2. each core's M latents attend the other cores' latents;
        3. that remote context is broadcast back to the core's K UOP queries.

        For C cores this changes the cross-core score matrix from
        ``O(C * K * (C - 1) * K)`` to
        ``O(C * M * K + C * M * (C - 1) * M + C * K * M)``.  Local
        attention and the output gate remain unchanged.  Unlike the exact
        backends, this is a learned architecture and therefore requires a
        latent checkpoint trained or fine-tuned with the same configuration.
        """
        latent_queries = self.cross_latent_queries
        if latent_queries is None or self.cross_latent_count <= 0:
            raise RuntimeError(
                "hierarchical latent attention is missing latent queries"
            )
        norms = (
            self.cross_latent_query_norm,
            self.cross_latent_state_norm,
            self.cross_latent_broadcast_query_norm,
            self.cross_latent_broadcast_key_norm,
        )
        if self.cross_latent_stabilization and any(
            norm is None for norm in norms
        ):
            raise RuntimeError(
                "hierarchical latent attention is missing stabilization norms"
            )
        query_norm, state_norm, broadcast_query_norm, broadcast_key_norm = norms

        output_dtype = r.dtype
        force_fp32 = bool(
            self.cross_latent_stabilization
            and self.training
            and self.cross_latent_fp32_training
            and r.dtype != torch.float32
        )
        precision_context = (
            torch.autocast(device_type=r.device.type, enabled=False)
            if force_fp32 else nullcontext()
        )
        with precision_context:
            compute_dtype = torch.float32 if force_fp32 else r.dtype
            r_compute = r.to(dtype=compute_dtype)
            k_compute = k.to(dtype=compute_dtype)
            v_compute = v.to(dtype=compute_dtype)
            latent_query_values = latent_queries.to(dtype=compute_dtype)
            mask = valid_uop_mask.bool()
            rows = int(r.shape[0])
            latent_query_input = (
                query_norm(latent_query_values)
                if query_norm is not None else latent_query_values
            )
            latent_query = latent_query_input.unsqueeze(0).expand(
                rows, -1, -1,
            )
            # [core,K,D] -> [core,M,D]. Partial tail chunks are supported
            # through the same validity mask used by local attention.
            local_latent = self._attend(
                latent_query, k_compute, v_compute, mask,
            )
            cross = r_compute * 0.0
            latent_valid = torch.ones(
                (1, self.cross_latent_count),
                dtype=torch.bool,
                device=r.device,
            )
            buckets: Dict[int, List[int]] = {}
            for start, end in self._sample_ranges(sample_ptr, rows):
                n_core = end - start
                if n_core > 0:
                    buckets.setdefault(n_core, []).append(start)
            K = int(r.shape[1])
            M = self.cross_latent_count
            for n_core, starts_cpu in buckets.items():
                starts = torch.tensor(
                    starts_cpu, dtype=torch.long, device=r.device,
                )
                row_offsets = torch.arange(
                    n_core, dtype=torch.long, device=r.device,
                )
                row_index = (
                    starts[:, None] + row_offsets[None, :]
                ).reshape(-1)
                n_samples = len(starts_cpu)
                if n_core == 1:
                    # Preserve a zero gradient edge for every latent-only
                    # parameter on single-core DDP ranks.
                    single = r_compute.index_select(0, row_index) * 0.0
                    latent_edge = local_latent.sum() * 0.0
                    latent_edge = latent_edge + sum(
                        parameter.sum() * 0.0
                        for norm in norms
                        if norm is not None
                        for parameter in norm.parameters()
                    )
                    cross = cross.index_copy(
                        0, row_index, single + latent_edge,
                    )
                    continue
                grouped = local_latent.index_select(0, row_index).reshape(
                    n_samples, n_core, M, self.d_dyn,
                )
                grouped_normalized = (
                    state_norm(grouped)
                    if state_norm is not None else grouped
                )
                core_ids = torch.arange(n_core, device=r.device)
                other_core_ids = core_ids.repeat(n_core, 1)[
                    ~torch.eye(n_core, dtype=torch.bool, device=r.device)
                ].reshape(n_core, n_core - 1)
                remote_key = grouped_normalized[:, other_core_ids].reshape(
                    n_samples * n_core,
                    (n_core - 1) * M,
                    self.d_dyn,
                )
                remote_value = grouped[:, other_core_ids].reshape_as(remote_key)
                remote_mask = torch.ones(
                    (n_samples * n_core, (n_core - 1) * M),
                    dtype=torch.bool,
                    device=r.device,
                )
                remote_latent = self._attend(
                    grouped_normalized.reshape(
                        n_samples * n_core, M, self.d_dyn,
                    ),
                    remote_key,
                    remote_value,
                    remote_mask,
                )
                broadcast_mask = latent_valid.expand(
                    n_samples * n_core, M,
                )
                broadcast_query_input = r_compute.index_select(
                    0, row_index,
                ).reshape(
                    n_samples * n_core, K, self.d_dyn,
                )
                broadcast_query = (
                    broadcast_query_norm(broadcast_query_input)
                    if broadcast_query_norm is not None
                    else broadcast_query_input
                )
                broadcast_key = (
                    broadcast_key_norm(remote_latent)
                    if broadcast_key_norm is not None
                    else remote_latent
                )
                cross_group = self._attend(
                    broadcast_query,
                    broadcast_key,
                    remote_latent,
                    broadcast_mask,
                )
                cross = cross.index_copy(
                    0, row_index, cross_group.reshape(-1, K, self.d_dyn),
                )
        return cross.to(dtype=output_dtype)

    def _cross_attention(
        self,
        r: torch.Tensor,
        k: torch.Tensor,
        v: torch.Tensor,
        valid_uop_mask: torch.Tensor,
        sample_ptr: torch.Tensor,
        all_windows_valid: bool = False,
    ) -> torch.Tensor:
        if self.cross_attention_backend == CROSS_ATTENTION_BACKEND_LOCAL_ONLY:
            self.local_only_cross_attention_calls += 1
            return r * 0.0
        if (
            self.cross_attention_backend
            == CROSS_ATTENTION_BACKEND_QUERY_PRESERVING_KV
        ):
            self.query_preserving_kv_cross_attention_calls += 1
            return self._cross_attention_query_preserving_kv(
                r, k, v, valid_uop_mask, sample_ptr,
            )
        if (
            self.cross_attention_backend
            == CROSS_ATTENTION_BACKEND_HIERARCHICAL_LATENT
        ):
            self.hierarchical_latent_cross_attention_calls += 1
            return self._cross_attention_hierarchical_latent(
                r, k, v, valid_uop_mask, sample_ptr,
            )
        if (
            self.cross_attention_backend
            == CROSS_ATTENTION_BACKEND_FLEX_SHARED_KV
            and bool(all_windows_valid)
            and r.device.type == "cuda"
        ):
            self.shared_kv_cross_attention_calls += 1
            return self._cross_attention_flex_shared_kv(
                r, k, v, sample_ptr,
            )
        self.legacy_cross_attention_calls += 1
        return self._cross_attention_legacy(
            r, k, v, valid_uop_mask, sample_ptr,
        )

    def forward(
        self,
        x: torch.Tensor,
        valid_uop_mask: torch.Tensor,
        sample_ptr: torch.Tensor,
        cross_gate: torch.Tensor,
        all_windows_valid: bool = False,
    ) -> torch.Tensor:
        h = self.attn_norm(x)
        q, r, k, v = self._project_qrkv(h)
        mask = valid_uop_mask.bool()
        local_ctx = self._attend(q, k, v, mask)
        cross_ctx = self._cross_attention(
            r, k, v, mask, sample_ptr, all_windows_valid,
        )
        if cross_gate.shape != (x.shape[0], self.d_dyn):
            raise ValueError(
                f"cross_gate shape {tuple(cross_gate.shape)} != {(x.shape[0], self.d_dyn)}"
            )
        attended = self.local_o_proj(local_ctx)
        attended = attended + (
            cross_gate.unsqueeze(1) * self.cross_o_proj(cross_ctx)
        )
        x = x + self.attn_drop(attended)
        x = x + self.ff(self.ffn_norm(x))
        return x * mask.unsqueeze(-1).to(x.dtype)


class FunctionalInteraction(nn.Module):
    """Cross-core interaction using only functional summaries and relations."""

    def __init__(
        self,
        d_static: int,
        d_dyn: int,
        d_dynamic_field: int = 16,
        n_heads: int = 4,
        dropout: float = 0.1,
        n_layers: int = 1,
        ffn_dim: Optional[int] = None,
        cross_target_block: int = 0,
        sdpa_backend: str = "auto",
        cross_attention_backend: str = CROSS_ATTENTION_BACKEND_LEGACY,
        qrkv_projection_backend: str = QRKV_PROJECTION_BACKEND_SEPARATE,
        cross_latent_count: int = 0,
        cross_latent_stabilization: bool = False,
        cross_latent_fp32_training: bool = True,
        cross_anchor_count: int = 0,
        cross_anchor_positional_count: int = 0,
        cross_anchor_stabilization: bool = False,
        cross_anchor_fp32_training: bool = True,
    ):
        super().__init__()
        self.summary_dim = len(CHUNK_SUMMARY_NAMES)
        self.relation_dim = len(RELATION_FEATURE_NAMES)
        self.uarch_dim = len(UARCH_FEATURE_NAMES)
        self.dynamic_field_sizes = tuple(int(x) for x in DYNAMIC_FIELD_SIZES)
        side_dim = self.summary_dim + self.relation_dim + self.uarch_dim + 1
        self.side_norm = nn.LayerNorm(side_dim)
        self.token_proj = nn.Linear(d_static, d_dyn)
        self.dynamic_embeddings = nn.ModuleList([
            nn.Embedding(size + 1, d_dynamic_field, padding_idx=size)
            for size in self.dynamic_field_sizes
        ])
        self.dynamic_proj = nn.Sequential(
            nn.Linear(d_dynamic_field * len(self.dynamic_field_sizes), d_dyn),
            nn.GELU(),
            nn.Linear(d_dyn, d_dyn),
        )
        self.side_proj = nn.Linear(side_dim, d_dyn)
        gate_dim = self.summary_dim + self.relation_dim
        self.cross_gate = nn.Sequential(
            nn.LayerNorm(gate_dim),
            nn.Linear(gate_dim, max(64, d_dyn // 4)),
            nn.GELU(),
            nn.Linear(max(64, d_dyn // 4), d_dyn),
        )
        self.layers = nn.ModuleList([
            FunctionalInteractionBlock(
                d_dyn, n_heads=n_heads, dropout=dropout, ffn_dim=ffn_dim,
                cross_target_block=cross_target_block,
                sdpa_backend=sdpa_backend,
                cross_attention_backend=cross_attention_backend,
                qrkv_projection_backend=qrkv_projection_backend,
                cross_latent_count=cross_latent_count,
                cross_latent_stabilization=cross_latent_stabilization,
                cross_latent_fp32_training=cross_latent_fp32_training,
                cross_anchor_count=cross_anchor_count,
                cross_anchor_positional_count=cross_anchor_positional_count,
                cross_anchor_stabilization=cross_anchor_stabilization,
                cross_anchor_fp32_training=cross_anchor_fp32_training,
            )
            for _ in range(max(1, int(n_layers)))
        ])

    def forward(
        self,
        token_static: torch.Tensor,
        dynamic_uop_fields: torch.Tensor,
        valid_uop_mask: torch.Tensor,
        chunk_summary: torch.Tensor,
        relation_features: torch.Tensor,
        uarch_features: torch.Tensor,
        n_uops: torch.Tensor,
        sample_ptr: torch.Tensor,
    ) -> torch.Tensor:
        expected = (self.summary_dim, self.relation_dim, self.uarch_dim)
        actual = (
            int(chunk_summary.shape[-1]),
            int(relation_features.shape[-1]),
            int(uarch_features.shape[-1]),
        )
        if actual != expected:
            raise ValueError(f"functional side dims {actual} != expected {expected}")
        if dynamic_uop_fields.ndim != 3:
            raise ValueError("dynamic_uop_fields must have shape [N,K,F_dynamic]")
        if int(dynamic_uop_fields.shape[-1]) != len(self.dynamic_embeddings):
            raise ValueError(
                f"dynamic field count {dynamic_uop_fields.shape[-1]} != "
                f"expected {len(self.dynamic_embeddings)}"
            )
        log_n = torch.log(n_uops.clamp(min=1.0)).unsqueeze(-1) / 8.0
        side = torch.cat([chunk_summary, relation_features, uarch_features, log_n], dim=-1)
        x = self.token_proj(token_static)
        dynamic_embedded = []
        for field_idx, (embedding, size) in enumerate(zip(
            self.dynamic_embeddings, self.dynamic_field_sizes,
        )):
            values = dynamic_uop_fields[..., field_idx].clamp(min=0, max=size)
            dynamic_embedded.append(embedding(values))
        x = x + self.dynamic_proj(torch.cat(dynamic_embedded, dim=-1))
        x = x + self.side_proj(self.side_norm(side)).unsqueeze(1)
        x = x * valid_uop_mask.unsqueeze(-1).to(x.dtype)
        cross_gate = torch.sigmoid(self.cross_gate(
            torch.cat([chunk_summary, relation_features], dim=-1)
        ))
        for layer in self.layers:
            x = layer(x, valid_uop_mask, sample_ptr, cross_gate)
        mask = valid_uop_mask.bool()
        denom = mask.sum(dim=1, keepdim=True).clamp(min=1).to(x.dtype)
        return (x * mask.unsqueeze(-1).to(x.dtype)).sum(dim=1) / denom


class TCSimModel(nn.Module):
    def __init__(
        self,
        d_field: int = 16,
        d_dynamic_field: int = 16,
        d_static: int = 128,
        d_dyn: int = 128,
        n_heads: int = 4,
        n_layers: int = 1,
        ffn_dim: Optional[int] = None,
        dropout: float = 0.1,
        max_K: int = 512,
        cross_target_block: int = 0,
        sdpa_backend: str = "auto",
    ) -> None:
        super().__init__()
        self.static_enc = StaticChunkEncoder(
            d_field=d_field, d_static=d_static, max_K=max_K,
        )
        self.interaction = FunctionalInteraction(
            d_static,
            d_dyn,
            d_dynamic_field=d_dynamic_field,
            n_heads=n_heads,
            dropout=dropout,
            n_layers=n_layers,
            ffn_dim=ffn_dim,
            cross_target_block=cross_target_block,
            sdpa_backend=sdpa_backend,
        )
        self.log_cpi_head = nn.Linear(d_dyn, 1)
        # Auxiliary PMU target: miss probability over all retired branches.
        # No predictor outcome or PMU value is consumed as a model input.
        self.branch_miss_head = nn.Linear(d_dyn, 1)

    def forward_from_static(
        self,
        batch: Dict[str, torch.Tensor],
        token_static: torch.Tensor,
    ) -> Dict[str, torch.Tensor]:
        """Run full-QKVR interaction from already encoded functional tokens.

        The regular ``forward`` method delegates here.  Deployment inference
        caches only base/branch/resource token encodings; dynamic context is a
        separate input and is recomputed on every scheduler context.
        """
        h_static = self.static_enc.pool_tokens(token_static, batch["valid_uop_mask"])
        h_dyn = self.interaction(
            token_static,
            batch["dynamic_uop_fields"],
            batch["valid_uop_mask"],
            batch["chunk_summary"],
            batch["relation_features"],
            batch["uarch_features"],
            batch["n_uops"],
            batch["sample_ptr"],
        )
        log_cpi = self.log_cpi_head(h_dyn).squeeze(-1)
        pred_cpi = torch.exp(log_cpi)
        pred_delta = pred_cpi * batch["n_uops"].clamp(min=1.0)
        branch_miss_logit = self.branch_miss_head(h_dyn).squeeze(-1)
        return {
            "log_cpi": log_cpi,
            "pred_cpi": pred_cpi,
            "pred_delta_cycles": pred_delta,
            "branch_miss_logit": branch_miss_logit,
            "pred_branch_miss_prob": torch.sigmoid(branch_miss_logit),
            "h_static": h_static,
        }

    def forward(self, batch: Dict[str, torch.Tensor]) -> Dict[str, torch.Tensor]:
        # No timing-state keys are consumed here.  T/E/delta_hat are computed by
        # the external scheduler only after this forward returns.
        token_static = self.static_enc.encode_tokens(batch["per_uop_fields"])
        return self.forward_from_static(batch, token_static)


class StaticEmbeddingCache:
    """Inference-only cache keyed by trace/chunk/uarch/checkpoint."""

    def __init__(self, max_entries: int = 0) -> None:
        self.max_entries = int(max_entries)
        if self.max_entries < 0:
            raise ValueError("max_entries must be non-negative")
        self._cache: "OrderedDict[Tuple, torch.Tensor]" = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def key(
        self,
        trace_id: str,
        core_id: int,
        chunk_id: int,
        uarch_id: str = "default",
        checkpoint_id: str = "unversioned",
    ) -> Tuple:
        return (trace_id, core_id, chunk_id, uarch_id, checkpoint_id)

    def get(self, key: Tuple) -> Optional[torch.Tensor]:
        value = self._cache.get(key)
        if value is None:
            self.misses += 1
        else:
            self.hits += 1
            self._cache.move_to_end(key)
        return value

    def put(self, key: Tuple, value: torch.Tensor) -> None:
        self._cache[key] = value.detach()
        self._cache.move_to_end(key)
        if self.max_entries > 0:
            while len(self._cache) > self.max_entries:
                self._cache.popitem(last=False)
                self.evictions += 1

    def reset_counters(self) -> None:
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def clear(self) -> None:
        self._cache.clear()

    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return float(self.hits) / max(1, total)
