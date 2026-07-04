"""llm_wrapper.py — Qwen3-0.6B-Base + LoRA + per-core PMU 回归头。

前向流程：
  input_ids/attention_mask -> Qwen3 backbone (LoRA) -> last_hidden_state
  -> 按 query_pos 抽取每核 <QUERY_C{i}> 的 hidden
  -> 可选融合每核 <LOCAL_C{i}> hidden
  -> PMURegressionHead -> [B, n_core, K]

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
    cpi_head_mode: str = "direct"


class UopEncoder(nn.Module):
    """v9 composite-uop encoder: six discrete functional fields -> d_model."""

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
        self.head = PMURegressionHead(
            d_model, hidden=cfg.head_hidden,
            cpi_head_mode=cfg.cpi_head_mode).to(torch.bfloat16)
        self.uop_encoder = UopEncoder(
            d_model, field_dim=cfg.uop_field_dim).to(torch.bfloat16)
        self.side_proj = nn.Linear(cfg.side_feat_dim, d_model).to(torch.bfloat16)
        nn.init.zeros_(self.side_proj.weight)
        nn.init.zeros_(self.side_proj.bias)
        # v16: per-core local summary token gives the tail query a stable local
        # anchor. Zero init keeps old tail-query behavior at initialization.
        self.local_proj = nn.Linear(d_model, d_model).to(torch.bfloat16)
        nn.init.zeros_(self.local_proj.weight)
        nn.init.zeros_(self.local_proj.bias)
        # 跨核时间锚点：每核窗口相对 T_start(cycle) -> 连续特征注入 query hidden。
        # 输入先 log1p 归一化（数值范围大），再线性投影到 d_model。
        self.tstart_proj = nn.Linear(1, d_model).to(torch.bfloat16)
        nn.init.zeros_(self.tstart_proj.weight)
        nn.init.zeros_(self.tstart_proj.bias)

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

    def forward(self, input_ids, attention_mask, query_pos, t_start=None,
                is_uop=None, uop_fields=None, side_feats=None,
                local_pos=None,
                core_mask=None):
        """query_pos: [B, n_core] 每核 <QUERY_C{i}> token 在序列中的位置索引。
        local_pos: [B, n_core] 每核 <LOCAL_C{i}> token 位置；可选。
        t_start:   [B, n_core] 每核窗口相对起始时间(cycle)，可选；None 时不注入。
        """
        if uop_fields is not None and is_uop is not None:
            tok_emb = self.backbone.get_input_embeddings()(input_ids)
            safe_fields = uop_fields.clamp(min=0)
            uop_emb = self.uop_encoder(safe_fields).to(tok_emb.dtype)
            inputs_embeds = tok_emb.clone()
            mask = is_uop.to(torch.bool)
            inputs_embeds[mask] = uop_emb[mask]
            out = self.backbone(inputs_embeds=inputs_embeds,
                                attention_mask=attention_mask)
        else:
            out = self.backbone(input_ids=input_ids,
                                attention_mask=attention_mask)
        hs = out.last_hidden_state                  # [B, L, D]
        B, n_core = query_pos.shape
        idx = query_pos.unsqueeze(-1).expand(-1, -1, hs.size(-1))  # [B,nc,D]
        query_hidden = torch.gather(hs, 1, idx)     # [B, n_core, D]
        if local_pos is not None:
            lidx = local_pos.unsqueeze(-1).expand(-1, -1, hs.size(-1))
            local_hidden = torch.gather(hs, 1, lidx)
            query_hidden = query_hidden + self.local_proj(local_hidden)
        if t_start is not None:
            # log1p 压缩动态范围，再投影；零初始化保证训练起点等价于不注入。
            ts = torch.log1p(t_start.clamp(min=0).to(query_hidden.dtype))
            query_hidden = query_hidden + self.tstart_proj(ts.unsqueeze(-1))
        if side_feats is not None:
            sf = side_feats.to(query_hidden.dtype)
            query_hidden = query_hidden + self.side_proj(sf)
        return self.head(query_hidden, core_mask=core_mask)  # [B, n_core, K]

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
