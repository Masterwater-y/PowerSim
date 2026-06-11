#!/usr/bin/env bash
# Run V9.6 ROI 实验 P3：仅跑 W5..W10（5 个新 µbench + STREAM）。
# 输出到 tmp/runP3_v96_<时间戳>，互不影响 runC_v96_clean。
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$(cd "$REPO/.." && pwd)"

GEM5="$ROOT/gem5/build/X86_MESI_Three_Level/gem5.opt"
CFG="$REPO/configs/run_mt_mvp.py"
REFSIM="$REPO/mesi_ref_sim/build/mesi_ref_sim"
COMPARE="$REPO/mesi_ref_sim/scripts/compare_oracle.py"
COMPARE_I="$REPO/mesi_ref_sim/scripts/compare_ifetch.py"
PMU="$REPO/mesi_ref_sim/scripts/pmu_report.py"
WL="$REPO/workloads"

export LD_LIBRARY_PATH="/opt/gcc-11/lib64:/root/.pyenv/versions/3.8.0/lib:${LD_LIBRARY_PATH:-}"

OUT_BASE="${1:-$REPO/tmp/runP3_v96_$(date +%Y%m%d_%H%M%S)}"
mkdir -p "$OUT_BASE"
SUMMARY="$OUT_BASE/SUMMARY.txt"
: > "$SUMMARY"
echo "[runP3] OUT=$OUT_BASE start $(date)" | tee -a "$SUMMARY"

run_one() {
  local NAME=$1 BIN=$2; shift 2
  local ARGS=("$@")
  local OUT="$OUT_BASE/$NAME"
  rm -rf "$OUT"; mkdir -p "$OUT"
  echo "=== $NAME start $(date +%H:%M:%S) ===" | tee -a "$SUMMARY"
  "$GEM5" --outdir="$OUT" "$CFG" \
      --cmd "$BIN" --workload-args "${ARGS[@]}" --num-cores 4 \
      --require-roi \
      > "$OUT/gem5.log" 2>&1 \
      || { echo "[$NAME] gem5 FAIL" | tee -a "$SUMMARY"; return 0; }
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
      2> "$OUT/refsim.log" || { echo "[$NAME] refsim FAIL" | tee -a "$SUMMARY"; return 0; }
  python3 "$COMPARE"   "$OUT/mem_events.merged.jsonl" "$OUT/pred.jsonl" > "$OUT/compare.log"        2>&1 || true
  python3 "$COMPARE_I" "$OUT/mem_events.merged.jsonl" "$OUT/pred.jsonl" > "$OUT/compare.ifetch.log" 2>&1 || true
  python3 "$PMU"       "$OUT/mem_events.merged.jsonl" "$OUT/pred.jsonl" --uarch-profile "$PROFILE" \
                                                                       > "$OUT/pmu.log"            2>&1 || true
  local M=$(grep -E "^matched\s"     "$OUT/compare.log"        | head -1)
  local X=$(grep -E "^mismatched\s"  "$OUT/compare.log"        | head -1)
  local IM=$(grep -E "^matched\s"    "$OUT/compare.ifetch.log" | head -1)
  local IX=$(grep -E "^mismatched\s" "$OUT/compare.ifetch.log" | head -1)
  echo "[$NAME done $(date +%H:%M:%S)] dside: $M | $X ; iside: $IM | $IX" | tee -a "$SUMMARY"
}

run_one W5_branch_storm  "$WL/mt_branch_storm/mt_branch_storm"   4 800
run_one W6_indirect_jump "$WL/mt_indirect_jump/mt_indirect_jump" 4 800
run_one W7_simd_fp       "$WL/mt_simd_fp/mt_simd_fp"             4 400
run_one W8_stride_pf     "$WL/mt_stride_pf/mt_stride_pf"         4 1
run_one W9_int_div       "$WL/mt_int_div/mt_int_div"             4 800
run_one W10_stream       "$WL/mt_stream/mt_stream"               4 1
run_one W11_stream_mix   "$WL/mt_stream_mix/mt_stream_mix"       4 2 256 0
run_one W12_stencil2d    "$WL/mt_stencil2d/mt_stencil2d"         4 2 256 0
run_one W13_graph_walk   "$WL/mt_graph_walk/mt_graph_walk"       4 1 8 0
run_one W14_branch_state "$WL/mt_branch_state_machine/mt_branch_state_machine" 4 1 8 0
run_one W15_indirect     "$WL/mt_indirect_dispatch/mt_indirect_dispatch"       4 1 16 0

echo "=== ALL DONE $(date +%H:%M:%S) ===" | tee -a "$SUMMARY"
