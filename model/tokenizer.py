"""LLMSim 自定义 tokenizer：每条 µop 编码为固定 6 个 token。

设计目标：
  - functional only：仅编码架构态可见字段，禁止任何 µarch oracle / tick / latency。
  - 定长：1 µop = 6 token，避免 BPE 把数字 / 地址切碎导致序列爆炸。
  - 词表小（~2K），新 token 注入 Qwen3 tokenizer 后 resize embedding。

6 槽编码（见 README §2 / docs/design.md §1.4）：
  slot1 OPCLASS   : 指令类（int/fp/simd/load/store/branch_*/atomic/fence/...）
  slot2 REG       : (n_src, n_dst, 寄存器槽 hash) 合并成一个桶 token
  slot3 MEMKIND   : none/load/store/atomic/fence
  slot4 VLINE     : vaddr >> 6 (cacheline) hash -> 1024 桶
  slot5 VPAGE     : vaddr >> 12 (page) hash -> 256 桶
  slot6 BR        : (taken<<2 | cond<<1 | indirect) 与 target delta bucket 合并

控制 token：
  <SYS> <CFG_*> <C{i}_BEGIN> <C{i}_END> <SYNC> <QUERY_C{i}> <PAD> <TRACE> <TRACE_END>
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Dict, List, Optional

# ----------------------------- vocab 规模常量
N_OPCLASS = 16          # OPCLASS 桶
N_REG_BUCKET = 64       # 寄存器组合 hash 桶
N_MEMKIND = 5
VLINE_BUCKETS = 1024
VPAGE_BUCKETS = 256
N_BR = 32               # (taken|cond|indirect)<<3 等组合
MAX_CORES = 8           # per-core BEGIN/END/QUERY token 预留

# CFG conditioning 离散桶（log2 KiB 等），给固定的数值区间
N_CFG_L1D = 8
N_CFG_L2 = 12
N_CFG_L3 = 16
N_CFG_CLK = 8


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
        for i in range(VLINE_BUCKETS):
            toks.append(f"<VL_{i}>")
        for i in range(VPAGE_BUCKETS):
            toks.append(f"<VP_{i}>")
        for i in range(N_BR):
            toks.append(f"<BR_{i}>")
        return VocabLayout(tokens=toks)


def encode_uop(rec: dict) -> List[str]:
    """1 µop -> 6 个 token 字符串。"""
    return [
        f"<OP_{opclass_id(rec)}>",
        f"<RG_{reg_bucket(rec)}>",
        f"<MK_{memkind_id(rec)}>",
        f"<VL_{vline_bucket(rec)}>",
        f"<VP_{vpage_bucket(rec)}>",
        f"<BR_{br_token(rec)}>",
    ]


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
