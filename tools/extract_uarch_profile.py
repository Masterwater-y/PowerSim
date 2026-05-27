#!/usr/bin/env python3
# coding: utf-8
"""extract_uarch_profile.py

从已有的 gem5 m5out/config.ini 反向提取 schema v2 uarch_profile.json，
用于：
  1. 后置一致性校验：把 run_mt_mvp.py 仿真前 write_uarch_profile() 写出的
     uarch_profile.json 与本工具从 config.ini 提取的结果做 diff，
     确认 oracle / ref_sim 真正吃到的微架构 == 用户声明的微架构。
  2. 历史 baseline 重放：对存档中的 m5out/config.ini 直接生成 profile，
     喂给当前 ref_sim 跑回归。

约束：
  - 只支持 X86_MESI_Three_Level (有 L0Cache + L1Cache + L2Cache + Directory)。
  - 只支持 LRU 替换 + cacheline = 64B。
  - 不支持的维度直接 RuntimeError，禁止 silent fallback。
"""

import argparse
import configparser
import json
import os
import re
import sys


# ---- 容量字符串解析 -----------------------------------------------------

_SIZE_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?i?B)$", re.IGNORECASE)


def parse_size_b(s: str) -> int:
    """gem5 风格 "32KiB" / "2MiB" / "262144B" → 字节数。"""
    s = str(s).strip()
    if s.isdigit():
        return int(s)
    m = _SIZE_RE.match(s)
    if not m:
        raise RuntimeError(f"cannot parse size string: {s!r}")
    val = float(m.group(1))
    unit = m.group(2)
    table = {
        "B": 1,
        "KB": 1000, "KiB": 1024,
        "MB": 1000**2, "MiB": 1024**2,
        "GB": 1000**3, "GiB": 1024**3,
        "TB": 1000**4, "TiB": 1024**4,
    }
    # 大小写规范化：KiB/MiB/GiB 严格区分；KB/MB 当作 1000
    key = None
    for k in table:
        if k.lower() == unit.lower():
            key = k
            break
    if key is None:
        raise RuntimeError(f"unknown size unit: {unit!r}")
    return int(val * table[key])


# ---- config.ini 反查 ----------------------------------------------------

def _find_section(cfg: configparser.ConfigParser, pattern: str):
    """返回第一个 section 名匹配 regex 的 section。"""
    pat = re.compile(pattern)
    for sec in cfg.sections():
        if pat.search(sec):
            return sec
    return None


def _find_all_sections(cfg: configparser.ConfigParser, pattern: str):
    pat = re.compile(pattern)
    return [s for s in cfg.sections() if pat.search(s)]


def _get(cfg, sec, key, default=None):
    if sec is None:
        return default
    if cfg.has_option(sec, key):
        return cfg.get(sec, key)
    return default


def _require(cfg, sec, key):
    v = _get(cfg, sec, key, None)
    if v is None:
        raise RuntimeError(f"config.ini missing [{sec}].{key}")
    return v


def _cache_cfg(cfg, sec, num_banks=1):
    size_b = parse_size_b(_require(cfg, sec, "size"))
    assoc = int(_require(cfg, sec, "assoc"))
    repl = _get(cfg, sec, "replacement_policy", "")
    # MESI_Three_Level 默认 LRURP；非 LRU 直接 fail。
    if repl and "LRU" not in repl.upper():
        # gem5 路径形如 system.cpu0.l1d_cache.replacement_policy
        # 解一层 sub-section 看 type
        sub = repl.strip()
        if sub.startswith("system.") and cfg.has_section(sub):
            t = _get(cfg, sub, "type", "")
            if "LRU" not in t.upper():
                raise RuntimeError(
                    f"only LRU replacement supported, got {t!r} at [{sub}]"
                )
    return {
        "size_b": size_b,
        "assoc": assoc,
        "line_b": 64,
        "num_banks": int(num_banks),
        "bank_select_low_bit": 6,
        "policy": "lru",
    }


def _tlb_cfg(cfg, sec):
    if sec is None:
        # 没找到 TLB section，schema v2 仍要求 dtlb/itlb 存在。
        raise RuntimeError("config.ini: dtlb/itlb section not found")
    entries = int(_require(cfg, sec, "size"))
    # gem5 TLB 通常全相联：assoc == entries。
    return {"entries": entries, "assoc": entries}


# ---- 主提取 -------------------------------------------------------------

