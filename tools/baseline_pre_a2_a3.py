#!/usr/bin/env python3
"""Step 2: A3-pre baseline 实测。

在改动 oracle 之前抓 4 个 workload records.micro 的现状分布，
作为 A2/A3/方案2/walker 改动后的对照基线。

输入: m5out_*/tao_trace/*.records.micro.jsonl
输出:
  - diagnosis/baseline_pre_a2_a3.json  汇总分布
  - 控制台每 workload 单独一段表

测量项:
  1. oracle_source=0/1 占比（0=packet 真值，1=fallback 推断）
  2. coh_oracle 分布（按枚举值）
  3. path_class 分布
  4. mesi_before 分布
  5. W7: outstanding DRAM miss 窗口式 dedup 估算
     验证 A2 思路（30866 → ~876 是否可达）
"""
import argparse
import glob
import json
import os
import sys
from collections import Counter, defaultdict

# coh 枚举（mem_events.jsonl 与 simulator.hpp 共用）
# 来自 pmu_report.py L35-36
COH_L1, COH_R_CLEAN, COH_R_DIRTY, COH_LLC, COH_DRAM, COH_WB, COH_L2 = \
    1, 2, 3, 4, 5, 6, 7

COH_NAME = {
    0: "INIT",
    1: "L1",
    2: "R_CLEAN",
    3: "R_DIRTY",
    4: "LLC",
    5: "DRAM",
    6: "WB",
    7: "L2",
}

# path_class 枚举（来自 tao_trace.cc 内部）
PATH_NAME = {
    0: "NONE",
    1: "L1",
    2: "L2",
    3: "LLC",
    4: "DRAM",
    5: "FWD_S",
    6: "FWD_M",
    7: "WB",
}

MESI_NAME = {0: "I", 1: "S", 2: "E", 3: "M"}

WORKLOADS = {
    "w1": "m5out_w1_detailed_micro_4x800",
    "w3": "m5out_v3_w3",
    "w4": "m5out_v3_w4",
    "w7": "m5out_w7_detailed_micro_4x1500",
}


def iter_records(m5out_dir):
    pat = os.path.join(m5out_dir, "tao_trace",
                       "*.records.micro.jsonl")
    for path in sorted(glob.glob(pat)):
        with open(path) as f:
            for line in f:
                if not line.strip():
                    continue
                yield json.loads(line)


def iter_mem_events(m5out_dir):
    path = os.path.join(m5out_dir, "tao_trace", "all_mem_events.merged.jsonl")
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            if not line.strip():
                continue
            yield json.loads(line)


