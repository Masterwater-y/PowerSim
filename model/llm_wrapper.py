"""llm_wrapper.py — Qwen3 backbone + LoRA + per-core PMU regression head.

前向流程：
  input_ids/attention_mask -> Qwen3 backbone (LoRA) -> last_hidden_state
  -> 按 query_pos 抽取每核 <QUERY_C{i}> 的 hidden -> PMURegressionHead -> [B, n_core, K]

只训练：LoRA 适配器 + 回归头；冻结 backbone 主体与 LM head。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

import torch
import torch.nn as nn

from .regression_head import PMURegressionHead, K, PMU_KEYS, KEY_SPACE
from . import tokenizer as tk


@dataclass
class WrapperConfig:
    base_model: str = "Qwen/Qwen3-0.6B-Base"
    lora_r: int = 32
    lora_alpha: int = 64
    lora_dropout: float = 0.05
    head_hidden: int = 256
    max_len: int = 8192
    uop_field_dim: int = 128
    side_feat_dim: int = len(tk.SIDE_FEATURE_KEYS)
    side_hidden: int = 256
    side_dropout: float = 0.05
    side_gamma_init: float = 0.1
    attn_feat_dim: int = len(tk.ATTN_FEATURE_KEYS)


class UopEncoder(nn.Module):
    """Composite-uop encoder: six discrete functional fields -> d_model."""

    def __init__(self, d_model: int, field_dim: int = 128):
        super().__init__()
        self.op = nn.Embedding(tk.N_OPCLASS, field_dim)
        self.rg = nn.Embedding(tk.N_REG_BUCKET, field_dim)
        self.mk = nn.Embedding(tk.N_MEMKIND, field_dim)
        self.rd = nn.Embedding(tk.N_RD, field_dim)
        self.st = nn.Embedding(tk.N_STRIDE, field_dim)
        self.br = nn.Embedding(tk.N_BR, field_dim)
        in_dim = 6 * field_dim
        self.base = nn.Linear(in_dim, d_model)
        self.mlp = nn.Sequential(
            nn.LayerNorm(in_dim),
            nn.Linear(in_dim, 4 * field_dim),
            nn.GELU(),
            nn.Linear(4 * field_dim, d_model),
        )
        # Start as a stable linear field combiner; let the MLP learn residual
        # interactions after the main path is already usable.
        nn.init.zeros_(self.mlp[-1].weight)
        nn.init.zeros_(self.mlp[-1].bias)

    def forward(self, fields: torch.Tensor) -> torch.Tensor:
        fields = fields.long()
        op, rg, mk, rd, st, br = fields.unbind(dim=-1)
        x = torch.cat([
            self.op(op.clamp(0, tk.N_OPCLASS - 1)),
            self.rg(rg.clamp(0, tk.N_REG_BUCKET - 1)),
            self.mk(mk.clamp(0, tk.N_MEMKIND - 1)),
            self.rd(rd.clamp(0, tk.N_RD - 1)),
            self.st(st.clamp(0, tk.N_STRIDE - 1)),
            self.br(br.clamp(0, tk.N_BR - 1)),
        ], dim=-1)
        return self.base(x) + self.mlp(x)


class AttentionFeatureEncoder(nn.Module):
    """Continuous functional feature token encoder.

    Each <GF_*>/<CF_*> sequence position is represented as feature identity
    plus a small value-dependent residual. This makes cross-core pressure
    visible to transformer attention instead of only to the final query head.
    """

    def __init__(self, d_model: int, n_features: int):
        super().__init__()
        self.name = nn.Embedding(n_features, d_model)
        self.value_mlp = nn.Sequential(
            nn.LayerNorm(2),
            nn.Linear(2, min(256, d_model)),
            nn.GELU(),
            nn.Linear(min(256, d_model), d_model),
        )
        # Stable start: the token identity is useful immediately; the numeric
        # value contribution is learned as a residual.
        nn.init.zeros_(self.value_mlp[-1].weight)
        nn.init.zeros_(self.value_mlp[-1].bias)

    def forward(self, feat_ids: torch.Tensor,
                feat_values: torch.Tensor) -> torch.Tensor:
        ids = feat_ids.long().clamp(0, len(tk.ATTN_FEATURE_KEYS) - 1)
        v = feat_values.to(self.name.weight.dtype)
        signed_log = torch.sign(v) * torch.log1p(torch.abs(v))
        x = torch.stack([v, signed_log], dim=-1)
        return self.name(ids) + self.value_mlp(x).to(self.name.weight.dtype)


class LLMSimModel(nn.Module):
    def __init__(self, cfg: WrapperConfig, hf_tokenizer):
        super().__init__()
        from transformers import AutoModel
        from peft import LoraConfig, get_peft_model

        self.cfg = cfg
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        backbone = AutoModel.from_pretrained(
            cfg.base_model, torch_dtype=torch.bfloat16,
            attn_implementation="sdpa")
        # 注入自定义 token 后 resize embedding
        backbone.resize_token_embeddings(len(hf_tokenizer))
        d_model = backbone.config.hidden_size
        # 长序列(L=32768)激活显存是瓶颈：开梯度检查点，用算力换显存。
        backbone.config.use_cache = False
        backbone.gradient_checkpointing_enable(
            gradient_checkpointing_kwargs={"use_reentrant": False})
        backbone.enable_input_require_grads()

        lora_cfg = LoraConfig(
            r=cfg.lora_r, lora_alpha=cfg.lora_alpha,
            lora_dropout=cfg.lora_dropout,
            target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
            bias="none", task_type="FEATURE_EXTRACTION",
        )
        self.backbone = get_peft_model(backbone, lora_cfg)
        # 只训练新增 token 的 embedding 行；原始 token 行用梯度 mask 冻结。
        self._unfreeze_new_embeddings(len(hf_tokenizer))
        self.head = PMURegressionHead(d_model, hidden=cfg.head_hidden).to(
            torch.bfloat16)
        self.uop_encoder = UopEncoder(
            d_model, field_dim=cfg.uop_field_dim).to(torch.bfloat16)
        self.attn_feat_encoder = AttentionFeatureEncoder(
            d_model, cfg.attn_feat_dim).to(torch.bfloat16)
        self.side_proj = nn.Linear(cfg.side_feat_dim, d_model).to(torch.bfloat16)
        self.side_mlp = nn.Sequential(
            nn.LayerNorm(cfg.side_feat_dim),
            nn.Linear(cfg.side_feat_dim, cfg.side_hidden),
            nn.GELU(),
            nn.Dropout(cfg.side_dropout),
            nn.Linear(cfg.side_hidden, d_model),
        ).to(torch.bfloat16)
        self.side_gate = nn.Sequential(
            nn.LayerNorm(cfg.side_feat_dim),
            nn.Linear(cfg.side_feat_dim, max(16, cfg.side_hidden // 2)),
            nn.GELU(),
            nn.Linear(max(16, cfg.side_hidden // 2), d_model),
            nn.Sigmoid(),
        ).to(torch.bfloat16)
        self.side_gamma = nn.Parameter(
            torch.tensor(float(cfg.side_gamma_init), dtype=torch.float32)
        )
        nn.init.zeros_(self.side_proj.weight)
        nn.init.zeros_(self.side_proj.bias)
        nn.init.zeros_(self.side_mlp[-1].weight)
        nn.init.zeros_(self.side_mlp[-1].bias)
        nn.init.zeros_(self.side_gate[-2].weight)
        nn.init.zeros_(self.side_gate[-2].bias)

    def _unfreeze_new_embeddings(self, vocab_size: int):
        """只让新增的 ~2k 个 token 行可训练，原始 ~15 万行通过 backward hook 把
        梯度置零（永不更新）。这样优化器虽持有整张 embedding，但只有新 token 真正变化。
        """
        n_new = len(tk.all_special_tokens())
        new_start = vocab_size - n_new          # 新 token 占据词表最高的一段连续 id
        self.new_token_start = new_start
        self.n_new_tokens = n_new

        emb = self.backbone.get_input_embeddings()
        emb.weight.requires_grad_(True)
        self.input_embedding = emb

        def _mask_old_rows(grad):
            grad = grad.clone()
            grad[:new_start] = 0
            return grad

        emb.weight.register_hook(_mask_old_rows)

    def forward(self, input_ids, attention_mask, query_pos,
                is_uop=None, uop_fields=None, side_feats=None,
                is_attn_feat=None, attn_feat_ids=None,
                attn_feat_values=None):
        """query_pos: [B, n_core] 每核 <QUERY_C{i}> token 在序列中的位置索引。"""
        need_embeds = (
            (uop_fields is not None and is_uop is not None)
            or (is_attn_feat is not None and attn_feat_ids is not None
                and attn_feat_values is not None)
        )
        if need_embeds:
            tok_emb = self.backbone.get_input_embeddings()(input_ids)
            inputs_embeds = tok_emb.clone()
            if uop_fields is not None and is_uop is not None:
                safe_fields = uop_fields.clamp(min=0)
                uop_emb = self.uop_encoder(safe_fields).to(tok_emb.dtype)
                mask = is_uop.to(torch.bool)
                inputs_embeds[mask] = uop_emb[mask]
            if (is_attn_feat is not None and attn_feat_ids is not None
                    and attn_feat_values is not None):
                feat_mask = is_attn_feat.to(torch.bool)
                if feat_mask.any():
                    feat_emb = self.attn_feat_encoder(
                        attn_feat_ids[feat_mask],
                        attn_feat_values[feat_mask],
                    ).to(tok_emb.dtype)
                    inputs_embeds[feat_mask] = feat_emb
            out = self.backbone(inputs_embeds=inputs_embeds,
                                attention_mask=attention_mask)
        else:
            out = self.backbone(input_ids=input_ids,
                                attention_mask=attention_mask)
        hs = out.last_hidden_state                  # [B, L, D]
        B, n_core = query_pos.shape
        idx = query_pos.unsqueeze(-1).expand(-1, -1, hs.size(-1))  # [B,nc,D]
        query_hidden = torch.gather(hs, 1, idx)     # [B, n_core, D]
        if side_feats is not None:
            sf = side_feats.to(query_hidden.dtype)
            side_linear = self.side_proj(sf)
            side_delta = self.side_mlp(sf)
            side_gate = self.side_gate(sf)
            side_residual = self.side_gamma.to(query_hidden.dtype) \
                * side_gate * side_delta
            query_hidden = query_hidden + side_linear + side_residual
        return self.head(query_hidden)              # [B, n_core, K]

    def trainable_parameters(self):
        return [p for p in self.parameters() if p.requires_grad]


def build_tokenizer(base_model: str = "Qwen/Qwen3-0.6B-Base"):
    from transformers import AutoTokenizer
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    tok = AutoTokenizer.from_pretrained(base_model)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    tk.inject_into_hf_tokenizer(tok)
    return tok