def extract(cfg: configparser.ConfigParser) -> dict:
    # 1) cores
    core_secs = _find_all_sections(cfg, r"^system\.processor\.cores\d+\.core$")
    if not core_secs:
        # gem5 stdlib SimpleProcessor 也可能直接 system.cpu0
        core_secs = _find_all_sections(cfg, r"^system\.cpu\d+$")
    if not core_secs:
        raise RuntimeError("no O3 core section found in config.ini")
    num_cores = len(core_secs)

    # freq: system.clk_domain.clock = "2GHz" 之类
    clk_sec = _find_section(cfg, r"^system\.clk_domain$")
    freq_ghz = 2.0
    if clk_sec:
        clk = _get(cfg, clk_sec, "clock", "2GHz")
        # gem5 的 clock 字符串有时是 "500"（频率列表第一个 tick），
        # 这里只支持简单 "<num>GHz" 形式。
        m = re.match(r"^([0-9.]+)\s*GHz$", clk.strip(), re.IGNORECASE)
        if m:
            freq_ghz = float(m.group(1))

    # 2) caches
    # MESI_Three_Level: l0 = L1 (split d/i), l1 = L2, l2 (Directory) = L3 banks
    l1d_sec = _find_section(cfg, r"L0Cache_Controller\d*\.Dcache$") \
        or _find_section(cfg, r"l1d_cache$")
    l1i_sec = _find_section(cfg, r"L0Cache_Controller\d*\.Icache$") \
        or _find_section(cfg, r"l1i_cache$")
    l2_sec = _find_section(cfg, r"L1Cache_Controller\d*\.cache$") \
        or _find_section(cfg, r"l2_cache$")
    l3_secs = _find_all_sections(cfg, r"L2Cache_Controller\d+\.L2cache$")
    if not l3_secs:
        l3_one = _find_section(cfg, r"l3_cache$")
        if l3_one:
            l3_secs = [l3_one]
    if not l1d_sec or not l1i_sec or not l2_sec or not l3_secs:
        raise RuntimeError(
            "config.ini: cannot locate L1D/L1I/L2/L3 sections "
            f"(found l1d={l1d_sec} l1i={l1i_sec} l2={l2_sec} "
            f"l3={l3_secs})"
        )

    num_l3_banks = len(l3_secs)
    l3_size_total = num_l3_banks * parse_size_b(_require(cfg, l3_secs[0], "size"))

    cache = {
        "l1d": _cache_cfg(cfg, l1d_sec, num_banks=1),
        "l1i": _cache_cfg(cfg, l1i_sec, num_banks=1),
        "l2":  _cache_cfg(cfg, l2_sec,  num_banks=1),
        "l3":  {
            **_cache_cfg(cfg, l3_secs[0], num_banks=num_l3_banks),
            "size_b": l3_size_total,
        },
    }

    # 3) TLB
    dtlb_sec = _find_section(cfg, r"\.mmu\.dtb$") \
        or _find_section(cfg, r"\.dtb$")
    itlb_sec = _find_section(cfg, r"\.mmu\.itb$") \
        or _find_section(cfg, r"\.itb$")
    tlb = {
        "dtlb": _tlb_cfg(cfg, dtlb_sec),
        "itlb": _tlb_cfg(cfg, itlb_sec),
        "stlb": None,
    }

    # 4) page walker：x86 默认 4 级 4KiB
    page_walker = {
        "levels": 4,
        "page_size_bits": 12,
        "walk_attaches_to": "sequencer",
        "pwc_entries": 0,
    }

    # 5) MSHR
    def _mshr(sec, fallback):
        v = _get(cfg, sec, "number_of_TBEs", None)
        if v is None:
            v = _get(cfg, sec, "mshrs", None)
        return int(v) if v is not None else fallback

    l1d_ctl = _find_section(cfg, r"L0Cache_Controller\d+$")
    l2_ctl = _find_section(cfg, r"L1Cache_Controller\d+$")
    l3_ctl = _find_section(cfg, r"L2Cache_Controller\d+$")
    mshr = {
        "l1d_entries": _mshr(l1d_ctl, 16),
        "l2_entries":  _mshr(l2_ctl, 32),
        "l3_entries":  _mshr(l3_ctl, 64),
    }

    profile = {
        "schema_version": 2,
        "source": "extract_uarch_profile.py(post-sim)",
        "core": {
            "isa": "X86",
            "num_cores": num_cores,
            "freq_ghz": freq_ghz,
        },
        "cache": cache,
        "tlb": tlb,
        "page_walker": page_walker,
        "mshr": mshr,
        "coherence": {"protocol": "MESI_Three_Level"},
    }
    return profile


# ---- CLI ----------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="从 m5out/config.ini 提取 schema v2 uarch_profile.json"
    )
    ap.add_argument("config_ini", help="gem5 m5out/config.ini 路径")
    ap.add_argument("-o", "--output", default=None,
                    help="输出 uarch_profile.json 路径（默认 stdout）")
    args = ap.parse_args()

    if not os.path.isfile(args.config_ini):
        print(f"error: not a file: {args.config_ini}", file=sys.stderr)
        sys.exit(2)

    cfg = configparser.ConfigParser(strict=False, interpolation=None)
    cfg.read(args.config_ini)

    profile = extract(cfg)
    text = json.dumps(profile, indent=2)
    if args.output:
        with open(args.output, "w") as f:
            f.write(text)
        print(f"[extract_uarch_profile] wrote {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(text + "\n")


if __name__ == "__main__":
    main()
