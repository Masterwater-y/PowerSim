"""Doc-style Q/K/V/R model for the clean v26 TSim path.

This module intentionally avoids HF tokenizers and LLM-style special tokens.
It consumes structured tensors:

  uop_fields  [B, C, L, F]
  uop_mask    [B, C, L]
  core_mask   [B, C]
  side_feats  [B, C, S]
  global_feats[B, G]

The default implementation consumes the clean 14-field v26 UOP schema. The
encoder can still instantiate a 6-field shape for local probes, but new v26
training requires rebuilt windows/cache with the 14-field schema.

The attention block follows docs/2026.7.6LLMSim.md: every UOP position computes
Q/K/V for same-core self attention and a separate R projection for cross-core
attention over other cores' K/V. The final per-core PMU prediction pools the
contextualized UOP states.
"""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from typing import Optional

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.attention import SDPBackend, sdpa_kernel

from . import tokenizer as tk
V26_PMU_KEYS = [
    "cpi_uop",
    "branch_miss",
    "l1d_ld_miss",
    "l1d_st_miss",
    "l2_ld_miss",
    "l2_st_miss",
    "llc_miss",
    "dtlb_miss",
]
V26_COUNT_KEYS = V26_PMU_KEYS[1:]
V26_K = len(V26_PMU_KEYS)


class V26PMUHead(nn.Module):
    """CPI log-ratio + bounded count/rate PMU head."""

    def __init__(self, d_model: int, hidden: int = 256):
        super().__init__()
        self.cpi_head = nn.Sequential(
            nn.LayerNorm(d_model),
            nn.Linear(d_model, hidden),
            nn.GELU(),
            nn.Linear(hidden, 1),
        )
        self.rate_head = nn.Sequential(
            nn.LayerNorm(d_model),
            nn.Linear(d_model, hidden),
            nn.GELU(),
            nn.Linear(hidden, len(V26_COUNT_KEYS)),
        )

    def forward(self, h: torch.Tensor) -> torch.Tensor:
        log_cpi = self.cpi_head(h)
        rates = torch.sigmoid(self.rate_head(h))
        return torch.cat([log_cpi, rates], dim=-1)


@dataclass
class V26KVQRConfig:
    d_model: int = 320
    field_dim: int = 96
    n_heads: int = 8
    n_layers: int = 4
    ffn_dim: int = 1280
    head_hidden: int = 256
    side_feat_dim: int = len(tk.SIDE_FEATURE_KEYS)
    global_feat_dim: int = 13
    max_uops_per_core: int = 32768
    dropout: float = 0.1
    uop_field_count: int = tk.V26_UOP_FIELD_COUNT
    attention_impl: str = "ragged_sdpa"
    sdpa_backend: str = "auto"


class StructuredUopEncoder(nn.Module):
    """Structured functional UOP fields -> model vector.

    Field order:
      base v9 six fields,
      pc_bucket, macro_pos_bucket, line_hash_bucket, line_role_bucket,
      same_core_hist_bucket, xcore_mem_bucket, coherence_bucket, fanout_bucket.
    """

    def __init__(self, d_model: int, field_dim: int = 96,
                 field_count: int = tk.V26_UOP_FIELD_COUNT):
        super().__init__()
        if field_count not in (tk.V9_UOP_FIELD_COUNT, tk.V26_UOP_FIELD_COUNT):
            raise ValueError(
                "v26 supports either 6-field local probes or the clean "
                f"{tk.V26_UOP_FIELD_COUNT}-field schema; got {field_count}"
            )
        self.field_count = int(field_count)
        self.field_sizes = list(tk.V26_FIELD_SIZES[:self.field_count])
        self.embs = nn.ModuleList([
            nn.Embedding(size, field_dim) for size in self.field_sizes
        ])
        in_dim = self.field_count * field_dim
        self.proj = nn.Sequential(
            nn.LayerNorm(in_dim),
            nn.Linear(in_dim, 2 * d_model),
            nn.GELU(),
            nn.Linear(2 * d_model, d_model),
        )

    def forward(self, fields: torch.Tensor) -> torch.Tensor:
        fields = fields.long()
        if fields.shape[-1] != self.field_count:
            raise ValueError(
                f"expected {self.field_count} UOP fields, got "
                f"{fields.shape[-1]}"
            )
        parts = []
        for idx, emb in enumerate(self.embs):
            parts.append(emb(fields[..., idx].clamp(0, self.field_sizes[idx] - 1)))
        x = torch.cat(parts, dim=-1)
        return self.proj(x)


