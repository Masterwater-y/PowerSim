#!/usr/bin/env bash
# Resume V9.6 ROI 实验：仅跑 W2/W3/W4，结果追加到 tmp/runC_v96_clean。
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

OUT_BASE="$REPO/tmp/runC_v96_clean"
SUMMARY="$OUT_BASE/SUMMARY.txt"

run_one() {
  local NAME=$1 BIN=$2; shift 2
  local ARGS=("$@")
  local OUT="$OUT_BASE/$NAME"
  rm -rf "$OUT"; mkdir -p "$OUT"
  echo "=== $NAME start $(date +%H:%M:%S) ===" | tee -a "$SUMMARY"
  "$GEM5" --outdir="$OUT" "$CFG" \
      --cmd "$BIN" --workload-args "${ARGS[@]}" --num-cores 4 \
      --require-roi \
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

run_one W2_chase_dram  "$WL/mt_chase_dram/mt_chase_dram"   4 1500
run_one W3_micro_coh   "$WL/mt_micro_coh/mt_micro_coh"     4 5000
run_one W4_coh_stress  "$WL/mt_coh_stress/mt_coh_stress"   4 1000

echo "=== ALL DONE $(date +%H:%M:%S) ===" | tee -a "$SUMMARY"
