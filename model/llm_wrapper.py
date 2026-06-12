"""llm_wrapper.py — Qwen3-0.6B-Base + LoRA + per-core PMU 回归头。

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

    def forward(self, input_ids, attention_mask, query_pos):
        """query_pos: [B, n_core] 每核 <QUERY_C{i}> token 在序列中的位置索引。"""
        out = self.backbone(input_ids=input_ids,
                            attention_mask=attention_mask)
        hs = out.last_hidden_state                  # [B, L, D]
        B, n_core = query_pos.shape
        idx = query_pos.unsqueeze(-1).expand(-1, -1, hs.size(-1))  # [B,nc,D]
        query_hidden = torch.gather(hs, 1, idx)     # [B, n_core, D]
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
