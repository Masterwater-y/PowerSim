"""v28.1 four-branch functional-only fixed-chunk timing model."""
from __future__ import annotations

from collections import OrderedDict
from contextlib import nullcontext
import math
from typing import Dict, List, Optional, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.attention import SDPBackend, sdpa_kernel

from ..chunker.functional_features import (
    CHUNK_SUMMARY_NAMES,
    DYNAMIC_FIELD_SIZES,
    FIELD_GROUP_INDICES,
    FIELD_SIZES,
    RELATION_FEATURE_NAMES,
    UARCH_FEATURE_NAMES,
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

    def _cross_attention(
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

    def forward(
        self,
        x: torch.Tensor,
        valid_uop_mask: torch.Tensor,
        sample_ptr: torch.Tensor,
        cross_gate: torch.Tensor,
    ) -> torch.Tensor:
        h = self.attn_norm(x)
        q = self.q_proj(h)
        r = self.r_proj(h)
        k = self.k_proj(h)
        v = self.v_proj(h)
        mask = valid_uop_mask.bool()
        local_ctx = self._attend(q, k, v, mask)
        cross_ctx = self._cross_attention(r, k, v, mask, sample_ptr)
        if cross_gate.shape != (x.shape[0], self.d_dyn):
            raise ValueError(
                f"cross_gate shape {tuple(cross_gate.shape)} != {(x.shape[0], self.d_dyn)}"
            )
        attended = self.local_o_proj(local_ctx)
        attended = attended + cross_gate.unsqueeze(1) * self.cross_o_proj(cross_ctx)
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
