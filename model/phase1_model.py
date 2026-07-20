"""phase1_model.py — Phase 1 semantic-gate model.

Architecture (docs/LLM语义建模方案.md §3.2 + §6.2):

  input_ids -> Qwen2.5-Coder (LoRA Q/K/V/O) -> hidden -> pool per BB -> E_static_raw
  E_static  = MLP_static(E_static_raw)                                     # d_model
  E_dyn     = DynamicMacroEncoder(op_class, flags, uop_count)              # d_model
  E_macro   = LayerNorm(E_static[macro_bb_idx] + E_dyn)                    # d_model
  chunk_emb = masked_mean(E_macro over macro_valid_mask)                   # d_model
  pred_log_cpi = MLP_head(chunk_emb)                                       # scalar

Ablation switches:
  input="real"|"pseudo"|"shuffle"|"register_rename" — chooses the prompt variant
    (handled outside this module; the model just consumes tokenized ids).
  input="side_only" — skips LLM entirely, uses only E_dyn.

Loss: Huber on ``log(cpi_macro)`` (log-space regression matches TSim / TCSim).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F


N_OPCLASS = 128           # gem5 Enums::OpClass reserved space
N_FLAGS = 4096            # macro-flag bit-field bucket (12 bits used, safe cap)
N_UOP_COUNT = 64          # log2 bucketing for uop count / macro


@dataclass
class Phase1ModelConfig:
    base_model: str = "Qwen/Qwen2.5-Coder-1.5B-Instruct"
    lora_r: int = 32
    lora_alpha: int = 64
    lora_dropout: float = 0.05
    dyn_field_dim: int = 96
    d_model: Optional[int] = None      # inferred from backbone
    head_hidden: int = 256
    freeze_backbone: bool = False      # v22 semantics: LoRA-only trainable
    side_only: bool = False
    dtype: str = "bf16"


class DynamicMacroEncoder(nn.Module):
    """Embed macro-level dynamic aggregates into d_model."""

    def __init__(self, d_model: int, field_dim: int = 96):
        super().__init__()
        self.op = nn.Embedding(N_OPCLASS, field_dim)
        # macro flags are a bit-packed integer; we hash-bucket into N_FLAGS.
        self.fl = nn.Embedding(N_FLAGS, field_dim)
        # uop_count is a small positive integer; log2 bucket into N_UOP_COUNT.
        self.uc = nn.Embedding(N_UOP_COUNT, field_dim)
        self.proj = nn.Sequential(
            nn.LayerNorm(3 * field_dim),
            nn.Linear(3 * field_dim, d_model),
            nn.GELU(),
            nn.Linear(d_model, d_model),
        )

    @staticmethod
    def _bucket_log(x: torch.Tensor, cap: int) -> torch.Tensor:
        x = x.clamp_min(0).float()
        return torch.log2(x + 1).clamp(0, cap - 1).long()

    def forward(self, op_class: torch.Tensor, flags: torch.Tensor,
                uop_count: torch.Tensor) -> torch.Tensor:
        op = self.op(op_class.clamp(0, N_OPCLASS - 1))
        fl = self.fl((flags % N_FLAGS).clamp(0, N_FLAGS - 1))
        uc = self.uc(self._bucket_log(uop_count, N_UOP_COUNT))
        return self.proj(torch.cat([op, fl, uc], dim=-1))


class Phase1Model(nn.Module):
    def __init__(self, cfg: Phase1ModelConfig):
        super().__init__()
        self.cfg = cfg
        d_model = None
        self.backbone = None
        if not cfg.side_only:
            self._build_backbone()
            d_model = self.backbone.config.hidden_size
        if cfg.d_model is None:
            cfg.d_model = int(d_model or 1024)
        d = cfg.d_model
        if not cfg.side_only:
            self.static_proj = nn.Sequential(
                nn.LayerNorm(d_model),
                nn.Linear(d_model, d),
                nn.GELU(),
                nn.Linear(d, d),
            )
        self.dyn_enc = DynamicMacroEncoder(d_model=d, field_dim=cfg.dyn_field_dim)
        self.macro_norm = nn.LayerNorm(d)
        self.head = nn.Sequential(
            nn.LayerNorm(d),
            nn.Linear(d, cfg.head_hidden),
            nn.GELU(),
            nn.Linear(cfg.head_hidden, 1),
        )

    def _build_backbone(self):
        from transformers import AutoModelForCausalLM
        # Load in fp32 so backbone params, adapter params and non-LLM heads
        # share one dtype.  Actual bf16 compute happens under torch.autocast in
        # the training loop; this avoids dtype-mismatch RuntimeErrors when
        # fp32 head/dyn_enc layers meet bf16 hidden states inside gather/add.
        base = AutoModelForCausalLM.from_pretrained(
            self.cfg.base_model, torch_dtype=torch.float32,
        )
        base.config.output_hidden_states = True
        # Strip the LM head — we only need hidden states.
        if hasattr(base, "get_input_embeddings"):
            # Keep the token embedding module intact
            pass
        if self.cfg.freeze_backbone:
            for p in base.parameters():
                p.requires_grad_(False)
            self.backbone = base
            return
        # Attach LoRA (Q/K/V/O only per docs/LLM语义建模方案.md §6.2 option 2)
        try:
            from peft import LoraConfig, get_peft_model, TaskType
        except ImportError:
            raise RuntimeError(
                "peft is required for LoRA fine-tuning; install with 'pip install peft'"
            )
        lora_cfg = LoraConfig(
            r=self.cfg.lora_r,
            lora_alpha=self.cfg.lora_alpha,
            lora_dropout=self.cfg.lora_dropout,
            bias="none",
            task_type=TaskType.CAUSAL_LM,
            target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
        )
        # Freeze base weights; LoRA adapters remain trainable.
        for p in base.parameters():
            p.requires_grad_(False)
        self.backbone = get_peft_model(base, lora_cfg)

    def _pool_bb(self, hidden: torch.Tensor,
                 bb_pos: torch.Tensor, bb_mask: torch.Tensor) -> torch.Tensor:
        """Mean-pool ``hidden`` over each bb's [start,end] token span.

        hidden: [B, T, D]; bb_pos: [B, B_max, 2]; bb_mask: [B, B_max]
        returns: [B, B_max, D]

        Vectorized: builds a boolean [B, B_max, T] span mask on-device, then
        computes masked mean in one matmul-like pass.  Avoids the per-example
        Python loop that used to cost ~40 host<->device sync per step.
        """
        B, T, D = hidden.shape
        Bmax = bb_pos.shape[1]
        # arange indices [T] broadcast against [B, Bmax, 1] start/end.
        idx = torch.arange(T, device=hidden.device).view(1, 1, T)  # [1, 1, T]
        s = bb_pos[..., 0:1]                                        # [B, Bmax, 1]
        e = bb_pos[..., 1:2]                                        # [B, Bmax, 1]
        span = (idx >= s) & (idx <= e)                              # [B, Bmax, T]
        valid = bb_mask.bool().unsqueeze(-1)                        # [B, Bmax, 1]
        span = span & valid                                         # [B, Bmax, T]
        span_f = span.to(hidden.dtype)                              # [B, Bmax, T]
        # sum over T: [B, Bmax, T] @ [B, T, D] -> [B, Bmax, D]
        num = torch.bmm(span_f, hidden)
        den = span_f.sum(dim=-1, keepdim=True).clamp_min(1.0)       # [B, Bmax, 1]
        return num / den

    def forward(self, batch: Dict[str, torch.Tensor]) -> Dict[str, torch.Tensor]:
        device = batch["macro_bb_idx"].device
        macro_valid = batch["macro_valid_mask"].float()
        B = macro_valid.shape[0]
        d = self.cfg.d_model

        # 1) Dynamic macro embedding
        e_dyn = self.dyn_enc(
            batch["macro_op_class"],
            batch["macro_flags"],
            batch["macro_uop_count"],
        )  # [B, M, D]

        if self.cfg.side_only or self.backbone is None:
            e_macro = self.macro_norm(e_dyn)
        else:
            # 2) LLM encoding — always fetch hidden_states[-1] because the
            # AutoModelForCausalLM head returns [B,T,V] logits, not [B,T,D].
            out = self.backbone(
                input_ids=batch["input_ids"],
                attention_mask=batch["attention_mask"],
                output_hidden_states=True,
            )
            if hasattr(out, "hidden_states") and out.hidden_states is not None:
                hidden = out.hidden_states[-1]
            elif hasattr(out, "last_hidden_state"):
                hidden = out.last_hidden_state
            else:
                raise RuntimeError(
                    "backbone forward returned neither hidden_states nor "
                    "last_hidden_state; cannot pool E_static"
                )
            # autocast handles dtype; no manual cast needed
            e_static_raw = self._pool_bb(
                hidden, batch["bb_boundary_pos"], batch["bb_valid_mask"]
            )  # [B, B_max, D_llm]
            e_static = self.static_proj(e_static_raw)  # [B, B_max, D]
            # Gather per-macro E_static via macro_bb_idx
            bb_idx = batch["macro_bb_idx"].clamp(0, e_static.shape[1] - 1)
            gather_idx = bb_idx.unsqueeze(-1).expand(-1, -1, e_static.shape[-1])
            e_static_per_macro = torch.gather(e_static, dim=1, index=gather_idx)
            e_macro = self.macro_norm(e_static_per_macro + e_dyn)

        # 3) Chunk pool (masked mean over valid macros)
        m = macro_valid.unsqueeze(-1)
        num = (e_macro * m).sum(dim=1)
        den = m.sum(dim=1).clamp_min(1.0)
        chunk_emb = num / den

        pred = self.head(chunk_emb).squeeze(-1)  # log(cpi_macro)
        return {
            "pred_log_cpi_macro": pred,
        }


def phase1_loss(pred: torch.Tensor, target: torch.Tensor,
                valid: torch.Tensor, huber_delta: float = 0.3) -> torch.Tensor:
    """Huber on log(cpi_macro).  Ignores invalid rows."""
    m = valid.float()
    if m.sum() < 1:
        return pred.sum() * 0.0
    y_log = torch.log(target.clamp_min(1e-4))
    return (F.huber_loss(pred, y_log, delta=huber_delta, reduction="none") * m).sum() / m.sum().clamp_min(1.0)
