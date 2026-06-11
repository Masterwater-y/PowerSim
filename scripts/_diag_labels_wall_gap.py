"""
诊断 gem5 TaoTrace probe 在 multi-core 配置下，labels.micro.jsonl 是否漏记 wall stall。

口径：
- wall_cycles = (last_commit_tick - first_commit_tick) / ticks_per_cycle
- label_cycles = sum(fetch_lat + exec_lat) over rows
- ratio = label_cycles / wall_cycles

ratio ≈ 1.0  → label 完整记录了 wall stall（OOO 流水线叠加抵消后接近线性）
ratio << 1.0 → wall 中存在大量 cycles 没有进入任何 µop 的 fetch_lat/exec_lat
              （ROB/LSQ 溢出阻塞、远端 NUMA fetch 等被吃掉的等待）

每核独立报告；分多个 µop 区间避免开头/末尾偏置。
"""
import json
import os
import glob
import sys

DS = sys.argv[1] if len(sys.argv) > 1 else "${TAO_INFER_ROOT}/data/W11_stream_mix_4c_u500000"
TPC = float(sys.argv[2]) if len(sys.argv) > 2 else 333.0

buckets = [
    ("warmup", 0, 50000),
    ("eval_first_50K", 50000, 100000),
    ("eval_mid_150K", 100000, 250000),
    ("eval_last_250K", 250000, 500000),
    ("full_500K", 0, 500000),
]

print(f"DS={DS} TPC={TPC}")
for cid in range(8):
    cs = sorted(glob.glob(os.path.join(DS, f"tao_trace/*cores{cid}*.labels.micro.jsonl")))
    if not cs:
        continue
    p = cs[0]
    rows = []
    with open(p) as f:
        for line in f:
            rows.append(json.loads(line))
    print(f"\n=== core{cid} total_rows={len(rows)} ===")
    for tag, lo, hi in buckets:
        sub = rows[lo:hi]
        if not sub:
            continue
        flat = sum(r.get("fetch_lat", 0) for r in sub)
        elat = sum(r.get("exec_lat", 0) for r in sub)
        ct0 = int(sub[0].get("commit_tick", 0))
        ct1 = int(sub[-1].get("commit_tick", 0))
        wall_cyc = (ct1 - ct0) / TPC
        lab_cyc = flat + elat
        cpi_wall = wall_cyc / max(1, len(sub))
        cpi_lab = lab_cyc / max(1, len(sub))
        ratio = lab_cyc / wall_cyc if wall_cyc > 0 else 0.0
        print(
            f"  {tag:>15} rows[{lo:>6}:{hi:>6}] n={len(sub):>6}"
            f"  CPI_wall={cpi_wall:6.3f}  CPI_label={cpi_lab:6.3f}"
            f"  label/wall={ratio:5.3f}"
            f"  fetch_sum={flat:>12d}  exec_sum={elat:>12d}"
        )