class QKVRBlock(nn.Module):
    """One packed doc-style block: local self attention + cross-core R attention."""

    def __init__(self, cfg: V26KVQRConfig):
        super().__init__()
        if cfg.d_model % cfg.n_heads != 0:
            raise ValueError("d_model must be divisible by n_heads")
        self.d_model = int(cfg.d_model)
        self.n_heads = int(cfg.n_heads)
        self.head_dim = self.d_model // self.n_heads
        self.attn_norm = nn.LayerNorm(cfg.d_model)
        self.q_proj = nn.Linear(cfg.d_model, cfg.d_model, bias=False)
        self.r_proj = nn.Linear(cfg.d_model, cfg.d_model, bias=False)
        self.k_proj = nn.Linear(cfg.d_model, cfg.d_model, bias=False)
        self.v_proj = nn.Linear(cfg.d_model, cfg.d_model, bias=False)
        self.o_proj = nn.Linear(cfg.d_model, cfg.d_model, bias=False)
        self.attention_impl = str(getattr(cfg, "attention_impl", "ragged_sdpa"))
        self.sdpa_backend = str(getattr(cfg, "sdpa_backend", "auto")).lower()
        self.attn_dropout = nn.Dropout(cfg.dropout)
        self.ffn_norm = nn.LayerNorm(cfg.d_model)
        self.ffn = nn.Sequential(
            nn.Linear(cfg.d_model, cfg.ffn_dim),
            nn.GELU(),
            nn.Dropout(cfg.dropout),
            nn.Linear(cfg.ffn_dim, cfg.d_model),
            nn.Dropout(cfg.dropout),
        )

    def _split_heads(self, x: torch.Tensor) -> torch.Tensor:
        # [N,D] -> [1,H,N,Dh]
        N, D = x.shape
        if D != self.d_model:
            raise ValueError(f"expected dim={self.d_model}, got {D}")
        return x.view(N, self.n_heads, self.head_dim).transpose(0, 1).unsqueeze(0)

    def _merge_heads(self, x: torch.Tensor) -> torch.Tensor:
        # [1,H,N,Dh] -> [N,D]
        _, H, N, Dh = x.shape
        return x.squeeze(0).transpose(0, 1).contiguous().view(N, H * Dh)

    def _sdpa_context(self):
        if self.sdpa_backend == "auto":
            return nullcontext()
        if self.sdpa_backend == "flash":
            return sdpa_kernel([
                SDPBackend.CUDNN_ATTENTION,
                SDPBackend.FLASH_ATTENTION,
            ], set_priority=True)
        if self.sdpa_backend == "no_flash":
            return sdpa_kernel([
                SDPBackend.EFFICIENT_ATTENTION,
                SDPBackend.MATH,
            ], set_priority=True)
        if self.sdpa_backend == "math":
            return sdpa_kernel(SDPBackend.MATH)
        if self.sdpa_backend == "efficient":
            return sdpa_kernel([
                SDPBackend.EFFICIENT_ATTENTION,
                SDPBackend.MATH,
            ], set_priority=True)
        raise ValueError(f"unsupported sdpa_backend={self.sdpa_backend}")

    def _attend_local(self, q: torch.Tensor, k: torch.Tensor,
                      v: torch.Tensor,
                      segments: list[tuple[int, int, int, int]]) -> torch.Tensor:
        # q/k/v [N,D]. Each segment is one active core's real UOP range.
        if self.attention_impl not in {"ragged_sdpa", "sdpa"}:
            raise ValueError(f"unsupported attention_impl={self.attention_impl}")
        out = q.new_zeros(q.shape)
        with self._sdpa_context():
            for _, _, start, end in segments:
                if end <= start:
                    continue
                ctx = F.scaled_dot_product_attention(
                    self._split_heads(q[start:end]),
                    self._split_heads(k[start:end]),
                    self._split_heads(v[start:end]),
                    attn_mask=None,
                    dropout_p=0.0,
                    is_causal=False,
                )
                out[start:end] = self._merge_heads(ctx)
        return out

    def _attend_cross(self, r: torch.Tensor, k: torch.Tensor,
                      v: torch.Tensor,
                      segments: list[tuple[int, int, int, int]],
                      sample_segments: list[list[int]]) -> torch.Tensor:
        # r/k/v [N,D]. Each target core attends same-sample, other-core UOPs.
        out = k.new_zeros(k.shape)
        with self._sdpa_context():
            for seg_idx, (bi, _, start, end) in enumerate(segments):
                if end <= start:
                    continue
                k_parts = []
                v_parts = []
                for other_idx in sample_segments[bi]:
                    if other_idx == seg_idx:
                        continue
                    _, _, other_start, other_end = segments[other_idx]
                    if other_end <= other_start:
                        continue
                    k_parts.append(k[other_start:other_end])
                    v_parts.append(v[other_start:other_end])
                if not k_parts:
                    # Single-active-core samples have no cross-core context,
                    # but r_proj must still participate in the graph for DDP.
                    out[start:end] = r[start:end] * 0.0
                    continue
                kj = torch.cat(k_parts, dim=0)
                vj = torch.cat(v_parts, dim=0)
                ctx = F.scaled_dot_product_attention(
                    self._split_heads(r[start:end]),
                    self._split_heads(kj),
                    self._split_heads(vj),
                    attn_mask=None,
                    dropout_p=0.0,
                    is_causal=False,
                )
                out[start:end] = self._merge_heads(ctx)
        return out

    def forward(self, x: torch.Tensor,
                segments: list[tuple[int, int, int, int]],
                sample_segments: list[list[int]]) -> torch.Tensor:
        if x.numel() == 0:
            return x
        h = self.attn_norm(x)
        q = self.q_proj(h)
        r = self.r_proj(h)
        k = self.k_proj(h)
        v = self.v_proj(h)
        local_ctx = self._attend_local(q, k, v, segments)
        cross_ctx = self._attend_cross(r, k, v, segments, sample_segments)
        x = x + self.attn_dropout(self.o_proj(local_ctx + cross_ctx))
        x = x + self.ffn(self.ffn_norm(x))
        return x


