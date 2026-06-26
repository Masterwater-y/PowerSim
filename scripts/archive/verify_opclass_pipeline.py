#!/usr/bin/env python3
"""Verify op_class 解析正确并贯通 tokenizer。

输入：records.micro.jsonl 目录（默认烟雾测试产物）。
检查：
  1. op_class 字段存在率、值域 [0,88]
  2. 与 is_* 旗位一致性抽查（IntAlu/IntDiv/MemRead/FloatMemRead/MemWrite 等）
  3. tokenizer.opclass_id() 返回的就是 op_class 原值
  4. encode_uop 6 token 范围与 vocab 一致
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from model import tokenizer as tk  # noqa: E402

# 关键枚举（OpClass.hh）
OPCLASS_NAME = {
    0: "No_OpClass", 1: "IntAlu", 2: "IntMult", 3: "IntDiv",
    4: "FloatAdd", 5: "FloatCmp", 6: "FloatCvt", 7: "FloatMult",
    8: "FloatMultAcc", 9: "FloatDiv", 10: "FloatMisc", 11: "FloatSqrt",
    12: "SimdAdd", 14: "SimdAlu", 18: "SimdMult", 21: "SimdShift",
    23: "SimdDiv", 24: "SimdSqrt", 25: "SimdFloatAdd", 27: "SimdFloatCmp",
    28: "SimdFloatCvt", 29: "SimdFloatDiv", 31: "SimdFloatMult",
    56: "MemRead", 57: "MemWrite", 58: "FloatMemRead", 59: "FloatMemWrite",
    60: "InstPrefetch", 88: "System",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--trace-dir",
        default="/tmp/smoke_opclass/W_search_index_proxy/tao_trace",
    )
    ap.add_argument("--max-rows-per-file", type=int, default=200000)
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(
        args.trace_dir, "*records.micro.jsonl")))
    if not files:
        print(f"[err] no records.micro.jsonl under {args.trace_dir}",
              file=sys.stderr)
        return 1

    total = 0
    have_opclass = 0
    oc_hist = Counter()
    consistency = Counter()
    sample_uop = None

    for fp in files:
        with open(fp) as f:
            n = 0
            for ln in f:
                ln = ln.strip()
                if not ln.startswith("{"):
                    continue
                rec = json.loads(ln)
                total += 1
                n += 1
                if n > args.max_rows_per_file:
                    break
                oc = rec.get("op_class")
                if oc is None:
                    continue
                have_opclass += 1
                oc = int(oc)
                oc_hist[oc] += 1
                # 一致性抽查
                if oc == 1 and not rec.get("is_int"):
                    consistency["IntAlu_no_is_int"] += 1
                if oc == 3 and not rec.get("is_int"):
                    consistency["IntDiv_no_is_int"] += 1
                if oc == 56 and not rec.get("is_load"):
                    consistency["MemRead_no_is_load"] += 1
                if oc == 57 and not (rec.get("is_store") or rec.get("is_atomic")):
                    consistency["MemWrite_no_is_store"] += 1
                if oc == 58 and not rec.get("is_load"):
                    consistency["FloatMemRead_no_is_load"] += 1
                if oc == 59 and not (rec.get("is_store") or rec.get("is_atomic")):
                    consistency["FloatMemWrite_no_is_store"] += 1
                if oc in (4, 7, 8, 9, 11) and not rec.get("is_fp"):
                    consistency["Float_no_is_fp"] += 1
                if oc in (29, 31) and not rec.get("is_simd"):
                    consistency["Simd_no_is_simd"] += 1
                if sample_uop is None and oc == 3:
                    sample_uop = rec  # 拿一个 IntDiv 样本看 token 编码

    print(f"[stat] files={len(files)} rows_scanned={total} have_op_class={have_opclass}")
    if have_opclass == 0:
        print("[FAIL] op_class 字段没出现，需要确认 gem5 build 是否带 patch")
        return 2

    # 值域检查
    bad = [k for k in oc_hist if not (0 <= k < tk.N_OPCLASS)]
    if bad:
        print(f"[FAIL] 值域越界: {bad}")
        return 3
    print(f"[ok] op_class 值域 ⊂ [0,{tk.N_OPCLASS - 1}]")

    # top-10 桶
    print("[stat] top-10 op_class 分布:")
    for oc, cnt in oc_hist.most_common(10):
        name = OPCLASS_NAME.get(oc, f"<{oc}>")
        print(f"  oc={oc:3d} ({name:<14}) {cnt:>8d}  {100.0*cnt/have_opclass:5.2f}%")

    # 一致性
    if consistency:
        print("[warn] 与 is_* 旗位不一致样本:")
        for k, v in consistency.most_common():
            print(f"  {k}: {v}")
    else:
        print("[ok] 抽查 IntAlu/IntDiv/MemRead/MemWrite/Float/Simd 与 is_* 一致")

    # tokenizer 端到端
    if sample_uop is None:
        # 没 IntDiv 就拿第一条
        with open(files[0]) as f:
            for ln in f:
                ln = ln.strip()
                if ln.startswith("{"):
                    sample_uop = json.loads(ln)
                    break
    assert sample_uop is not None
    tok_oc = tk.opclass_id(sample_uop)
    raw_oc = int(sample_uop.get("op_class", -1))
    if tok_oc != raw_oc:
        print(f"[FAIL] tokenizer.opclass_id 返回 {tok_oc} 但原值 {raw_oc}")
        return 4
    print(f"[ok] tokenizer.opclass_id == op_class ({raw_oc} = {OPCLASS_NAME.get(raw_oc, '?')})")

    encoded = tk.encode_uop(sample_uop)
    print(f"[ok] encode_uop sample (op_class={raw_oc}): {encoded}")

    # vocab 覆盖检查
    layout = tk.VocabLayout.build()
    vocab_set = set(layout.tokens)
    missing = [t for t in encoded if t not in vocab_set]
    if missing:
        print(f"[FAIL] token 不在 vocab: {missing}")
        return 5
    print(f"[ok] 6 token 全在 vocab，total vocab size = {len(vocab_set)}")
    print(f"[ok] N_OPCLASS={tk.N_OPCLASS}，覆盖率（实际出现/词表）= "
          f"{len(oc_hist)}/{tk.N_OPCLASS} = {100.0*len(oc_hist)/tk.N_OPCLASS:.1f}%")

    return 0


if __name__ == "__main__":
    sys.exit(main())
