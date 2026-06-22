"""downsample_workload.py — 对 windows.jsonl 按 workload 做上限下采样。

用途：phased_mix trace 体积是其他负载的 ~9×（54GB vs 5–10GB），quota 切窗下
phased_mix 单 workload 占 55% (8426/15245)，会主导训练梯度。
本脚本按 --cap 给每个 workload 设上限，固定 seed 蓄水池抽样，输出到新目录。

输出：
  <out_dir>/windows.jsonl          下采样后的样本
  <out_dir>/windows.maxlen{N}.ids_cache/   自动调用 prepare_dataset_cache 重建

用法：
  python scripts/downsample_workload.py \
    --in data/windows_quota_maxlen32768/windows.jsonl \
    --out data/windows_quota_maxlen32768_balanced \
    --cap W_phased_mix=1500 \
    --max-len 32768 --seed 1234
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
from collections import defaultdict


def parse_caps(items: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in items:
        if "=" not in it:
            raise ValueError(f"--cap expects W_NAME=N, got: {it}")
        k, v = it.split("=", 1)
        out[k.strip()] = int(v)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cap", action="append", default=[],
                    help="W_NAME=N，可多次。未指定的 workload 不限")
    ap.add_argument("--max-len", type=int, default=32768)
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

    caps = parse_caps(args.cap)
    rng = random.Random(args.seed)

    # 先过一遍统计每个 workload 的样本下标
    wl_idx: dict[str, list[int]] = defaultdict(list)
    n_total = 0
    with open(args.inp) as f:
        for i, ln in enumerate(f):
            s = ln.strip()
            if not s.startswith("{"):
                continue
            rec = json.loads(s)
            wl_idx[rec["workload"]].append(i)
            n_total += 1

    keep_set: set[int] = set()
    print(f"[downsample] total={n_total}", file=sys.stderr)
    for w in sorted(wl_idx):
        idx = wl_idx[w]
        cap = caps.get(w)
        if cap is None or len(idx) <= cap:
            keep_set.update(idx)
            print(f"  {w}: keep all {len(idx)}", file=sys.stderr)
        else:
            picked = rng.sample(idx, cap)
            keep_set.update(picked)
            print(f"  {w}: {len(idx)} -> {cap}", file=sys.stderr)

    os.makedirs(args.out, exist_ok=True)
    out_jsonl = os.path.join(args.out, "windows.jsonl")
    n_kept = 0
    with open(args.inp) as fin, open(out_jsonl, "w") as fout:
        for i, ln in enumerate(fin):
            if i in keep_set:
                fout.write(ln)
                n_kept += 1
    print(f"[downsample] kept={n_kept} -> {out_jsonl}", file=sys.stderr)

    if args.no_cache:
        return
    here = os.path.dirname(os.path.abspath(__file__))
    cmd = [
        sys.executable,
        os.path.join(here, "prepare_dataset_cache.py"),
        "--data", out_jsonl,
        "--max-len", str(args.max_len),
    ]
    print(f"[downsample] {' '.join(cmd)}", file=sys.stderr)
    subprocess.run(cmd, check=True)


if __name__ == "__main__":
    main()