class V26KVQRModel(nn.Module):
    """v26 doc-style Q/K/V/R implementation with per-core PMU readout."""

    def __init__(self, cfg: V26KVQRConfig):
        super().__init__()
        if cfg.d_model % cfg.n_heads != 0:
            raise ValueError("d_model must be divisible by n_heads")
        self.cfg = cfg
        self.d_model = int(cfg.d_model)

        self.uop_encoder = StructuredUopEncoder(
            cfg.d_model, field_dim=cfg.field_dim,
            field_count=cfg.uop_field_count,
        )
        self.pos_emb = nn.Embedding(cfg.max_uops_per_core, cfg.d_model)
        self.role_uop = nn.Parameter(torch.zeros(cfg.d_model))
        core_feat_dim = int(cfg.side_feat_dim) + 4
        global_feat_dim = int(cfg.global_feat_dim) + 2
        self.core_feat_proj = nn.Sequential(
            nn.LayerNorm(core_feat_dim),
            nn.Linear(core_feat_dim, cfg.d_model),
            nn.GELU(),
            nn.Linear(cfg.d_model, cfg.d_model),
        )
        self.global_feat_proj = nn.Sequential(
            nn.LayerNorm(global_feat_dim),
            nn.Linear(global_feat_dim, cfg.d_model),
            nn.GELU(),
            nn.Linear(cfg.d_model, cfg.d_model),
        )
        self.drop = nn.Dropout(cfg.dropout)
        self.blocks = nn.ModuleList([
            QKVRBlock(cfg) for _ in range(int(cfg.n_layers))
        ])
        self.pool_norm = nn.LayerNorm(cfg.d_model)
        self.head = V26PMUHead(cfg.d_model, hidden=cfg.head_hidden)

    def forward(self, uop_fields: torch.Tensor, uop_mask: torch.Tensor,
                core_mask: torch.Tensor, side_feats: torch.Tensor,
                global_feats: Optional[torch.Tensor] = None) -> torch.Tensor:
        B, C, L, F = uop_fields.shape
        if F != self.cfg.uop_field_count:
            raise ValueError(
                f"expected {self.cfg.uop_field_count} UOP fields, got {F}"
            )
        if L > self.cfg.max_uops_per_core:
            raise ValueError(
                f"L={L} exceeds max_uops_per_core={self.cfg.max_uops_per_core}"
            )

        device = self.role_uop.device
        dtype = next(self.parameters()).dtype
        uop_fields = uop_fields.to(device=device)
        uop_mask = uop_mask.to(device=device)
        core_mask = core_mask.to(device=device)
        side_feats = side_feats.to(device=device, dtype=dtype)
        if global_feats is None:
            global_feats = torch.zeros(
                (B, self.cfg.global_feat_dim), device=device, dtype=dtype)
        else:
            global_feats = global_feats.to(device=device, dtype=dtype)
        if side_feats.shape[-1] != self.cfg.side_feat_dim:
            raise ValueError(
                f"expected side_feat_dim={self.cfg.side_feat_dim}, "
                f"got {side_feats.shape[-1]}"
            )
        if global_feats.shape[-1] != self.cfg.global_feat_dim:
            raise ValueError(
                f"expected global_feat_dim={self.cfg.global_feat_dim}, "
                f"got {global_feats.shape[-1]}"
            )

        valid = (
            uop_mask.to(torch.bool)
            & core_mask.to(torch.bool).unsqueeze(-1)
        )
        lengths_t = valid.sum(dim=-1)
        n_core = core_mask.to(torch.bool).sum(dim=1).to(dtype).clamp(min=1.0)
        total_uops = lengths_t.to(dtype).sum(dim=1).clamp(min=1.0)

        log_n_core = torch.log1p(n_core)
        log_total_uops = torch.log1p(total_uops)
        log_core_uops = torch.log1p(lengths_t.to(dtype))
        core_share = lengths_t.to(dtype) / total_uops[:, None]
        core_extra = torch.stack([
            log_n_core[:, None].expand(B, C),
            log_total_uops[:, None].expand(B, C),
            log_core_uops,
            core_share,
        ], dim=-1)
        core_input = torch.cat([side_feats, core_extra], dim=-1)
        core_cond = self.core_feat_proj(core_input)
        core_cond = core_cond * core_mask.to(dtype).unsqueeze(-1)

        global_extra = torch.stack([log_n_core, log_total_uops], dim=-1)
        global_input = torch.cat([global_feats, global_extra], dim=-1)
        global_cond = self.global_feat_proj(global_input)

        token_index = valid.nonzero(as_tuple=False)
        segments: list[tuple[int, int, int, int]] = []
        sample_segments: list[list[int]] = [[] for _ in range(B)]
        cursor = 0
        lengths_cpu = lengths_t.detach().cpu()
        for bi in range(B):
            for ci in range(C):
                li = int(lengths_cpu[bi, ci])
                if li <= 0:
                    continue
                seg_idx = len(segments)
                segments.append((bi, ci, cursor, cursor + li))
                sample_segments[bi].append(seg_idx)
                cursor += li

        if token_index.shape[0] != cursor:
            raise RuntimeError(
                f"packed token count mismatch: nonzero={token_index.shape[0]} "
                f"segments={cursor}"
            )

        token_fields = uop_fields[valid]
        h = self.uop_encoder(token_fields).to(dtype)
        pos = token_index[:, 2].clamp(max=self.cfg.max_uops_per_core - 1)
        h = h + self.pos_emb(pos).to(dtype)
        h = h + self.role_uop.to(dtype)[None, :]
        h = h + core_cond[token_index[:, 0], token_index[:, 1]]
        h = h + global_cond[token_index[:, 0]]
        h = self.drop(h)
        for block in self.blocks:
            h = block(h, segments, sample_segments)

        pooled = h.new_zeros((B, C, self.d_model))
        for bi, ci, start, end in segments:
            if end <= start:
                continue
            pooled[bi, ci] = h[start:end].mean(dim=0)
        pooled = pooled + core_cond + global_cond[:, None, :]
        pooled = self.pool_norm(pooled)
        pooled = pooled * core_mask.to(dtype).unsqueeze(-1)
        return self.head(pooled)

    def trainable_parameters(self):
        return [p for p in self.parameters() if p.requires_grad]
