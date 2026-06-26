"""PMU 全栈分层误差诊断：oracle (shared_system) vs real (gem5 stats.txt)。

层级 / 对照列（Ruby MESI_Three_Level <-> oracle 命名）::

  real L0Cache_Controller  <->  oracle L1d
  real L1Cache_Controller  <->  oracle L2     （第二级私有 cache，gem5 命名容易混）
  real L2Cache_Controller  <->  oracle LLC    （MESI_Three_Level 里的 directory + LLC）

CHA 行的对照：

  oracle ``cha.requests.{reads,writes}``（已在 P3.c 加 L2-miss 门限）
      = L2 miss 后真正离开核进 LLC/CHA 的请求
      <-> real ``L2Cache_Controller.{L1_GETS,L1_GETX,L1_UPGRADE}::total``
          （所有从 L1Cache_Controller 抵达 L2Cache 的请求）

  oracle ``cha.dir_lookup.snp``
      = REMOTE_HIT_CLEAN + REMOTE_HIT_DIRTY + WB_REQUIRED （需要 snoop 的目录查询）
      <-> real ``L1Cache_Controller.{Fwd_GETS,Fwd_GETX}::total``
          （L2 forward 给 L1 的 snoop）

  oracle ``cha.remote_hit`` (uncore.cha_remote_{clean,dirty})
      = 本核读到他核持有的行
      <-> real ``L1Cache_Controller.Fwd_GETS::total`` （读类 forward）

  oracle ``cha.wb_required`` (uncore.wb_required)
      = 本核 store 触发的需失效的目录请求
      <-> real ``L1Cache_Controller.Fwd_GETX::total`` （写类 forward）

P3.c 之前 oracle 的 ``cha.requests`` 对每条 mem 访问无条件累加，
等于把 L1/L2 命中也算 CHA 请求；与真实 PMU 偏差极大。该 bug 已修。

用法::

    /root/miniconda3/envs/yinhaolang/bin/python scripts/pmu_layers_diag.py \\
        --snap-dir logs/oracle_ab_p3c \\
        --raw-root data/raw_eval11_8c

输出: logs/<snap-dir>/pmu_layers_diag.txt 与同名 .json。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional


ROOT = Path("/data00/yinhaolang/LLMSim")
DEFAULT_WORKLOADS = [
    "W_ads_ctr", "W_stream", "W_feed_ranking",
    "W_interest_graph_recall", "W_false_sharing",
]

_PAT_TOTAL = re.compile(r"\s+([\d.]+)\s+\(Unspecified\)\s*$")


def load_oracle_final(path: Path) -> dict:
    last: dict = {}
    if not path.exists():
        return last
    with open(path) as f:
        for ln in f:
            ln = ln.strip()
            if not ln.startswith("{"):
                continue
            try:
                last = json.loads(ln)
            except Exception:
                continue
    return last


def grep_total(stats_path: Path, prefix_ends_with_total: str) -> Optional[float]:
    s = 0.0
    found = False
    with open(stats_path) as f:
        for ln in f:
            if prefix_ends_with_total not in ln:
                continue
            m = _PAT_TOTAL.search(ln)
            if not m:
                continue
            try:
                s += float(m.group(1))
                found = True
            except Exception:
                pass
    return s if found else None


def parse_real(stats_path: Path) -> Dict[str, Optional[float]]:
    if not stats_path.exists():
        return {}
    r: Dict[str, Optional[float]] = {}

    ld_miss = st_miss = ld_hit = st_hit = 0.0
    seen_seqr = False
    with open(stats_path) as f:
        for ln in f:
            m = re.search(
                r"RequestType\.(LD|ST)\.(hit|miss)_latency_hist_seqr::samples"
                r"\s+([\d.]+)", ln)
            if not m:
                continue
            seen_seqr = True
            op, kind, v = m.group(1), m.group(2), float(m.group(3))
            if op == "LD" and kind == "hit":  ld_hit += v
            if op == "LD" and kind == "miss": ld_miss += v
            if op == "ST" and kind == "hit":  st_hit += v
            if op == "ST" and kind == "miss": st_miss += v
    r["l1d_loads"]    = (ld_hit + ld_miss) if seen_seqr else None
    r["l1d_stores"]   = (st_hit + st_miss) if seen_seqr else None
    r["l1d_ld_miss"]  = ld_miss if seen_seqr else None
    r["l1d_st_miss"]  = st_miss if seen_seqr else None

    l1_load  = grep_total(stats_path, "L1Cache_Controller.Load::total")
    l1_store = grep_total(stats_path, "L1Cache_Controller.Store::total")
    l1_i_load  = grep_total(stats_path, "L1Cache_Controller.I.Load::total")
    l1_i_store = grep_total(stats_path, "L1Cache_Controller.I.Store::total")
    r["l2_lookups"]    = ((l1_load or 0) + (l1_store or 0)) or None
    r["l2_miss_to_llc"] = ((l1_i_load or 0) + (l1_i_store or 0)) or None

    g_s = grep_total(stats_path, "L2Cache_Controller.L1_GETS::total")
    g_x = grep_total(stats_path, "L2Cache_Controller.L1_GETX::total")
    g_u = grep_total(stats_path, "L2Cache_Controller.L1_UPGRADE::total")
    r["llc_lookups"] = ((g_s or 0) + (g_x or 0) + (g_u or 0)) or None
    r["llc_miss_to_dram"] = grep_total(
        stats_path, "L2Cache_Controller.Mem_Data::total")

    fwd_s = grep_total(stats_path, "L1Cache_Controller.Fwd_GETS::total")
    fwd_x = grep_total(stats_path, "L1Cache_Controller.Fwd_GETX::total")
    r["fwd_get_s"] = fwd_s
    r["fwd_get_x"] = fwd_x
    r["fwd_total"] = ((fwd_s or 0) + (fwd_x or 0)) or None
    return r


def errpct(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if b is None or b == 0 or a is None:
        return None
    return abs(a - b) / abs(b) * 100


def fmt(v: Optional[float], w: int = 12, p: int = 0) -> str:
    if v is None:
        return " " * (w - 1) + "-"
    return f"{v:>{w}.{p}f}"


def fmt_pct(v: Optional[float], w: int = 8) -> str:
    if v is None:
        return " " * (w - 1) + "-"
    return f"{v:>{w}.2f}"


def collect_rows(snap_dir: Path, raw_root: Path,
                 workload: str, snap_tag: str) -> List[dict]:
    snap_path = snap_dir / f"{workload}.{snap_tag}.shared_pmu.jsonl"
    snap = load_oracle_final(snap_path)
    pmu = snap.get("pmu", {}) if snap else {}
    uncore = snap.get("uncore", {}) if snap else {}

    o = {
        "l1d.loads":          pmu.get("l1d.loads"),
        "l1d.stores":         pmu.get("l1d.stores"),
        "l1d.load_misses":    pmu.get("l1d.load_misses"),
        "l1d.store_misses":   pmu.get("l1d.store_misses"),
        "l2.misses":          pmu.get("l2.misses"),
        "llc.misses":         (pmu.get("llc.load_misses", 0)
                               + pmu.get("llc.store_misses", 0)),
        "cha.requests":       (pmu.get("cha.requests.reads", 0)
                               + pmu.get("cha.requests.writes", 0)),
        "cha.dir_lookup.snp": pmu.get("cha.dir_lookup.snp"),
        "cha.remote_hit":     (uncore.get("cha_remote_clean", 0)
                               + uncore.get("cha_remote_dirty", 0)),
        "cha.wb_required":    uncore.get("wb_required"),
    }

    real = parse_real(raw_root / workload / "stats.txt")

    rows = [
        ("l1d.loads",         o["l1d.loads"],        real.get("l1d_loads")),
        ("l1d.stores",        o["l1d.stores"],       real.get("l1d_stores")),
        ("l1d.load_miss",     o["l1d.load_misses"],  real.get("l1d_ld_miss")),
        ("l1d.store_miss",    o["l1d.store_misses"], real.get("l1d_st_miss")),
        # L2 (private second level) miss == requests leaving L1Cache to L2Cache.
        ("l2.miss_to_llc",    o["l2.misses"],        real.get("l2_miss_to_llc")),
        # LLC lookups == requests arriving at L2Cache (directory). CHA requests
        # is the same physical count.
        ("llc.lookups",       o["cha.requests"],     real.get("llc_lookups")),
        ("cha.requests",      o["cha.requests"],     real.get("llc_lookups")),
        ("llc.miss_to_dram",  o["llc.misses"],       real.get("llc_miss_to_dram")),
        # Snoops forwarded back to L1: REMOTE_HIT_* + WB_REQUIRED on oracle side
        # <-> Fwd_GET{S,X} totals on real side.
        ("cha.snp_fwd",       o["cha.dir_lookup.snp"], real.get("fwd_total")),
        ("cha.remote_hit",    o["cha.remote_hit"],   real.get("fwd_get_s")),
        ("cha.wb_required",   o["cha.wb_required"],  real.get("fwd_get_x")),
    ]

    out = []
    for tier, oc, rc in rows:
        out.append({
            "workload": workload,
            "tier": tier,
            "oracle": oc,
            "real": rc,
            "errpct": errpct(oc, rc),
        })
    return out


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snap-dir", required=True,
                    help="Oracle shared_pmu.jsonl 所在目录")
    ap.add_argument("--snap-tag", default="warmup0.wm-none",
                    help="文件名中介标签；完整名 <workload>.<tag>.shared_pmu.jsonl")
    ap.add_argument("--raw-root", default=str(ROOT / "data/raw_eval11_8c"))
    ap.add_argument("--workload", action="append", default=[])
    ap.add_argument("--out-name", default="pmu_layers_diag",
                    help="写入 <snap-dir>/<out-name>.{txt,json}")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    snap_dir = Path(args.snap_dir)
    raw_root = Path(args.raw_root)
    workloads = args.workload or DEFAULT_WORKLOADS

    all_rows: List[dict] = []
    lines: List[str] = []
    bar = "=" * 96
    lines.append(bar)
    lines.append("PMU 误差分层 (err% = |oracle - real| / real * 100)")
    lines.append(
        "real L0Cache <-> oracle L1d ; L1Cache <-> oracle L2 ; "
        "L2Cache <-> oracle LLC/CHA")
    lines.append(bar)
    lines.append(
        f"{'workload':<26} {'tier':<18} {'oracle':>12} {'real':>12} {'err%':>8}")
    lines.append("-" * 96)

    for w in workloads:
        rows = collect_rows(snap_dir, raw_root, w, args.snap_tag)
        for r in rows:
            lines.append(
                f"{r['workload']:<26} {r['tier']:<18} "
                f"{fmt(r['oracle'])} {fmt(r['real'])} "
                f"{fmt_pct(r['errpct'])}"
            )
        lines.append("")
        all_rows.extend(rows)

    out_txt = snap_dir / f"{args.out_name}.txt"
    out_json = snap_dir / f"{args.out_name}.json"
    out_txt.write_text("\n".join(lines) + "\n")
    out_json.write_text(json.dumps(all_rows, indent=2))
    print("\n".join(lines))
    print(f"\nreport -> {out_txt}\njson   -> {out_json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
