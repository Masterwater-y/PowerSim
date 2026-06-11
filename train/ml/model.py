"""TAO-style multi-core micro-arch Transformer (V9.5).

参考：
  - TAO 论文 §7.1 两级 embedding：每个特征族独立 sub-embedding，再线性合并。
  - TAO 论文 §7.2 多头自注意力 encoder：因果 mask，6 层 × 8 头，d_model=256。
  - TAO 论文 §7.3 多任务头：fetch_lat / exec_lat 回归 + mispred / coh / path 分类。

特征族划分（与 ml/dataset.py 一致）：
  Family-1 OPCODE_LIKE   ：14 个 bool 旗位 + n_src/n_dst/size
  Family-2 REGISTER_DEP  ：4 路 producer dist + 4 路 producer class
  Family-3 MEM_COH       ：mesi_before / coh_oracle / sharer_bucket / owner_dist
                           / dirty_owner / path_class / inval_fanout / same_line_recent
                           / oracle_source + 4 路地址桶
                           （vaddr / paddr / cline (vaddr-line) / cline_p (paddr-line)）
  I-side oracle/cache fields are intentionally not modeled: functional retired
  traces cannot reconstruct the real ifetch cacheline stream.  Only relative
  frontend structure derived from macro_pc order is kept in OPCODE_LIKE.

每族 d_feat=64，最终 5 × 64 → Linear → d_model=256。
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
    addr_bucket: int = 16
    dist_bucket: int = 9
    pc_vocab: int = 16
    mesi_vocab: int = 8
    coh_vocab: int = 8
    path_vocab: int = 8
    # 多任务损失权重
    # fetch 采用 zero-inflated / hurdle 方案：
    #   - w_fetch: 仅在 head=1 上回归正分支幅度
    #   - w_fetch_cons: 用 sigmoid(head) * fetch_pos 约束最终软门控值
    w_fetch: float = 1.0
    w_fetch_cons: float = 0.25
    w_fetch_tail_bce: float = 0.15
    w_fetch_tail_mae: float = 0.05
    w_fetch_decomp: float = 0.1
    w_fetch_decomp_cons: float = 0.0
    w_exec: float = 1.0
    w_exec_bucket: float = 0.25
    w_exec_residual: float = 0.5
    w_exec_quantile: float = 0.25
    w_tail_bce: float = 0.15
    w_tail_mae: float = 0.05
    w_mispred: float = 0.5
    # V9.7 方案 B：fetch group head 辅助任务（推理时丢弃，仅用于 backbone 正则）
    w_head: float = 0.1
    # 正负失衡：pos_weight 由 dataset 统计后注入；focal 仅在正类极少时启用
    mispred_pos_weight: float = 1.0
    mispred_focal_gamma: float = 0.0    # 0 = 关闭 focal
    # head 任务：~12.5% 正例，需 pos_weight=7
    head_pos_weight: float = 7.0
    fetch_tail_p95_pos_weight: float = 19.0
    fetch_tail_p99_pos_weight: float = 99.0
    tail_p95_pos_weight: float = 19.0
    tail_p99_pos_weight: float = 99.0
    exec_huber_delta: float = 0.25
    exec_bucket_count: int = 16
    exec_bucket_cycle_max: float = 262144.0


def build_exec_bucket_centers(cfg: TaoConfig) -> torch.Tensor:
    edges = torch.linspace(
        0.0, math.log1p(cfg.exec_bucket_cycle_max), cfg.exec_bucket_count + 1)
    return 0.5 * (edges[:-1] + edges[1:])


def pinball_loss(pred: torch.Tensor, target: torch.Tensor, q: float) -> torch.Tensor:
    err = target - pred
    return torch.maximum((q - 1.0) * err, q * err).mean()


# ============================================================ Embedding
class _OpcodeLike(nn.Module):
    """结构化 opcode 特征 -> d_feat。

    仅保留相对结构信号（如 is_macro_head / uop_pos_in_macro），
    不接受 macro_pc_id 这类绝对 PC identifier。
    """

    BOOL_KEYS = (
        'is_load', 'is_store', 'is_atomic', 'is_branch', 'is_branch_cond',
        'is_branch_indirect', 'is_call', 'is_return', 'is_int', 'is_fp',
        'is_simd', 'is_serialize', 'is_microop', 'is_last_microop',
        'is_macro_head', 'i_group_head',
    )
    SMALL_INT_KEYS = ('n_src', 'n_dst', 'size', 'uop_pos_in_macro',
                      'i_group_pos')

    def __init__(self, cfg: TaoConfig):
        super().__init__()
        self.cfg = cfg
        # bool flags：每位独立学习参数 (2 行 emb)，共 N 路 → 单矩阵 [N,2,d_feat//4]
        self.bool_emb = nn.Embedding(2 * len(self.BOOL_KEYS), cfg.d_feat // 4)
        # n_src/n_dst/size/uop_pos_in_macro 统一走 16 行 emb；uop_pos 做 0..15 clamp。
        self.small_emb = nn.ModuleDict({
            k: nn.Embedding(16, cfg.d_feat // 4) for k in self.SMALL_INT_KEYS
        })
        proj_in = cfg.d_feat // 4 + cfg.d_feat // 4 * len(self.SMALL_INT_KEYS)
        self.proj = nn.Linear(proj_in, cfg.d_feat)
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
        x = torch.cat([b, small], dim=-1)
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
        # P0-A d-side（值域：mshr/bank 0..15、tlb 0/1、walker 0..7）
        'd_mshr_depth', 'dtlb_hit', 'd_walker_levels',
        'd_walker_dram_misses', 'd_bank_id',
        # V10.3 A d-side（LLC set residency / lru_pos，值域 0..31，clamp 0..15
        # 进 16-行 emb；超过 15 的高层挤压一档，保持族口径一致）
        'd_llc_set_residency', 'd_llc_set_lru_pos',
    )
    KEYS_ADDR = ('vaddr_bucket', 'paddr_bucket', 'cline_bucket', 'cline_p_bucket')
    # V10 方案 B：cline_p_bucket = paddr-line 桶（cacheline_paddr 真值）。
    # COMPAT-OLD-50M: 旧数据无该列时，dataset 端会回退到 cacheline_addr
    # 等同 cline_bucket，模型仍可正常前向（多一组与 cline_bucket 同分布的 emb）。
    # 全 V10+ 重采后 cline_p_bucket 才会与 cline_bucket 在 alias / 共享内存等
    # 场景上分化。

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


class _CtxWindow(nn.Module):
    """P1-C 上下文窗口派生（packer 离线生成、严格因果）。

    包含 4 个计数列（W=64 → 0..64，clamp 到 0..63 进 64-行 emb）和
    3 个对数列（0..15 进 16-行 emb）。embedding-only，与其它族同维度合并。
    """

    KEYS_64 = ('mem_density_W64', 'branch_density_W64',
               'unique_cl_W64', 'pc_freq_W64', 'bank_conflict_W64')
    KEYS_16 = ('cl_reuse_dist_log', 'time_since_last_branch_log')

    def __init__(self, cfg: TaoConfig):
        super().__init__()
        self.embs64 = nn.ModuleDict({
            k: nn.Embedding(64, cfg.d_feat // 4) for k in self.KEYS_64
        })
        self.embs16 = nn.ModuleDict({
            k: nn.Embedding(16, cfg.d_feat // 4) for k in self.KEYS_16
        })
        d_in = (cfg.d_feat // 4) * (len(self.KEYS_64) + len(self.KEYS_16))
        self.proj = nn.Linear(d_in, cfg.d_feat)
        self.ln = nn.LayerNorm(cfg.d_feat)

    def forward(self, feat: Dict[str, torch.Tensor]) -> torch.Tensor:
        parts = []
        for k in self.KEYS_64:
            parts.append(self.embs64[k](feat[k].clamp(0, 63)))
        for k in self.KEYS_16:
            parts.append(self.embs16[k](feat[k].clamp(0, 15)))
        x = torch.cat(parts, dim=-1)
        return self.ln(self.proj(x))


class _DramFeats(nn.Module):
    """V10.3 B + C：长窗口 unique_cl + DRAM bank/row 派生。

    B 字段：
      - unique_cl_W256  cap=255 → clamp 0..255 进 256-行 emb
      - unique_cl_W1024 cap=2047 → log2 化（int(log2(x+1))）后 0..11 进 16-行 emb
        （避免 2048 行 embedding 过大；log 后保留稀疏/密集对比即可）
    C 字段：
      - dram_bank_id           0..15 → 16-行 emb
      - dram_bank_freq_W256    cap=255 → clamp 0..255 进 256-行 emb
      - dram_row_freq_W256     cap=255 → clamp 0..255 进 256-行 emb
    """

    KEYS_256 = ('unique_cl_W256', 'dram_bank_freq_W256',
                'dram_row_freq_W256')
    KEYS_16  = ('dram_bank_id',)

    def __init__(self, cfg: TaoConfig):
        super().__init__()
        self.embs256 = nn.ModuleDict({
            k: nn.Embedding(256, cfg.d_feat // 4) for k in self.KEYS_256
        })
        self.embs16 = nn.ModuleDict({
            k: nn.Embedding(16, cfg.d_feat // 4) for k in self.KEYS_16
        })
        # unique_cl_W1024 单独 log 桶
        self.emb_w1024_log = nn.Embedding(16, cfg.d_feat // 4)
        d_in = (cfg.d_feat // 4) * (len(self.KEYS_256) + len(self.KEYS_16) + 1)
        self.proj = nn.Linear(d_in, cfg.d_feat)
        self.ln = nn.LayerNorm(cfg.d_feat)

    def forward(self, feat: Dict[str, torch.Tensor]) -> torch.Tensor:
        parts = []
        for k in self.KEYS_256:
            parts.append(self.embs256[k](feat[k].clamp(0, 255)))
        for k in self.KEYS_16:
            parts.append(self.embs16[k](feat[k].clamp(0, 15)))
        # unique_cl_W1024 -> log2(x+1) clamp 0..15
        x_w1024 = feat['unique_cl_W1024'].clamp(min=0).float()
        log_w1024 = torch.log2(x_w1024 + 1.0).long().clamp(0, 15)
        parts.append(self.emb_w1024_log(log_w1024))
        x = torch.cat(parts, dim=-1)
        return self.ln(self.proj(x))


class TwoLevelEmbedding(nn.Module):
    """L1 5 个特征族 + L2 线性合并 → d_model。"""

    def __init__(self, cfg: TaoConfig):
        super().__init__()
        self.f1 = _OpcodeLike(cfg)
        self.f2 = _RegisterDep(cfg)
        self.f3 = _MemCoh(cfg)
        # P1-C：上下文窗口派生作为第 5 族
        self.f5 = _CtxWindow(cfg)
        # V10.3 B+C：长窗口 unique_cl + DRAM bank/row 派生作为第 6 族
        self.f6 = _DramFeats(cfg)
        self.merge = nn.Linear(5 * cfg.d_feat, cfg.d_model)
        self.ln = nn.LayerNorm(cfg.d_model)

    def forward(self, feat: Dict[str, torch.Tensor]) -> torch.Tensor:
        x = torch.cat([self.f1(feat), self.f2(feat), self.f3(feat),
                       self.f5(feat), self.f6(feat)], dim=-1)
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
        # padding mask: [B,1,1,N]，True 表示 key 需要被屏蔽。
        pad = ~key_pad_mask[:, None, None, :]
        # causal: [N,N] True 表示未来 key 需要被屏蔽。
        causal = torch.triu(torch.ones(N, N, device=x.device, dtype=torch.bool), diagonal=1)
        keep = ~(pad | causal)                                  # SDPA: True=keep
        # 左 padding 的 query 可能被 pad+causal 屏蔽到整行无有效 key；
        # 为 padding query 补充对角线 self-attend，避免 softmax 全 -inf。
        query_is_pad = ~key_pad_mask[:, None, :, None]
        diag = torch.eye(N, device=x.device, dtype=torch.bool)[None, None, :, :]
        keep = keep | (query_is_pad & diag)
        out = F.scaled_dot_product_attention(
            q, k, v, attn_mask=keep,
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


class _Heads(nn.Module):
    """多任务头。

    fetch 采用 zero-inflated / hurdle 形式：
      - head_logit 表示当前 retired row 是否承接 frontend gap
      - fetch_lat 回归正分支幅度
      - soft gate 路径用 sigmoid(head_logit) * fetch_pos 约束有效 fetch
    """

    def __init__(self, cfg: TaoConfig):
        super().__init__()
        d = cfg.d_model
        self.fetch = nn.Linear(d, 1)
        self.fetch_base = nn.Linear(d, 1)
        self.fetch_after_mispred = nn.Linear(d, 1)
        self.fetch_residual_tail = nn.Linear(d, 1)
        self.fetch_tail_p95 = nn.Linear(d, 1)
        self.fetch_tail_p99 = nn.Linear(d, 1)
        self.exec_bucket = nn.Linear(d, cfg.exec_bucket_count)
        self.exec_residual = nn.Linear(d, 1)
        self.exec_quantiles = nn.Linear(d, 3)
        self.exec_tail_p95 = nn.Linear(d, 1)
        self.exec_tail_p99 = nn.Linear(d, 1)
        self.mispred = nn.Linear(d, 1)
        # V9.7 方案 B：head 辅助分类头
        self.head = nn.Linear(d, 1)
        self.register_buffer('exec_bucket_centers',
                             build_exec_bucket_centers(cfg))

    def forward(self, h: torch.Tensor) -> Dict[str, torch.Tensor]:
        # h: [B, D] - 锚点位置（最后一个 token）
        fetch_lat = F.relu(self.fetch(h).squeeze(-1))
        head_logit = self.head(h).squeeze(-1)
        head_prob = torch.sigmoid(head_logit)
        exec_bucket_logits = self.exec_bucket(h)
        exec_bucket_prob = torch.softmax(exec_bucket_logits, dim=-1)
        exec_residual = self.exec_residual(h).squeeze(-1)
        exec_bucket_mean = torch.sum(
            exec_bucket_prob * self.exec_bucket_centers.unsqueeze(0), dim=-1)
        exec_lat = torch.clamp(exec_bucket_mean + exec_residual, min=0.0)
        q_raw = self.exec_quantiles(h)
        exec_q50 = F.relu(q_raw[:, 0])
        exec_q90 = exec_q50 + F.relu(q_raw[:, 1])
        exec_q99 = exec_q90 + F.relu(q_raw[:, 2])
        return {
            'fetch_lat': fetch_lat,
            'fetch_lat_soft': head_prob * fetch_lat,
            'fetch_base': F.relu(self.fetch_base(h).squeeze(-1)),
            'fetch_after_mispred': F.relu(self.fetch_after_mispred(h).squeeze(-1)),
            'fetch_residual_tail': F.relu(self.fetch_residual_tail(h).squeeze(-1)),
            'fetch_tail_p95_logit': self.fetch_tail_p95(h).squeeze(-1),
            'fetch_tail_p99_logit': self.fetch_tail_p99(h).squeeze(-1),
            'exec_lat': exec_lat,
            'exec_bucket_logits': exec_bucket_logits,
            'exec_residual': exec_residual,
            'exec_quantiles': torch.stack([exec_q50, exec_q90, exec_q99], dim=-1),
            'exec_tail_p95_logit': self.exec_tail_p95(h).squeeze(-1),
            'exec_tail_p99_logit': self.exec_tail_p99(h).squeeze(-1),
            'mispred_logit': self.mispred(h).squeeze(-1),
            'head_logit': head_logit,
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
        zero = torch.tensor(0.0, device=out['exec_lat'].device,
                            dtype=out['exec_lat'].dtype)

        def effective_fetch_raw() -> torch.Tensor:
            fetch_pos = torch.expm1(torch.clamp(
                out['fetch_lat'], min=0.0, max=math.log1p(c.exec_bucket_cycle_max)))
            gate = torch.sigmoid(out['head_logit'])
            return fetch_pos * gate

        huber_e = F.huber_loss(out['exec_lat'], batch['exec_lat'],
                               delta=c.exec_huber_delta)
        ce_exec_bucket = F.cross_entropy(out['exec_bucket_logits'],
                                         batch['exec_bucket'])
        huber_exec_res = F.huber_loss(out['exec_residual'],
                                      batch['exec_residual'],
                                      delta=c.exec_huber_delta)
        q = out['exec_quantiles']
        pinball_exec_q = (
            pinball_loss(q[:, 0], batch['exec_lat'], 0.5)
            + pinball_loss(q[:, 1], batch['exec_lat'], 0.9)
            + pinball_loss(q[:, 2], batch['exec_lat'], 0.99)
        ) / 3.0
        bce_tail = torch.tensor(0.0, device=out['exec_lat'].device,
                                dtype=out['exec_lat'].dtype)
        tail_raw_mae = torch.tensor(0.0, device=out['exec_lat'].device,
                                    dtype=out['exec_lat'].dtype)
        if 'exec_tail_p95' in batch and 'exec_tail_p95_logit' in out:
            p95_pw = torch.tensor(c.tail_p95_pos_weight, device=out['exec_lat'].device,
                                  dtype=out['exec_lat'].dtype)
            p99_pw = torch.tensor(c.tail_p99_pos_weight, device=out['exec_lat'].device,
                                  dtype=out['exec_lat'].dtype)
            bce_p95 = F.binary_cross_entropy_with_logits(
                out['exec_tail_p95_logit'], batch['exec_tail_p95'], pos_weight=p95_pw)
            bce_p99 = F.binary_cross_entropy_with_logits(
                out['exec_tail_p99_logit'], batch['exec_tail_p99'], pos_weight=p99_pw)
            bce_tail = 0.5 * (bce_p95 + bce_p99)

            pred_raw = torch.expm1(torch.clamp(
                out['exec_lat'], min=0.0, max=math.log1p(c.exec_bucket_cycle_max)))
            gold_raw = batch['exec_lat_raw'].to(pred_raw.dtype)
            abs_err = torch.abs(pred_raw - gold_raw)
            tail_terms = []
            for key, thr_key in (
                ('exec_tail_p95', 'exec_tail_p95_thr'),
                ('exec_tail_p99', 'exec_tail_p99_thr'),
            ):
                mask_tail = batch[key] > 0.5
                if torch.any(mask_tail):
                    denom = batch[thr_key].to(pred_raw.dtype).clamp(min=1.0)
                    tail_terms.append((abs_err[mask_tail] / denom[mask_tail]).mean())
            if tail_terms:
                tail_raw_mae = torch.stack(tail_terms).mean()
        mse_f_pos = zero
        mse_f_cons = zero
        pred_effective_fetch_raw = effective_fetch_raw()
        if 'head' in batch:
            fetch_target = batch['fetch_lat']
            head_target = batch['head'].to(fetch_target.dtype)
            head_mask = head_target > 0.5
            fetch_soft = torch.log1p(pred_effective_fetch_raw.clamp(min=0.0))
            if torch.any(head_mask):
                mse_f_pos = F.mse_loss(out['fetch_lat'][head_mask],
                                       fetch_target[head_mask])
            mse_f_cons = F.mse_loss(fetch_soft, fetch_target)
        else:
            mse_f_pos = F.mse_loss(out['fetch_lat'], batch['fetch_lat'])
            mse_f_cons = mse_f_pos

        bce_fetch_tail = zero
        fetch_tail_raw_mae = zero
        fetch_decomp = zero
        fetch_decomp_cons = zero
        if 'fetch_tail_p95' in batch and 'fetch_tail_p95_logit' in out:
            fp95_pw = torch.tensor(c.fetch_tail_p95_pos_weight,
                                   device=out['fetch_lat'].device,
                                   dtype=out['fetch_lat'].dtype)
            fp99_pw = torch.tensor(c.fetch_tail_p99_pos_weight,
                                   device=out['fetch_lat'].device,
                                   dtype=out['fetch_lat'].dtype)
            bce_fp95 = F.binary_cross_entropy_with_logits(
                out['fetch_tail_p95_logit'], batch['fetch_tail_p95'],
                pos_weight=fp95_pw)
            bce_fp99 = F.binary_cross_entropy_with_logits(
                out['fetch_tail_p99_logit'], batch['fetch_tail_p99'],
                pos_weight=fp99_pw)
            bce_fetch_tail = 0.5 * (bce_fp95 + bce_fp99)

            pred_fetch_raw = torch.expm1(torch.clamp(
                out['fetch_lat'], min=0.0, max=math.log1p(c.exec_bucket_cycle_max)))
            gold_fetch_raw = batch['fetch_lat_raw'].to(pred_fetch_raw.dtype)
            fetch_abs_err = torch.abs(pred_fetch_raw - gold_fetch_raw)
            fetch_tail_terms = []
            for key, thr_key in (
                ('fetch_tail_p95', 'fetch_tail_p95_thr'),
                ('fetch_tail_p99', 'fetch_tail_p99_thr'),
            ):
                mask_tail = batch[key] > 0.5
                if torch.any(mask_tail):
                    denom = batch[thr_key].to(pred_fetch_raw.dtype).clamp(min=1.0)
                    fetch_tail_terms.append(
                        (fetch_abs_err[mask_tail] / denom[mask_tail]).mean())
            if fetch_tail_terms:
                fetch_tail_raw_mae = torch.stack(fetch_tail_terms).mean()

            fetch_decomp_terms = [
                F.huber_loss(out['fetch_base'], batch['fetch_base'],
                             delta=c.exec_huber_delta)
            ]
            branch_mask = batch.get('fetch_after_mispred_mask')
            if branch_mask is not None and torch.any(branch_mask > 0.5):
                m = branch_mask > 0.5
                fetch_decomp_terms.append(F.huber_loss(
                    out['fetch_after_mispred'][m], batch['fetch_after_mispred'][m],
                    delta=c.exec_huber_delta))
            residual_mask = batch.get('fetch_residual_tail_mask')
            if residual_mask is not None and torch.any(residual_mask > 0.5):
                m = residual_mask > 0.5
                fetch_decomp_terms.append(F.huber_loss(
                    out['fetch_residual_tail'][m], batch['fetch_residual_tail'][m],
                    delta=c.exec_huber_delta))
            fetch_decomp = torch.stack(fetch_decomp_terms).mean()

            part_sum_raw = (
                torch.expm1(out['fetch_base'].clamp(min=0.0))
                + torch.expm1(out['fetch_after_mispred'].clamp(min=0.0))
                + torch.expm1(out['fetch_residual_tail'].clamp(min=0.0))
            )
            part_sum_log = torch.log1p(part_sum_raw.clamp(min=0.0))
            fetch_decomp_cons = F.huber_loss(
                part_sum_log, batch['fetch_lat'], delta=c.exec_huber_delta)
        # mispred: pos_weight 处理失衡；可选 focal 抑制易分负样本
        logit = out['mispred_logit']
        target = batch['mispred']
        mask = batch.get('mispred_mask')
        if mask is not None:
            mask = mask > 0.5
        pw = torch.tensor(c.mispred_pos_weight, device=logit.device, dtype=logit.dtype)
        if mask is not None and not torch.any(mask):
            bce_m = logit.sum() * 0.0
        else:
            if mask is not None:
                logit = logit[mask]
                target = target[mask]
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
        total = (c.w_fetch * mse_f_pos + c.w_fetch_cons * mse_f_cons
                 + c.w_fetch_tail_bce * bce_fetch_tail
                 + c.w_fetch_tail_mae * fetch_tail_raw_mae
                 + c.w_fetch_decomp * fetch_decomp
                 + c.w_fetch_decomp_cons * fetch_decomp_cons
                 + c.w_exec * huber_e
                 + c.w_exec_bucket * ce_exec_bucket
                 + c.w_exec_residual * huber_exec_res
                 + c.w_exec_quantile * pinball_exec_q
                 + c.w_tail_bce * bce_tail
                 + c.w_tail_mae * tail_raw_mae
                 + c.w_mispred * bce_m + c.w_head * bce_h)
        return {
            'loss': total,
            'mse_fetch': mse_f_pos.detach(),
            'mse_fetch_cons': mse_f_cons.detach(),
            'bce_fetch_tail': bce_fetch_tail.detach(),
            'fetch_tail_raw_mae': fetch_tail_raw_mae.detach(),
            'fetch_decomp': fetch_decomp.detach(),
            'fetch_decomp_cons': fetch_decomp_cons.detach(),
            'huber_exec': huber_e.detach(),
            'ce_exec_bucket': ce_exec_bucket.detach(),
            'huber_exec_residual': huber_exec_res.detach(),
            'pinball_exec_quantile': pinball_exec_q.detach(),
            'bce_tail': bce_tail.detach(),
            'tail_raw_mae': tail_raw_mae.detach(),
            'bce_mispred': bce_m.detach(),
            'bce_head': bce_h.detach(),
        }

    def num_params(self) -> int:
        return sum(p.numel() for p in self.parameters())
