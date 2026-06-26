"""解析真实 gem5 ``stats.txt``，作为 oracle 评测的主标尺。

约定：
- Ruby MESI_Three_Level 把 L1d/L0d 的命中/未命中以 Sequencer 时延直方图样本数
  导出，字段是 ``RequestType.{LD,ST}.{hit,miss}_latency_hist_seqr::samples``。
- 我们按"全核合计"和"按核拆分"两种粒度给出 hit/miss 计数与 miss rate；
- 与 tao_trace 软件标签并列报告，作为 P1 决策的主标尺。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Optional

# 匹配类似:
#   board.processor.cores0.core...RequestType.LD.miss_latency_hist_seqr::samples  6600
_PAT = re.compile(
    r"\bRequestType\.(LD|ST)\.(hit|miss)_latency_hist_seqr::samples\s+(\d+)\b"
)
# 提取核 id（可选，用于 per-core）
_CORE_PAT = re.compile(r"\bcores(\d+)\b")


def parse_stats_txt(path: Path) -> dict:
    """读取 ``stats.txt``，返回结构化结果。

    Returns
    -------
    dict
        ``{"by_core": {core_id: {"ld_hit": .., "ld_miss": .., "st_hit": ..,
            "st_miss": ..}}, "total": {同字段}, "rates": {"mr_l1d_ld": ..,
            "mr_l1d_st": ..}, "source": str(path)}``。
    """
    by_core: Dict[int, Dict[str, int]] = {}
    if not path.exists():
        return {"by_core": {}, "total": {}, "rates": {}, "source": str(path)}

    with open(path) as f:
        for line in f:
            m = _PAT.search(line)
            if not m:
                continue
            op, kind, val = m.group(1), m.group(2), int(m.group(3))
            cm = _CORE_PAT.search(line)
            cid = int(cm.group(1)) if cm else -1
            entry = by_core.setdefault(cid, {
                "ld_hit": 0, "ld_miss": 0, "st_hit": 0, "st_miss": 0})
            key = f"{op.lower()}_{kind}"
            entry[key] += val

    total = {"ld_hit": 0, "ld_miss": 0, "st_hit": 0, "st_miss": 0}
    for entry in by_core.values():
        for k in total:
            total[k] += entry[k]

    def mr(miss: int, hit: int) -> Optional[float]:
        n = miss + hit
        return (miss / n) if n > 0 else None

    rates = {
        "mr_l1d_ld": mr(total["ld_miss"], total["ld_hit"]),
        "mr_l1d_st": mr(total["st_miss"], total["st_hit"]),
    }
    return {
        "by_core": by_core,
        "total": total,
        "rates": rates,
        "source": str(path),
    }


def workload_stats_path(raw_root: Path, workload: str) -> Path:
    """约定的 stats.txt 位置：``<raw_root>/<workload>/stats.txt``。"""
    return raw_root / workload / "stats.txt"


def load_real_stats(raw_root: Path, workload: str) -> dict:
    return parse_stats_txt(workload_stats_path(raw_root, workload))


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--raw-root", default="/data00/yinhaolang/LLMSim/data/raw_eval11_8c")
    ap.add_argument("--workload", action="append", default=[])
    args = ap.parse_args()
    workloads = args.workload or ["W_ads_ctr"]
    for w in workloads:
        r = load_real_stats(Path(args.raw_root), w)
        print(f"\n=== {w} (source={r['source']}) ===")
        if not r["total"]:
            print("  (no Ruby Seqr samples found)")
            continue
        t = r["total"]
        print(f"  TOTAL  LD: hit={t['ld_hit']} miss={t['ld_miss']} "
              f"mr={r['rates']['mr_l1d_ld']:.4%}")
        print(f"  TOTAL  ST: hit={t['st_hit']} miss={t['st_miss']} "
              f"mr={r['rates']['mr_l1d_st']:.4%}")
        for cid in sorted(c for c in r["by_core"] if c >= 0):
            e = r["by_core"][cid]
            mr_ld = e["ld_miss"] / max(1, e["ld_miss"] + e["ld_hit"])
            mr_st = e["st_miss"] / max(1, e["st_miss"] + e["st_hit"])
            print(f"  core{cid}: ld_mr={mr_ld:.4%} st_mr={mr_st:.4%}")
