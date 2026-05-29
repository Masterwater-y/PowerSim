"""TAO-style multi-core micro-arch Transformer (V9.5).

参考：
  - TAO 论文 §7.1 两级 embedding：每个特征族独立 sub-embedding，再线性合并。
  - TAO 论文 §7.2 多头自注意力 encoder：因果 mask，6 层 × 8 头，d_model=256。
  - TAO 论文 §7.3 多任务头：fetch_lat / exec_lat 回归 + mispred / coh / path 分类。

特征族划分（与 ml/dataset.py 一致）：
  Family-1 OPCODE_LIKE   ：14 个 bool 旗位 + n_src/n_dst/size + macro_pc_id
  Family-2 REGISTER_DEP  ：4 路 producer dist + 4 路 producer class
  Family-3 MEM_COH       ：mesi_before / coh_oracle / sharer_bucket / owner_dist
                           / dirty_owner / path_class / inval_fanout / same_line_recent
                           / oracle_source + 3 路地址桶（vaddr/paddr/cline）
  Family-4 I_SIDE        ：i_path_class / i_coh_oracle / i_mesi_before / i_oracle_source

每族 d_feat=64，最终 4 × 64 = 256 → Linear → d_model=256。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict

import torch
import torch.nn as nn
import torch.nn.functional as F


@dataclass
class TaoConfig:
    d_model: int = 256
    d_feat: int = 64
    n_layer: int = 6
    n_head: int = 8
    d_ff: int = 1024
    dropout: float = 0.1
    context_len: int = 128
    macro_pc_vocab: int = 1024     # 由 dataset.num_features() 提供
    addr_bucket: int = 16
    dist_bucket: int = 9
    pc_vocab: int = 16
    mesi_vocab: int = 8
    coh_vocab: int = 8
    path_vocab: int = 8
    # 与方案 §5.2 对齐：仅 3 个输出头
    # 多任务损失权重
    w_fetch: float = 1.0
    w_exec: float = 1.0
    w_mispred: float = 0.5
    # V9.7 方案 B：fetch group head 辅助任务（推理时丢弃，仅用于 backbone 正则）
    w_head: float = 0.1
    # 正负失衡：pos_weight 由 dataset 统计后注入；focal 仅在正类极少时启用
    mispred_pos_weight: float = 1.0
    mispred_focal_gamma: float = 0.0    # 0 = 关闭 focal
    # head 任务：~12.5% 正例，需 pos_weight=7
    head_pos_weight: float = 7.0


# ============================================================ Embedding
class _OpcodeLike(nn.Module):
    """14 bool + n_src/n_dst/size + macro_pc_id -> d_feat。

    bool/小整数：单一 16 维 emb 后求和；macro_pc_id：独立 emb。
    """

    BOOL_KEYS = (
        'is_load', 'is_store', 'is_atomic', 'is_branch', 'is_branch_cond',
        'is_branch_indirect', 'is_call', 'is_return', 'is_int', 'is_fp',
        'is_simd', 'is_serialize', 'is_microop', 'is_last_microop',
    )
    SMALL_INT_KEYS = ('n_src', 'n_dst', 'size')   # 0..N

    def __init__(self, cfg: TaoConfig):
        super().__init__()
        self.cfg = cfg
        # bool flags：每位独立学习参数 (2 行 emb)，共 14 路 → 单矩阵 [14,2,d_feat//4]
        self.bool_emb = nn.Embedding(2 * len(self.BOOL_KEYS), cfg.d_feat // 4)
        # n_src/n_dst：[0..8] 已富余；size：[0..8]
        self.small_emb = nn.ModuleDict({
            'n_src': nn.Embedding(16, cfg.d_feat // 4),
            'n_dst': nn.Embedding(16, cfg.d_feat // 4),
            'size': nn.Embedding(16, cfg.d_feat // 4),
        })
        self.macro_pc_emb = nn.Embedding(cfg.macro_pc_vocab, cfg.d_feat)
        self.proj = nn.Linear(cfg.d_feat // 4 + cfg.d_feat // 4 * 3 + cfg.d_feat,
                              cfg.d_feat)
        self.ln = nn.LayerNorm(cfg.d_feat)

    def forward(self, feat: Dict[str, torch.Tensor]) -> torch.Tensor:
        # feat[k]: [B, N] long
        B, N = feat['is_load'].shape
        # bool: 把 14 个 bool 按 key index 映射到 emb 表的不同行
        offsets = torch.arange(len(self.BOOL_KEYS), device=feat['is_load'].device) * 2
        bools = torch.stack([feat[k] for k in self.BOOL_KEYS], dim=-1)  # [B,N,14]
        bools = (bools + offsets).long()                                # 0..27
        b = self.bool_emb(bools).sum(dim=-2)                            # [B,N,d/4]
        small = torch.cat([self.small_emb[k](feat[k].clamp(0, 15)) for k in self.SMALL_INT_KEYS], dim=-1)
        mpc = self.macro_pc_emb(feat['macro_pc_id'].clamp(0, self.cfg.macro_pc_vocab - 1))
        x = torch.cat([b, small, mpc], dim=-1)
        return self.ln(self.proj(x))


class _RegisterDep(nn.Module):
    """4 路 (dist_bucket, pc_vocab) -> d_feat。"""

    def __init__(self, cfg: TaoConfig):
        super().__init__()
        self.dist_emb = nn.Embedding(cfg.dist_bucket, cfg.d_feat // 4)
        self.pc_emb = nn.Embedding(cfg.pc_vocab, cfg.d_feat // 4)
        self.proj = nn.Linear(4 * (cfg.d_feat // 4 + cfg.d_feat // 4), cfg.d_feat)
        self.ln = nn.LayerNorm(cfg.d_feat)

    def forward(self, feat: Dict[str, torch.Tensor]) -> torch.Tensor:
        parts = []
        for i in range(4):
            d = self.dist_emb(feat[f'd{i}'].clamp(0, self.dist_emb.num_embeddings - 1))
            p = self.pc_emb(feat[f'pc{i}'].clamp(0, self.pc_emb.num_embeddings - 1))
            parts.append(torch.cat([d, p], dim=-1))
        x = torch.cat(parts, dim=-1)
        return self.ln(self.proj(x))


class _MemCoh(nn.Module):
    KEYS_SMALL = (
        'mesi_before', 'coh_oracle', 'sharer_bucket', 'owner_dist',
        'dirty_owner', 'path_class', 'inval_fanout', 'same_line_recent',
        'oracle_source',
    )
    KEYS_ADDR = ('vaddr_bucket', 'paddr_bucket', 'cline_bucket')

    def __init__(self, cfg: TaoConfig):
        super().__init__()
        # 各小整数特征统一用 16 行 emb 表（值域 < 16 时已富余，超过的会 clamp）
        self.smalls = nn.ModuleDict({
            k: nn.Embedding(16, cfg.d_feat // 4) for k in self.KEYS_SMALL
        })
        self.addr = nn.ModuleDict({
            k: nn.Embedding(cfg.addr_bucket, cfg.d_feat // 4) for k in self.KEYS_ADDR
        })
        d_in = (cfg.d_feat // 4) * (len(self.KEYS_SMALL) + len(self.KEYS_ADDR))
        self.proj = nn.Linear(d_in, cfg.d_feat)
        self.ln = nn.LayerNorm(cfg.d_feat)

    def forward(self, feat: Dict[str, torch.Tensor]) -> torch.Tensor:
        parts = []
        for k in self.KEYS_SMALL:
            parts.append(self.smalls[k](feat[k].clamp(0, 15)))
        for k in self.KEYS_ADDR:
            parts.append(self.addr[k](feat[k]))
        x = torch.cat(parts, dim=-1)
        return self.ln(self.proj(x))


class _ISide(nn.Module):
    KEYS = ('i_path_class', 'i_coh_oracle', 'i_mesi_before', 'i_oracle_source')

    def __init__(self, cfg: TaoConfig):
        super().__init__()
        self.embs = nn.ModuleDict({
            k: nn.Embedding(16, cfg.d_feat // 4) for k in self.KEYS
        })
        self.proj = nn.Linear((cfg.d_feat // 4) * len(self.KEYS), cfg.d_feat)
        self.ln = nn.LayerNorm(cfg.d_feat)

    def forward(self, feat: Dict[str, torch.Tensor]) -> torch.Tensor:
        x = torch.cat([self.embs[k](feat[k].clamp(0, 15)) for k in self.KEYS], dim=-1)
        return self.ln(self.proj(x))


class TwoLevelEmbedding(nn.Module):
    """L1 4 个特征族 + L2 线性合并 → d_model。"""

    def __init__(self, cfg: TaoConfig):
        super().__init__()
        self.f1 = _OpcodeLike(cfg)
        self.f2 = _RegisterDep(cfg)
        self.f3 = _MemCoh(cfg)
        self.f4 = _ISide(cfg)
        self.merge = nn.Linear(4 * cfg.d_feat, cfg.d_model)
        self.ln = nn.LayerNorm(cfg.d_model)

    def forward(self, feat: Dict[str, torch.Tensor]) -> torch.Tensor:
        x = torch.cat([self.f1(feat), self.f2(feat), self.f3(feat), self.f4(feat)], dim=-1)
        return self.ln(self.merge(x))


# ============================================================ Transformer Encoder（Pre-LN + causal）
class _MHA(nn.Module):
    def __init__(self, cfg: TaoConfig):
        super().__init__()
        self.h = cfg.n_head
        self.dh = cfg.d_model // cfg.n_head
        self.qkv = nn.Linear(cfg.d_model, 3 * cfg.d_model, bias=False)
        self.proj = nn.Linear(cfg.d_model, cfg.d_model, bias=False)
        self.dropout = cfg.dropout

    def forward(self, x: torch.Tensor, key_pad_mask: torch.Tensor) -> torch.Tensor:
        # x: [B, N, D]，key_pad_mask: [B, N] True=valid
        B, N, D = x.shape
        qkv = self.qkv(x).reshape(B, N, 3, self.h, self.dh).permute(2, 0, 3, 1, 4)
        q, k, v = qkv[0], qkv[1], qkv[2]                       # [B, h, N, dh]
        # padding mask: [B,1,1,N]，True 表示需要被屏蔽（PyTorch 约定）
        pad = ~key_pad_mask[:, None, None, :]
        # causal: [N,N] True 表示屏蔽
        causal = torch.triu(torch.ones(N, N, device=x.device, dtype=torch.bool), diagonal=1)
        attn_mask = pad | causal
        out = F.scaled_dot_product_attention(
            q, k, v, attn_mask=~attn_mask,                      # SDPA: True=keep
            dropout_p=self.dropout if self.training else 0.0,
            is_causal=False,
        )
        out = out.transpose(1, 2).reshape(B, N, D)
        return self.proj(out)


class _Block(nn.Module):
    def __init__(self, cfg: TaoConfig):
        super().__init__()
        self.ln1 = nn.LayerNorm(cfg.d_model)
        self.attn = _MHA(cfg)
        self.ln2 = nn.LayerNorm(cfg.d_model)
        self.ff = nn.Sequential(
            nn.Linear(cfg.d_model, cfg.d_ff),
            nn.GELU(),
            nn.Dropout(cfg.dropout),
            nn.Linear(cfg.d_ff, cfg.d_model),
        )
        self.drop = nn.Dropout(cfg.dropout)

    def forward(self, x, mask):
        x = x + self.drop(self.attn(self.ln1(x), mask))
        x = x + self.drop(self.ff(self.ln2(x)))
        return x


class _PosEmb(nn.Module):
    def __init__(self, cfg: TaoConfig):
        super().__init__()
        self.pe = nn.Embedding(cfg.context_len, cfg.d_model)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        B, N, D = x.shape
        idx = torch.arange(N, device=x.device).unsqueeze(0).expand(B, N)
        return x + self.pe(idx)


# ============================================================ Multi-task heads
class _Heads(nn.Module):
    """方案 §5.2：(fetch_latency, execution_latency, mispredicted)。

    两个非负回归头（ReLU），一个二分类头（sigmoid，训练用 logits + BCE）。
    V9.7 方案 B：新增 head_logit 辅助二分类（fetch group head 标识，
    detailed-only label，推理时不使用，仅用于训练时让 backbone 学到
    fetch group 边界结构）。
    """

    def __init__(self, cfg: TaoConfig):
        super().__init__()
        d = cfg.d_model
        self.fetch = nn.Linear(d, 1)
        self.execlat = nn.Linear(d, 1)
        self.mispred = nn.Linear(d, 1)
        # V9.7 方案 B：head 辅助分类头
        self.head = nn.Linear(d, 1)

    def forward(self, h: torch.Tensor) -> Dict[str, torch.Tensor]:
        # h: [B, D] - 锚点位置（最后一个 token）
        return {
            'fetch_lat': F.relu(self.fetch(h).squeeze(-1)),
            'exec_lat': F.relu(self.execlat(h).squeeze(-1)),
            'mispred_logit': self.mispred(h).squeeze(-1),
            # V9.7 方案 B：head logit（仅训练 loss，不用于推理）
            'head_logit': self.head(h).squeeze(-1),
        }


# ============================================================ Top model
class TaoCoreTransformer(nn.Module):
    def __init__(self, cfg: TaoConfig):
        super().__init__()
        self.cfg = cfg
        self.embed = TwoLevelEmbedding(cfg)
        self.posemb = _PosEmb(cfg)
        self.blocks = nn.ModuleList([_Block(cfg) for _ in range(cfg.n_layer)])
        self.ln_f = nn.LayerNorm(cfg.d_model)
        self.heads = _Heads(cfg)
        self.apply(self._init_weights)

    @staticmethod
    def _init_weights(m):
        if isinstance(m, nn.Linear):
            nn.init.normal_(m.weight, std=0.02)
            if m.bias is not None:
                nn.init.zeros_(m.bias)
        elif isinstance(m, nn.Embedding):
            nn.init.normal_(m.weight, std=0.02)

    def forward(self, batch: Dict) -> Dict[str, torch.Tensor]:
        feat: Dict[str, torch.Tensor] = batch['feat']
        mask: torch.Tensor = batch['attn_mask']             # [B,N] bool, True=valid
        x = self.embed(feat)                                 # [B,N,D]
        x = self.posemb(x)
        for blk in self.blocks:
            x = blk(x, mask)
        x = self.ln_f(x)
        h_anchor = x[:, -1, :]                               # 锚点位置
        return self.heads(h_anchor)

    def compute_loss(self, batch: Dict, out: Dict) -> Dict[str, torch.Tensor]:
        c = self.cfg
        mse_f = F.mse_loss(out['fetch_lat'], batch['fetch_lat'])
        mse_e = F.mse_loss(out['exec_lat'], batch['exec_lat'])
        # mispred: pos_weight 处理失衡；可选 focal 抑制易分负样本
        logit = out['mispred_logit']
        target = batch['mispred']
        pw = torch.tensor(c.mispred_pos_weight, device=logit.device, dtype=logit.dtype)
        if c.mispred_focal_gamma > 0.0:
            # focal-BCE：(1-p_t)^gamma * BCE，且 pos 仍乘 pos_weight
            p = torch.sigmoid(logit)
            pt = target * p + (1 - target) * (1 - p)
            ce = F.binary_cross_entropy_with_logits(logit, target, reduction='none',
                                                   pos_weight=pw)
            bce_m = ((1 - pt).clamp(min=1e-6) ** c.mispred_focal_gamma * ce).mean()
        else:
            bce_m = F.binary_cross_entropy_with_logits(logit, target, pos_weight=pw)
        # V9.7 方案 B：head 辅助 loss（仅当 batch 中有 head label 时启用）
        bce_h = torch.tensor(0.0, device=logit.device, dtype=logit.dtype)
        if 'head' in batch and 'head_logit' in out:
            head_pw = torch.tensor(c.head_pos_weight, device=logit.device,
                                    dtype=logit.dtype)
            bce_h = F.binary_cross_entropy_with_logits(
                out['head_logit'], batch['head'], pos_weight=head_pw)
        total = (c.w_fetch * mse_f + c.w_exec * mse_e
                 + c.w_mispred * bce_m + c.w_head * bce_h)
        return {
            'loss': total,
            'mse_fetch': mse_f.detach(), 'mse_exec': mse_e.detach(),
            'bce_mispred': bce_m.detach(),
            'bce_head': bce_h.detach(),
        }

    def num_params(self) -> int:
        return sum(p.numel() for p in self.parameters())