def analyze_workload(name, m5out_dir):
    # ----- records.micro 分布（按 tao_trace.hh CoherenceAction 枚举） -----
    REC_COH_NAME = {
        0: "UNKNOWN", 1: "L1_HIT", 2: "R_CLEAN", 3: "R_DIRTY",
        4: "LLC_HIT", 5: "DRAM", 6: "WB", 7: "L2_HIT",
    }

    src_cnt = Counter()
    rec_coh_cnt = Counter()
    path_cnt = Counter()
    mesi_cnt = Counter()
    rec_total = 0
    rec_load_total = 0
    rec_store_total = 0
    rec_load_dram = 0
    rec_store_dram = 0

    for rec in iter_records(m5out_dir):
        rec_total += 1
        src_cnt[rec["oracle_source"]] += 1
        rec_coh_cnt[rec["coh_oracle"]] += 1
        path_cnt[rec["path_class"]] += 1
        mesi_cnt[rec["mesi_before"]] += 1
        if rec["is_load"]:
            rec_load_total += 1
            if rec["coh_oracle"] == 5:  # DRAM in tao_trace.hh
                rec_load_dram += 1
        if rec["is_store"]:
            rec_store_total += 1
            if rec["coh_oracle"] == 5:
                rec_store_dram += 1

    # ----- mem_events.merged.jsonl 分布（pmu_report 实际消费这一份） -----
    # coh 枚举：1=L1,2=R_CLEAN,3=R_DIRTY,4=LLC,5=DRAM,6=WB,7=L2
    ev_coh_cnt_load = Counter()
    ev_coh_cnt_store = Counter()
    ev_src_cnt = Counter()
    ev_load_dram_lines = []  # for MSHR estimate
    ev_total = 0

    for ev in iter_mem_events(m5out_dir):
        et = ev.get("event_type", "")
        if et != "request":
            continue
        ev_total += 1
        ev_src_cnt[ev.get("oracle_source", -1)] += 1
        coh = ev.get("coh_oracle", 0)
        is_store = ev.get("is_store", 0) == 1
        if is_store:
            ev_coh_cnt_store[coh] += 1
        else:
            ev_coh_cnt_load[coh] += 1
            if coh == 5:  # DRAM
                ev_load_dram_lines.append(
                    int(ev.get("cacheline_addr", 0)) >> 6)

    # MSHR 窗口式 dedup 估算（W7 验证 A2 思路）
    mshr_estimates = {}
    for window in (50, 100, 200, 500, 1000, 5000):
        outstanding = {}
        coalesced = 0
        unique_misses = 0
        for seq, cl in enumerate(ev_load_dram_lines):
            stale = [k for k, v in outstanding.items() if seq - v > window]
            for k in stale:
                del outstanding[k]
            if cl in outstanding:
                coalesced += 1
                outstanding[cl] = seq
            else:
                unique_misses += 1
                outstanding[cl] = seq
        mshr_estimates[window] = {
            "raw_load_dram": len(ev_load_dram_lines),
            "after_mshr": unique_misses,
            "coalesced": coalesced,
            "ratio": (unique_misses / len(ev_load_dram_lines)
                      if ev_load_dram_lines else 1.0),
        }
    unique_cl = len(set(ev_load_dram_lines))
    mshr_estimates["infinite"] = {
        "raw_load_dram": len(ev_load_dram_lines),
        "after_mshr": unique_cl,
        "coalesced": len(ev_load_dram_lines) - unique_cl,
        "ratio": (unique_cl / len(ev_load_dram_lines)
                  if ev_load_dram_lines else 1.0),
    }

    summary = {
        "workload": name,
        "m5out_dir": m5out_dir,
        "records_micro": {
            "total": rec_total,
            "loads": rec_load_total,
            "stores": rec_store_total,
            "load_dram(coh==5)": rec_load_dram,
            "store_dram(coh==5)": rec_store_dram,
            "oracle_source": {
                "packet(0)": src_cnt[0],
                "fallback(1)": src_cnt[1],
                "fallback_ratio": src_cnt[1] / rec_total if rec_total else 0,
            },
            "coh_oracle": {REC_COH_NAME.get(k, f"UNK_{k}"): v
                           for k, v in rec_coh_cnt.most_common()},
            "path_class": {PATH_NAME.get(k, f"UNK_{k}"): v
                           for k, v in path_cnt.most_common()},
            "mesi_before": {MESI_NAME.get(k, f"UNK_{k}"): v
                            for k, v in mesi_cnt.most_common()},
        },
        "mem_events": {
            "request_total": ev_total,
            "oracle_source": {
                "packet(0)": ev_src_cnt[0],
                "fallback(1)": ev_src_cnt[1],
                "fallback_ratio": (ev_src_cnt[1] / ev_total
                                   if ev_total else 0),
            },
            "load_coh_oracle": {COH_NAME.get(k, f"UNK_{k}"): v
                                for k, v in ev_coh_cnt_load.most_common()},
            "store_coh_oracle": {COH_NAME.get(k, f"UNK_{k}"): v
                                 for k, v in ev_coh_cnt_store.most_common()},
            "llc_load_misses(DRAM)": ev_coh_cnt_load.get(5, 0),
            "llc_store_misses(DRAM)": ev_coh_cnt_store.get(5, 0),
        },
        "mshr_dedup_estimates_on_mem_events": mshr_estimates,
    }
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="diagnosis/baseline_pre_a2_a3.json")
    args = ap.parse_args()

    os.makedirs(os.path.dirname(args.out), exist_ok=True)

    all_summary = {}
    for name, d in WORKLOADS.items():
        if not os.path.isdir(d):
            print(f"[SKIP] {name}: dir not found: {d}", file=sys.stderr)
            continue
        print(f"[RUN ] {name}: scanning {d} ...", file=sys.stderr)
        all_summary[name] = analyze_workload(name, d)

    with open(args.out, "w") as f:
        json.dump(all_summary, f, indent=2)
    print(f"[OK  ] wrote {args.out}", file=sys.stderr)

    # 控制台简表
    print()
    print(f"{'wl':<4} {'rec_total':>10} {'rec_load':>10} "
          f"{'ev_load_DRAM':>14} {'rec_fb%':>8} {'ev_fb%':>8}")
    for name, s in all_summary.items():
        rm = s["records_micro"]
        em = s["mem_events"]
        print(f"{name:<4} {rm['total']:>10} {rm['loads']:>10} "
              f"{em['llc_load_misses(DRAM)']:>14} "
              f"{rm['oracle_source']['fallback_ratio']*100:>7.2f}% "
              f"{em['oracle_source']['fallback_ratio']*100:>7.2f}%")

    print()
    print("=== W7 MSHR dedup estimates (on mem_events) ===")
    if "w7" in all_summary:
        for w, e in all_summary["w7"][
                "mshr_dedup_estimates_on_mem_events"].items():
            print(f"  window={w!s:>8}  raw={e['raw_load_dram']:>6}  "
                  f"after_mshr={e['after_mshr']:>6}  "
                  f"coalesced={e['coalesced']:>6}  "
                  f"ratio={e['ratio']*100:.2f}%")

    print()
    print("=== records.micro coh_oracle 分布 ===")
    for name, s in all_summary.items():
        print(f"  {name}: {s['records_micro']['coh_oracle']}")
    print()
    print("=== mem_events load coh_oracle 分布 ===")
    for name, s in all_summary.items():
        print(f"  {name}: {s['mem_events']['load_coh_oracle']}")
    print()
    print("=== mem_events store coh_oracle 分布 ===")
    for name, s in all_summary.items():
        print(f"  {name}: {s['mem_events']['store_coh_oracle']}")


if __name__ == "__main__":
    main()
