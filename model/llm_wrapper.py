"""llm_wrapper.py — Qwen3-0.6B-Base + LoRA + per-core PMU 回归头。

前向流程：
  input_ids/attention_mask -> Qwen3 backbone (LoRA) -> last_hidden_state
  -> 按 query_pos 抽取每核 <QUERY_C{i}> 的 hidden
  -> 可选融合每核 <LOCAL_C{i}> hidden
  -> 可选 cross-core adapter
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
    model_input_mode: str = "global"
    core_adapter_layers: int = 0
    core_adapter_heads: int = 8
    core_adapter_ff_mult: int = 2
    core_adapter_dropout: float = 0.05


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


class CoreAdapterBlock(nn.Module):
    """Mask-aware residual self-attention over active cores.

    The attention output projection and FFN final projection are zero-initialized
    so enabling the adapter starts as an identity mapping and can be warm-started
    from older checkpoints without immediately perturbing CPI predictions.
    """

    def __init__(self, d_model: int, n_heads: int,
                 ff_mult: int = 2, dropout: float = 0.05):
        super().__init__()
        if d_model % n_heads != 0:
            raise ValueError(
                f"core adapter heads must divide d_model: "
                f"d_model={d_model} heads={n_heads}"
            )
        self.ln_attn = nn.LayerNorm(d_model)
        self.attn = nn.MultiheadAttention(
            d_model, n_heads, dropout=dropout, batch_first=True)
        self.dropout = nn.Dropout(dropout)
        self.ln_ff = nn.LayerNorm(d_model)
        self.ff = nn.Sequential(
            nn.Linear(d_model, ff_mult * d_model),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(ff_mult * d_model, d_model),
        )
        nn.init.zeros_(self.attn.out_proj.weight)
        nn.init.zeros_(self.attn.out_proj.bias)
        nn.init.zeros_(self.ff[-1].weight)
        nn.init.zeros_(self.ff[-1].bias)

    def forward(self, x: torch.Tensor,
                core_mask: torch.Tensor | None = None) -> torch.Tensor:
        key_padding_mask = None
        if core_mask is not None:
            key_padding_mask = ~core_mask.to(torch.bool)
        h = self.ln_attn(x)
        attn_out, _ = self.attn(
            h, h, h, key_padding_mask=key_padding_mask, need_weights=False)
        x = x + self.dropout(attn_out)
        x = x + self.dropout(self.ff(self.ln_ff(x)))
        if core_mask is not None:
            x = x * core_mask.to(x.dtype).unsqueeze(-1)
        return x


class CoreAdapter(nn.Module):
    def __init__(self, d_model: int, n_layers: int, n_heads: int,
                 ff_mult: int = 2, dropout: float = 0.05):
        super().__init__()
        self.layers = nn.ModuleList([
            CoreAdapterBlock(d_model, n_heads, ff_mult, dropout)
            for _ in range(max(0, int(n_layers)))
        ])

    def forward(self, x: torch.Tensor,
                core_mask: torch.Tensor | None = None) -> torch.Tensor:
        for layer in self.layers:
            x = layer(x, core_mask)
        return x


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
        if cfg.core_adapter_layers > 0:
            self.core_adapter = CoreAdapter(
                d_model,
                n_layers=cfg.core_adapter_layers,
                n_heads=cfg.core_adapter_heads,
                ff_mult=cfg.core_adapter_ff_mult,
                dropout=cfg.core_adapter_dropout,
            ).to(torch.bfloat16)
        else:
            self.core_adapter = None

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

    def _backbone_forward(self, input_ids, attention_mask,
                          is_uop=None, uop_fields=None):
        if uop_fields is not None and is_uop is not None:
            tok_emb = self.backbone.get_input_embeddings()(input_ids)
            safe_fields = uop_fields.clamp(min=0)
            uop_emb = self.uop_encoder(safe_fields).to(tok_emb.dtype)
            inputs_embeds = tok_emb.clone()
            mask = is_uop.to(torch.bool)
            inputs_embeds[mask] = uop_emb[mask]
            return self.backbone(inputs_embeds=inputs_embeds,
                                 attention_mask=attention_mask)
        return self.backbone(input_ids=input_ids,
                             attention_mask=attention_mask)

    def _forward_global(self, input_ids, attention_mask, query_pos,
                        is_uop=None, uop_fields=None,
                        local_pos=None) -> torch.Tensor:
        out = self._backbone_forward(
            input_ids, attention_mask, is_uop=is_uop, uop_fields=uop_fields)
        hs = out.last_hidden_state                  # [B, L, D]
        idx = query_pos.unsqueeze(-1).expand(-1, -1, hs.size(-1))
        query_hidden = torch.gather(hs, 1, idx)     # [B, n_core, D]
        if local_pos is not None:
            lidx = local_pos.unsqueeze(-1).expand(-1, -1, hs.size(-1))
            local_hidden = torch.gather(hs, 1, lidx)
            query_hidden = query_hidden + self.local_proj(local_hidden)
        return query_hidden

    def _forward_local_core(self, local_input_ids, local_attention_mask,
                            local_query_pos, core_mask,
                            local_is_uop=None,
                            local_uop_fields=None) -> torch.Tensor:
        if local_input_ids is None:
            raise ValueError(
                "model_input_mode=local_core requires local_input_ids")
        B, n_core, L = local_input_ids.shape
        valid = core_mask.to(torch.bool)
        flat_ids = local_input_ids[valid]
        flat_attn = local_attention_mask[valid]
        flat_pos = local_query_pos[valid]
        flat_is_uop = local_is_uop[valid] if local_is_uop is not None else None
        flat_uop_fields = (
            local_uop_fields[valid] if local_uop_fields is not None else None
        )
        out = self._backbone_forward(
            flat_ids, flat_attn, is_uop=flat_is_uop,
            uop_fields=flat_uop_fields)
        hs = out.last_hidden_state
        idx = flat_pos.unsqueeze(-1).unsqueeze(-1).expand(-1, 1, hs.size(-1))
        local_hidden = torch.gather(hs, 1, idx).squeeze(1)
        query_hidden = torch.zeros(
            (B, n_core, hs.size(-1)), device=hs.device, dtype=hs.dtype)
        query_hidden[valid] = local_hidden
        # Use the existing zero-initialized local projection as an identity
        # residual path, so DDP still sees the parameter in local-core mode.
        query_hidden = query_hidden + self.local_proj(query_hidden)
        query_hidden = query_hidden * core_mask.to(query_hidden.dtype).unsqueeze(-1)
        return query_hidden

    def forward(self, input_ids, attention_mask, query_pos, t_start=None,
                is_uop=None, uop_fields=None, side_feats=None,
                local_pos=None,
                core_mask=None,
                local_input_ids=None, local_attention_mask=None,
                local_query_pos=None, local_is_uop=None,
                local_uop_fields=None):
        """query_pos: [B, n_core] 每核 <QUERY_C{i}> token 在序列中的位置索引。
        local_pos: [B, n_core] 每核 <LOCAL_C{i}> token 位置；可选。
        t_start:   [B, n_core] 每核窗口相对起始时间(cycle)，可选；None 时不注入。
        """
        if core_mask is None:
            core_mask = torch.ones_like(query_pos, dtype=torch.float32)
        if self.cfg.model_input_mode == "local_core":
            query_hidden = self._forward_local_core(
                local_input_ids, local_attention_mask, local_query_pos,
                core_mask, local_is_uop=local_is_uop,
                local_uop_fields=local_uop_fields)
        else:
            query_hidden = self._forward_global(
                input_ids, attention_mask, query_pos,
                is_uop=is_uop, uop_fields=uop_fields, local_pos=local_pos)
        if t_start is not None:
            # log1p 压缩动态范围，再投影；零初始化保证训练起点等价于不注入。
            ts = torch.log1p(t_start.clamp(min=0).to(query_hidden.dtype))
            query_hidden = query_hidden + self.tstart_proj(ts.unsqueeze(-1))
        if side_feats is not None:
            sf = side_feats.to(query_hidden.dtype)
            query_hidden = query_hidden + self.side_proj(sf)
        if self.core_adapter is not None:
            query_hidden = self.core_adapter(query_hidden, core_mask)
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
