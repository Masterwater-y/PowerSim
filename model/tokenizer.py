"""LLMSim 自定义 tokenizer：每条 µop 编码为固定 6 个 token。

设计目标：
  - functional only：仅编码架构态可见字段，禁止任何 µarch oracle / tick / latency。
  - 定长：1 µop = 6 token，避免 BPE 把数字 / 地址切碎导致序列爆炸。
  - 词表小（~2K），新 token 注入 Qwen3 tokenizer 后 resize embedding。

6 槽编码（见 README §2 / docs/design.md §1.4）：
  slot1 OPCLASS   : 指令类（int/fp/simd/load/store/branch_*/atomic/fence/...）
  slot2 REG       : (n_src, n_dst, 寄存器槽 hash) 合并成一个桶 token
  slot3 MEMKIND   : none/load/store/atomic/fence
  slot4 RD        : bounded sliding reuse distance bucket
  slot5 STRIDE    : cacheline stride bucket
  slot6 BR        : (taken<<2 | cond<<1 | indirect) 与 target delta bucket 合并

控制 token：
  <SYS> <CFG_*> <C{i}_BEGIN> <C{i}_END> <SYNC> <QUERY_C{i}> <PAD> <TRACE> <TRACE_END>
  <SM_*> per-core functional summary tokens
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Dict, List, Optional

# ----------------------------- vocab 规模常量
N_OPCLASS = 16          # OPCLASS 桶
N_REG_BUCKET = 64       # 寄存器组合 hash 桶
N_MEMKIND = 5
VLINE_BUCKETS = 1024    # legacy helpers only; no longer emitted in vocab
VPAGE_BUCKETS = 256     # legacy helpers only; no longer emitted in vocab
N_RD = 9                # nonmem/cold/le8/le64/le512/le4k/le32k/le256k/far
N_STRIDE = 10           # nonmem/first/same/+1/-1/+2..8/-2..8/+9..64/-9..64/large
N_BR = 32               # (taken|cond|indirect)<<3 等组合
MAX_CORES = 8           # per-core BEGIN/END/QUERY token 预留

# CFG conditioning 离散桶（log2 KiB 等），给固定的数值区间
N_CFG_L1D = 8
N_CFG_L2 = 12
N_CFG_L3 = 16
N_CFG_CLK = 8

# per-core summary 离散桶。
N_SUM_FRAC = 8          # fraction [0,1] -> 8 桶
N_SUM_LOG = 16          # log2(count+1) -> 16 桶

RD_NONMEM = 0
RD_COLD = 1
RD_LE8 = 2
RD_LE64 = 3
RD_LE512 = 4
RD_LE4K = 5
RD_LE32K = 6
RD_LE256K = 7
RD_FAR = 8

ST_NONMEM = 0
ST_FIRST = 1
ST_SAME = 2
ST_P1 = 3
ST_M1 = 4
ST_P2_8 = 5
ST_M2_8 = 6
ST_P9_64 = 7
ST_M9_64 = 8
ST_LARGE = 9


def _hash_bucket(x: int, n: int) -> int:
    """splitmix-ish 稳定 hash -> [0, n)。x 为非负整数。"""
    x &= 0xFFFFFFFFFFFFFFFF
    x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9 & 0xFFFFFFFFFFFFFFFF
    x = (x ^ (x >> 27)) * 0x94D049BB133111EB & 0xFFFFFFFFFFFFFFFF
    x ^= x >> 31
    return x % n


def opclass_id(rec: dict) -> int:
    """从 functional bool 旗位派生 opclass（互斥优先级）。"""
    if rec.get("is_atomic"):
        return 1
    if rec.get("is_load"):
        return 2
    if rec.get("is_store"):
        return 3
    if rec.get("is_branch"):
        if rec.get("is_branch_indirect"):
            return 4
        if rec.get("is_branch_cond"):
            return 5
        return 6          # 无条件直接跳转
    if rec.get("is_call"):
        return 7
    if rec.get("is_return"):
        return 8
    if rec.get("is_fp"):
        return 9
    if rec.get("is_simd"):
        return 10
    if rec.get("is_serialize"):
        return 11
    if rec.get("is_int"):
        return 12
    return 0              # OTHER


def reg_bucket(rec: dict) -> int:
    """(n_src, n_dst, producer_classes) 合并 hash。producer_classes 是寄存器类，
    属架构依赖结构信息，可见。绝对寄存器号不直接进，避免过拟合。"""
    n_src = int(rec.get("n_src", 0)) & 0x7
    n_dst = int(rec.get("n_dst", 0)) & 0x7
    pc = rec.get("producer_classes", []) or []
    h = (n_src << 3) | n_dst
    for i, c in enumerate(pc[:4]):
        h = (h << 8) | (int(c) & 0xFF)
    return _hash_bucket(h, N_REG_BUCKET)


def memkind_id(rec: dict) -> int:
    if rec.get("is_atomic"):
        return 3
    if rec.get("is_load"):
        return 1
    if rec.get("is_store"):
        return 2
    # fence 没有独立旗位，用 serialize+无 mem 近似（保守归 none）
    return 0


def vline_bucket(rec: dict) -> int:
    v = int(rec.get("vaddr", 0))
    if v == 0:
        return 0
    return 1 + _hash_bucket(v >> 6, VLINE_BUCKETS - 1)


def vpage_bucket(rec: dict) -> int:
    v = int(rec.get("vaddr", 0))
    if v == 0:
        return 0
    return 1 + _hash_bucket(v >> 12, VPAGE_BUCKETS - 1)


def rd_bucket_from_distance(rd: Optional[int]) -> int:
    """Bounded reuse distance -> token bucket.

    rd is distinct cacheline reuse distance within the maintained sliding
    memory-reference window. None means cold/far is decided by caller.
    """
    if rd is None:
        return RD_COLD
    rd = int(rd)
    if rd <= 8:
        return RD_LE8
    if rd <= 64:
        return RD_LE64
    if rd <= 512:
        return RD_LE512
    if rd <= 4096:
        return RD_LE4K
    if rd <= 32768:
        return RD_LE32K
    if rd <= 262144:
        return RD_LE256K
    return RD_FAR


def rd_bucket(rec: dict) -> int:
    if not rec.get("is_load") and not rec.get("is_store") and not rec.get("is_atomic"):
        return RD_NONMEM
    return int(rec.get("_rd_bucket", RD_COLD))


def stride_bucket_from_delta(delta: Optional[int]) -> int:
    """Cacheline stride delta -> token bucket."""
    if delta is None:
        return ST_FIRST
    delta = int(delta)
    if delta == 0:
        return ST_SAME
    if delta == 1:
        return ST_P1
    if delta == -1:
        return ST_M1
    if 2 <= delta <= 8:
        return ST_P2_8
    if -8 <= delta <= -2:
        return ST_M2_8
    if 9 <= delta <= 64:
        return ST_P9_64
    if -64 <= delta <= -9:
        return ST_M9_64
    return ST_LARGE


def stride_bucket(rec: dict) -> int:
    if not rec.get("is_load") and not rec.get("is_store") and not rec.get("is_atomic"):
        return ST_NONMEM
    return int(rec.get("_stride_bucket", ST_FIRST))


def frac_bucket(x: float) -> int:
    x = max(0.0, min(1.0, float(x)))
    return min(N_SUM_FRAC - 1, int(x * N_SUM_FRAC))


def log_count_bucket(x: int) -> int:
    x = max(0, int(x))
    if x <= 0:
        return 0
    return min(N_SUM_LOG - 1, int(x.bit_length() - 1))


def br_token(rec: dict) -> int:
    """分支控制位组合（仅架构态：是否分支/条件/间接）。
    注意：taken / target 在 records.micro 中没有直接给（functional 无分支结果），
    故此处只编码静态分支类型；taken 留给模型从上下文学。"""
    if not rec.get("is_branch"):
        return 0
    cond = 1 if rec.get("is_branch_cond") else 0
    ind = 1 if rec.get("is_branch_indirect") else 0
    call = 1 if rec.get("is_call") else 0
    ret = 1 if rec.get("is_return") else 0
    return 1 + ((cond << 3) | (ind << 2) | (call << 1) | ret)


@dataclass
class VocabLayout:
    """把各字段桶映射到一段连续 id 空间，返回 special token 名 -> 文本。
    实际 id 由 HF tokenizer 在 add_special_tokens 后分配，这里只生成 token 字符串。
    """
    tokens: List[str]

    @staticmethod
    def build() -> "VocabLayout":
        toks: List[str] = []
        # 结构控制
        toks += ["<SYS>", "<TRACE>", "<TRACE_END>", "<SYNC>", "<PAD_UOP>"]
        for c in range(MAX_CORES):
            toks += [f"<C{c}_BEGIN>", f"<C{c}_END>", f"<QUERY_C{c}>"]
        # CFG conditioning
        for i in range(N_CFG_L1D):
            toks.append(f"<CFG_L1D_{i}>")
        for i in range(N_CFG_L2):
            toks.append(f"<CFG_L2_{i}>")
        for i in range(N_CFG_L3):
            toks.append(f"<CFG_L3_{i}>")
        for i in range(N_CFG_CLK):
            toks.append(f"<CFG_CLK_{i}>")
        # 6 槽字段 token
        for i in range(N_OPCLASS):
            toks.append(f"<OP_{i}>")
        for i in range(N_REG_BUCKET):
            toks.append(f"<RG_{i}>")
        for i in range(N_MEMKIND):
            toks.append(f"<MK_{i}>")
        for i in range(N_RD):
            toks.append(f"<RD_{i}>")
        for i in range(N_STRIDE):
            toks.append(f"<ST_{i}>")
        for i in range(N_BR):
            toks.append(f"<BR_{i}>")
        # per-core functional summary tokens.
        for name in ("MEM", "LD", "STF"):
            for i in range(N_SUM_FRAC):
                toks.append(f"<SM_{name}_{i}>")
        for name in ("DLINE", "DPAGE"):
            for i in range(N_SUM_LOG):
                toks.append(f"<SM_{name}_{i}>")
        for i in range(N_RD):
            for j in range(N_SUM_FRAC):
                toks.append(f"<SM_RD{i}_{j}>")
        for i in range(N_STRIDE):
            for j in range(N_SUM_FRAC):
                toks.append(f"<SM_STR{i}_{j}>")
        return VocabLayout(tokens=toks)


def encode_uop(rec: dict) -> List[str]:
    """1 µop -> 6 个 token 字符串。"""
    return [
        f"<OP_{opclass_id(rec)}>",
        f"<RG_{reg_bucket(rec)}>",
        f"<MK_{memkind_id(rec)}>",
        f"<RD_{rd_bucket(rec)}>",
        f"<ST_{stride_bucket(rec)}>",
        f"<BR_{br_token(rec)}>",
    ]


def core_summary_tokens(summary: dict) -> List[str]:
    """Window/core functional summary -> compact discrete tokens."""
    rd_hist = summary.get("rd_hist", [0.0] * N_RD)
    stride_hist = summary.get("stride_hist", [0.0] * N_STRIDE)
    toks = [
        f"<SM_MEM_{frac_bucket(summary.get('mem_ratio', 0.0))}>",
        f"<SM_LD_{frac_bucket(summary.get('load_frac_mem', 0.0))}>",
        f"<SM_STF_{frac_bucket(summary.get('store_frac_mem', 0.0))}>",
        f"<SM_DLINE_{log_count_bucket(summary.get('distinct_lines', 0))}>",
        f"<SM_DPAGE_{log_count_bucket(summary.get('distinct_pages', 0))}>",
    ]
    for i in range(N_RD):
        v = rd_hist[i] if i < len(rd_hist) else 0.0
        toks.append(f"<SM_RD{i}_{frac_bucket(v)}>")
    for i in range(N_STRIDE):
        v = stride_hist[i] if i < len(stride_hist) else 0.0
        toks.append(f"<SM_STR{i}_{frac_bucket(v)}>")
    return toks


def cfg_tokens(cfg: dict) -> List[str]:
    """uarch config -> CFG conditioning token 列表。"""
    ct = cfg.get("cfg_tokens", {})
    return [
        f"<CFG_L1D_{min(int(ct.get('l1d_kib_log2', 5)), N_CFG_L1D-1)}>",
        f"<CFG_L2_{min(int(ct.get('l2_kib_log2', 8)), N_CFG_L2-1)}>",
        f"<CFG_L3_{min(int(ct.get('l3_kib_log2', 11)), N_CFG_L3-1)}>",
        f"<CFG_CLK_{min(int(ct.get('clk_ghz', 3)), N_CFG_CLK-1)}>",
    ]


def all_special_tokens() -> List[str]:
    return VocabLayout.build().tokens


def inject_into_hf_tokenizer(tok):
    """把全部 LLMSim token 作为 additional_special_tokens 注入 HF tokenizer。
    返回新增 token 数。调用方需对 model.resize_token_embeddings(len(tok))。"""
    new_tokens = all_special_tokens()
    added = tok.add_special_tokens(
        {"additional_special_tokens": new_tokens}
    )
    return added


if __name__ == "__main__":
    layout = VocabLayout.build()
    print(f"total LLMSim tokens = {len(layout.tokens)}")
    sample = {"is_int": 1, "n_dst": 1, "is_branch": 0, "vaddr": 0,
              "producer_classes": [255, 255, 255, 255]}
    print("encode_uop sample:", encode_uop(sample))
    print("cfg_tokens sample:", cfg_tokens({"cfg_tokens": {}}))
