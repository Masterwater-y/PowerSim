#!/usr/bin/env bash
# taogen/scripts/run_experiment.sh
# 一键复现 V9.5 A2/A3 + L3-fix 实验：
#   gem5 detailed run → ref_sim 重放 → oracle↔ref_sim 17/17 bit-exact → PMU vs ruby.
#
# 用法：
#   bash scripts/run_experiment.sh [OUT_DIR]
# 环境变量：
#   REQUIRE_ROI=1   启用 V9.6 ROI 闸门（默认 0 = V9.5 全程 emit）
#   NUM_CORES       默认 4
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$(cd "$REPO/.." && pwd)"   # simulators/

GEM5="${GEM5:-$ROOT/gem5/build/X86_MESI_Three_Level/gem5.opt}"
CFG="$REPO/configs/run_mt_mvp.py"
REFSIM="$REPO/mesi_ref_sim/build/mesi_ref_sim"
COMPARE="$REPO/mesi_ref_sim/scripts/compare_oracle.py"
COMPARE_I="$REPO/mesi_ref_sim/scripts/compare_ifetch.py"
PMU="$REPO/mesi_ref_sim/scripts/pmu_report.py"
WL="$REPO/workloads"

REQUIRE_ROI="${REQUIRE_ROI:-0}"
NUM_CORES="${NUM_CORES:-4}"
ROI_FLAG=""
if [ "$REQUIRE_ROI" = "1" ]; then ROI_FLAG="--require-roi"; fi

# V9.6: gem5 链接的 libstdc++ / libpython 不在系统默认路径，必须显式注入
export LD_LIBRARY_PATH="/opt/gcc-11/lib64:/root/.pyenv/versions/3.8.0/lib:${LD_LIBRARY_PATH:-}"

OUT_BASE="${1:-$REPO/tmp/run_$(date +%Y%m%d_%H%M%S)}"
mkdir -p "$OUT_BASE"
SUMMARY="$OUT_BASE/SUMMARY.txt"
: > "$SUMMARY"
echo "[run_experiment] REQUIRE_ROI=$REQUIRE_ROI NUM_CORES=$NUM_CORES OUT=$OUT_BASE" \
    | tee -a "$SUMMARY"

run_one() {
  local NAME=$1 BIN=$2; shift 2
  local ARGS=("$@")
  local OUT="$OUT_BASE/$NAME"
  rm -rf "$OUT"; mkdir -p "$OUT"
  echo "=== $NAME ==="
  "$GEM5" --outdir="$OUT" "$CFG" \
      --cmd "$BIN" --workload-args "${ARGS[@]}" --num-cores "$NUM_CORES" \
      $ROI_FLAG \
      > "$OUT/gem5.log" 2>&1
  cat "$OUT/tao_trace/"*.mem_events.jsonl 2>/dev/null \
      | python3 -c "
import json,sys
rows=[]
for ln in sys.stdin:
    s=ln.strip()
    if not s.startswith('{'): continue
    try: rows.append(json.loads(s))
    except Exception: pass
rows.sort(key=lambda r:(r.get('commit_tick',0), r.get('seq',0)))
for r in rows: print(json.dumps(r, separators=(',',':')))
" > "$OUT/mem_events.merged.jsonl"
  local PROFILE="$OUT/uarch_profile.json"
  "$REFSIM" "$PROFILE" "$OUT/mem_events.merged.jsonl" "$OUT/pred.jsonl" \
      2> "$OUT/refsim.log" || { echo "[$NAME] refsim FAIL" | tee -a "$SUMMARY"; return 1; }
  python3 "$COMPARE"   "$OUT/mem_events.merged.jsonl" "$OUT/pred.jsonl" > "$OUT/compare.log"        2>&1 || true
  python3 "$COMPARE_I" "$OUT/mem_events.merged.jsonl" "$OUT/pred.jsonl" > "$OUT/compare.ifetch.log" 2>&1 || true
  python3 "$PMU"       "$OUT/mem_events.merged.jsonl" "$OUT/pred.jsonl" --uarch-profile "$PROFILE" \
                                                                       > "$OUT/pmu.log"            2>&1 || true
  local M=$(grep -E "^matched\s"     "$OUT/compare.log"        | head -1)
  local X=$(grep -E "^mismatched\s"  "$OUT/compare.log"        | head -1)
  local IM=$(grep -E "^matched\s"    "$OUT/compare.ifetch.log" | head -1)
  local IX=$(grep -E "^mismatched\s" "$OUT/compare.ifetch.log" | head -1)
  echo "[$NAME] dside: $M | $X ; iside: $IM | $IX" | tee -a "$SUMMARY"
}

run_one W1_compute_int   "$WL/mt_compute_int/mt_compute_int"     4 800
run_one W2_chase_dram    "$WL/mt_chase_dram/mt_chase_dram"       4 1500
run_one W3_micro_coh     "$WL/mt_micro_coh/mt_micro_coh"         4 5000
run_one W4_coh_stress    "$WL/mt_coh_stress/mt_coh_stress"       4 1000
# P3 新增：5 µbench 覆盖 BR_COND/BR_IND/SIMD/FP/PF/INT_DIV 盲区 + 1 真实负载 STREAM
run_one W5_branch_storm  "$WL/mt_branch_storm/mt_branch_storm"   4 800
run_one W6_indirect_jump "$WL/mt_indirect_jump/mt_indirect_jump" 4 800
run_one W7_simd_fp       "$WL/mt_simd_fp/mt_simd_fp"             4 400
run_one W8_stride_pf     "$WL/mt_stride_pf/mt_stride_pf"         4 2
run_one W9_int_div       "$WL/mt_int_div/mt_int_div"             4 800
run_one W10_stream       "$WL/mt_stream/mt_stream"               4 2
run_one W11_stream_mix   "$WL/mt_stream_mix/mt_stream_mix"       4 2 256 0
run_one W12_stencil2d    "$WL/mt_stencil2d/mt_stencil2d"         4 2 256 0
run_one W13_graph_walk   "$WL/mt_graph_walk/mt_graph_walk"       4 1 8 0
run_one W14_branch_state "$WL/mt_branch_state_machine/mt_branch_state_machine" 4 1 8 0
run_one W15_indirect     "$WL/mt_indirect_dispatch/mt_indirect_dispatch"       4 1 16 0

echo
echo "=== SUMMARY ==="
cat "$SUMMARY"
echo "out: $OUT_BASE"
