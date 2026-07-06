"""Small causal Transformer backbone for the v25a self-trained baseline.

The class intentionally exposes the subset of the HuggingFace AutoModel API
used by LLMSimModel: forward(...).last_hidden_state, get_input_embeddings(),
resize_token_embeddings(), enable_input_require_grads(), and gradient
checkpointing toggles.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.utils.checkpoint


@dataclass
class TinyTransformerConfig:
    vocab_size: int
    d_model: int = 320
    n_layers: int = 8
    n_heads: int = 8
    ffn_dim: int = 1280
    max_len: int = 32768
    dropout: float = 0.1
    attn_dropout: float = 0.1
    pad_token_id: int = 0
    rope_theta: float = 10000.0


@dataclass
class TinyOutput:
    last_hidden_state: torch.Tensor

    def __getitem__(self, key: str) -> torch.Tensor:
        if key != "last_hidden_state":
            raise KeyError(key)
        return self.last_hidden_state


class RMSNorm(nn.Module):
    def __init__(self, dim: int, eps: float = 1.0e-6):
        super().__init__()
        self.weight = nn.Parameter(torch.ones(dim))
        self.eps = eps

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        orig_dtype = x.dtype
        xf = x.float()
        scale = torch.rsqrt(xf.pow(2).mean(dim=-1, keepdim=True) + self.eps)
        return (xf * scale).to(orig_dtype) * self.weight.to(orig_dtype)


def _rope_inv_freq(head_dim: int, theta: float) -> torch.Tensor:
    if head_dim % 2 != 0:
        raise ValueError("RoPE requires an even head_dim")
    idx = torch.arange(0, head_dim, 2, dtype=torch.float32)
    return 1.0 / (float(theta) ** (idx / float(head_dim)))


def _apply_rope(x: torch.Tensor, inv_freq: torch.Tensor) -> torch.Tensor:
    """Apply adjacent-pair RoPE to q/k: [B,H,L,D] -> [B,H,L,D]."""
    seq_len = x.size(-2)
    pos = torch.arange(seq_len, device=x.device, dtype=torch.float32)
    freqs = torch.outer(pos, inv_freq.to(device=x.device))  # [L,D/2]
    cos = freqs.cos().to(dtype=x.dtype)[None, None, :, :]
    sin = freqs.sin().to(dtype=x.dtype)[None, None, :, :]
    x_even = x[..., 0::2]
    x_odd = x[..., 1::2]
    rot_even = x_even * cos - x_odd * sin
    rot_odd = x_even * sin + x_odd * cos
    return torch.stack((rot_even, rot_odd), dim=-1).flatten(-2)


class TinySelfAttention(nn.Module):
    def __init__(self, cfg: TinyTransformerConfig):
        super().__init__()
        if cfg.d_model % cfg.n_heads != 0:
            raise ValueError("d_model must be divisible by n_heads")
        self.d_model = cfg.d_model
        self.n_heads = cfg.n_heads
        self.head_dim = cfg.d_model // cfg.n_heads
        self.attn_dropout = float(cfg.attn_dropout)
        self.qkv = nn.Linear(cfg.d_model, 3 * cfg.d_model, bias=False)
        self.o_proj = nn.Linear(cfg.d_model, cfg.d_model, bias=False)
        self.out_dropout = nn.Dropout(cfg.dropout)
        self.register_buffer("rope_inv_freq",
                             _rope_inv_freq(self.head_dim, cfg.rope_theta),
                             persistent=False)

    def forward(self, x: torch.Tensor,
                attention_mask: torch.Tensor | None = None) -> torch.Tensor:
        bsz, seq_len, _ = x.shape
        qkv = self.qkv(x)
        q, k, v = qkv.chunk(3, dim=-1)
        q = q.view(bsz, seq_len, self.n_heads, self.head_dim).transpose(1, 2)
        k = k.view(bsz, seq_len, self.n_heads, self.head_dim).transpose(1, 2)
        v = v.view(bsz, seq_len, self.n_heads, self.head_dim).transpose(1, 2)

        q = _apply_rope(q, self.rope_inv_freq)
        k = _apply_rope(k, self.rope_inv_freq)
        out = F.scaled_dot_product_attention(
            q, k, v,
            dropout_p=self.attn_dropout if self.training else 0.0,
            is_causal=True,
        )
        out = out.transpose(1, 2).contiguous().view(bsz, seq_len, self.d_model)
        out = self.o_proj(out)
        return self.out_dropout(out)


class TinyBlock(nn.Module):
    def __init__(self, cfg: TinyTransformerConfig):
        super().__init__()
        self.attn_norm = RMSNorm(cfg.d_model)
        self.attn = TinySelfAttention(cfg)
        self.ffn_norm = RMSNorm(cfg.d_model)
        self.ffn = nn.Sequential(
            nn.Linear(cfg.d_model, cfg.ffn_dim, bias=False),
            nn.GELU(),
            nn.Dropout(cfg.dropout),
            nn.Linear(cfg.ffn_dim, cfg.d_model, bias=False),
            nn.Dropout(cfg.dropout),
        )

    def forward(self, x: torch.Tensor,
                attention_mask: torch.Tensor | None = None) -> torch.Tensor:
        x = x + self.attn(self.attn_norm(x), attention_mask=attention_mask)
        x = x + self.ffn(self.ffn_norm(x))
        return x


class TinyTransformer(nn.Module):
    def __init__(self, cfg: TinyTransformerConfig):
        super().__init__()
        self.config = cfg
        self.token_emb = nn.Embedding(cfg.vocab_size, cfg.d_model,
                                      padding_idx=cfg.pad_token_id)
        self.drop = nn.Dropout(cfg.dropout)
        self.blocks = nn.ModuleList([TinyBlock(cfg) for _ in range(cfg.n_layers)])
        self.final_norm = RMSNorm(cfg.d_model)
        self.gradient_checkpointing = False
        self.checkpoint_use_reentrant = False
        self._init_weights()

    def _init_weights(self) -> None:
        residual_std = 0.02 / math.sqrt(2.0 * max(1, self.config.n_layers))
        nn.init.normal_(self.token_emb.weight, mean=0.0, std=0.02)
        if self.token_emb.padding_idx is not None:
            with torch.no_grad():
                self.token_emb.weight[self.token_emb.padding_idx].zero_()
        for module in self.modules():
            if isinstance(module, nn.Linear):
                nn.init.normal_(module.weight, mean=0.0, std=0.02)
                if module.bias is not None:
                    nn.init.zeros_(module.bias)
        for block in self.blocks:
            nn.init.normal_(block.attn.o_proj.weight, mean=0.0,
                            std=residual_std)
            nn.init.normal_(block.ffn[3].weight, mean=0.0, std=residual_std)

    def get_input_embeddings(self) -> nn.Embedding:
        return self.token_emb

    def resize_token_embeddings(self, new_size: int) -> nn.Embedding:
        old = self.token_emb
        old_size, dim = old.weight.shape
        if int(new_size) == int(old_size):
            return old
        new_emb = nn.Embedding(
            int(new_size), dim, padding_idx=old.padding_idx,
            device=old.weight.device, dtype=old.weight.dtype,
        )
        nn.init.normal_(new_emb.weight, mean=0.0, std=0.02)
        n = min(old_size, int(new_size))
        with torch.no_grad():
            new_emb.weight[:n].copy_(old.weight[:n])
            if new_emb.padding_idx is not None:
                new_emb.weight[new_emb.padding_idx].zero_()
        self.token_emb = new_emb
        self.config.vocab_size = int(new_size)
        return self.token_emb

    def enable_input_require_grads(self) -> None:
        return None

    def gradient_checkpointing_enable(self, gradient_checkpointing_kwargs=None,
                                      kwargs=None) -> None:
        opts = gradient_checkpointing_kwargs or kwargs or {}
        self.gradient_checkpointing = True
        self.checkpoint_use_reentrant = bool(opts.get("use_reentrant", False))

    def gradient_checkpointing_disable(self) -> None:
        self.gradient_checkpointing = False

    def forward(self, input_ids: torch.Tensor | None = None,
                inputs_embeds: torch.Tensor | None = None,
                attention_mask: torch.Tensor | None = None) -> TinyOutput:
        if inputs_embeds is None:
            if input_ids is None:
                raise ValueError("input_ids or inputs_embeds is required")
            x = self.token_emb(input_ids)
        else:
            x = inputs_embeds
        if attention_mask is not None:
            x = x * attention_mask.to(dtype=x.dtype).unsqueeze(-1)
        x = self.drop(x)
        for block in self.blocks:
            if self.gradient_checkpointing and self.training:
                x = torch.utils.checkpoint.checkpoint(
                    block, x, attention_mask,
                    use_reentrant=self.checkpoint_use_reentrant,
                )
            else:
                x = block(x, attention_mask=attention_mask)
        x = self.final_norm(x)
        if attention_mask is not None:
            x = x * attention_mask.to(dtype=x.dtype).unsqueeze(-1)
        return TinyOutput(last_hidden_state=x)

    def save_pretrained(self, path: str | Path) -> None:
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        with open(path / "config.json", "w") as f:
            json.dump(asdict(self.config), f, indent=2, sort_keys=True)
        torch.save(self.state_dict(), path / "pytorch_model.bin")

    @classmethod
    def from_pretrained(cls, path: str | Path,
                        map_location: str | torch.device = "cpu"):
        path = Path(path)
        with open(path / "config.json") as f:
            cfg = TinyTransformerConfig(**json.load(f))
        model = cls(cfg)
        sd = torch.load(path / "pytorch_model.bin", map_location=map_location)
        model.load_state_dict(sd)
        return model
