#!/usr/bin/env bash
# LLMSim Phase0 数据采集：用 taogen 已编译 gem5.opt 跑 8 个 workload 的 ROI run。
# 产出 records.micro(functional 输入) + labels.micro(µarch 标签) per core。
set -uo pipefail

REPO=/data00/yinhaolang/taogen
GEM5=/data00/yinhaolang/gem5/build/X86_MESI_Three_Level/gem5.opt
CFG=$REPO/configs/run_mt_mvp.py
WL=$REPO/workloads
OUT_BASE=/data00/yinhaolang/LLMSim/data/raw
NUM_CORES=${NUM_CORES:-4}

# gem5 运行所需的库路径（新版 libstdc++ 含 GLIBCXX_3.4.30 + uv libpython3.11）
export LD_LIBRARY_PATH="/data00/yinhaolang/LLMSim/data/_gem5libs:/opt/gcc-11.5.0/lib64:${LD_LIBRARY_PATH:-}"

mkdir -p "$OUT_BASE"

run_one() {
  local NAME=$1 BIN=$2; shift 2
  local OUT="$OUT_BASE/$NAME"
  rm -rf "$OUT"; mkdir -p "$OUT"
  echo "=== [$NAME] start $(date +%T) ==="
  timeout 1800 "$GEM5" --outdir="$OUT" "$CFG" \
      --cmd "$BIN" --workload-args "$@" --num-cores "$NUM_CORES" \
      --require-roi > "$OUT/gem5.log" 2>&1
  local rc=$?
  local nrec=$(cat "$OUT"/tao_trace/*.records.micro.jsonl 2>/dev/null | wc -l)
  echo "=== [$NAME] exit=$rc records=$nrec $(date +%T) ==="
}

run_one W1_compute_int   "$WL/mt_compute_int/mt_compute_int"     "$NUM_CORES" 800
run_one W2_chase_dram    "$WL/mt_chase_dram/mt_chase_dram"       "$NUM_CORES" 1500
run_one W3_micro_coh     "$WL/mt_micro_coh/mt_micro_coh"         "$NUM_CORES" 5000
run_one W4_coh_stress    "$WL/mt_coh_stress/mt_coh_stress"       "$NUM_CORES" 1000
run_one W5_branch_storm  "$WL/mt_branch_storm/mt_branch_storm"   "$NUM_CORES" 800
run_one W6_indirect_jump "$WL/mt_indirect_jump/mt_indirect_jump" "$NUM_CORES" 800
run_one W7_simd_fp       "$WL/mt_simd_fp/mt_simd_fp"             "$NUM_CORES" 400
run_one W8_stream        "$WL/mt_stream/mt_stream"               "$NUM_CORES" 2

echo "=== ALL DONE $(date +%T) ==="
