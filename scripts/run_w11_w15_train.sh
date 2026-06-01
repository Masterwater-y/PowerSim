#!/usr/bin/env bash
# 采集 W11..W15 realistic pthread workloads，用于 1M/3M/10M 均衡数据集构造。
#
# 用法：
#   PROFILE=1m  bash scripts/run_w11_w15_train.sh [OUT_BASE]
#   PROFILE=3m  bash scripts/run_w11_w15_train.sh [OUT_BASE]
#   PROFILE=10m bash scripts/run_w11_w15_train.sh [OUT_BASE]
#
# 输出目录可直接作为 scripts/build_balanced_dataset.sh 的 RUN_BASE。
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$(cd "$REPO/.." && pwd)"

GEM5="${GEM5:-$ROOT/gem5/build/X86_MESI_Three_Level/gem5.opt}"
CFG="$REPO/configs/run_mt_mvp.py"
REFSIM="$REPO/mesi_ref_sim/build/mesi_ref_sim"
COMPARE="$REPO/mesi_ref_sim/scripts/compare_oracle.py"
COMPARE_I="$REPO/mesi_ref_sim/scripts/compare_ifetch.py"
PMU="$REPO/mesi_ref_sim/scripts/pmu_report.py"
WL="$REPO/workloads"

PROFILE="${PROFILE:-1m}"
NUM_CORES="${NUM_CORES:-4}"
OUT_BASE="${1:-$REPO/tmp/run_w11_w15_${PROFILE}_$(date +%Y%m%d_%H%M%S)}"

export LD_LIBRARY_PATH="/opt/gcc-11/lib64:/root/.pyenv/versions/3.8.0/lib:${LD_LIBRARY_PATH:-}"

mkdir -p "$OUT_BASE"
SUMMARY="$OUT_BASE/SUMMARY.txt"
: > "$SUMMARY"
echo "[run_w11_w15] PROFILE=$PROFILE NUM_CORES=$NUM_CORES OUT=$OUT_BASE" | tee -a "$SUMMARY"

run_one() {
  local NAME=$1 BIN=$2; shift 2
  local ARGS=("$@")
  local OUT="$OUT_BASE/$NAME"
  rm -rf "$OUT"; mkdir -p "$OUT"
  echo "=== $NAME start $(date +%H:%M:%S) args=${ARGS[*]} ===" | tee -a "$SUMMARY"
  "$GEM5" --outdir="$OUT" "$CFG" \
      --cmd "$BIN" --workload-args "${ARGS[@]}" --num-cores "$NUM_CORES" \
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
  local PROFILE_JSON="$OUT/uarch_profile.json"
  "$REFSIM" "$PROFILE_JSON" "$OUT/mem_events.merged.jsonl" "$OUT/pred.jsonl" \
      2> "$OUT/refsim.log" || { echo "[$NAME] refsim FAIL" | tee -a "$SUMMARY"; return 0; }
  python3 "$COMPARE"   "$OUT/mem_events.merged.jsonl" "$OUT/pred.jsonl" > "$OUT/compare.log"        2>&1 || true
  python3 "$COMPARE_I" "$OUT/mem_events.merged.jsonl" "$OUT/pred.jsonl" > "$OUT/compare.ifetch.log" 2>&1 || true
  python3 "$PMU"       "$OUT/mem_events.merged.jsonl" "$OUT/pred.jsonl" --uarch-profile "$PROFILE_JSON" \
                                                                       > "$OUT/pmu.log"            2>&1 || true
  local R=$(wc -l < "$OUT/tao_trace/"*.records.micro.jsonl 2>/dev/null | awk '{s+=$1} END{print s+0}')
  local M=$(grep -E "^matched\s" "$OUT/compare.log" | head -1 || true)
  local IM=$(grep -E "^matched\s" "$OUT/compare.ifetch.log" | head -1 || true)
  echo "[$NAME done $(date +%H:%M:%S)] records=$R ; dside: $M ; iside: $IM" | tee -a "$SUMMARY"
}

case "$PROFILE" in
  1m)
    run_one W11_stream_mix   "$WL/mt_stream_mix/mt_stream_mix"                  4 5 256 0 11
    run_one W12_stencil2d    "$WL/mt_stencil2d/mt_stencil2d"                    4 1 256 0 12
    run_one W13_graph_walk   "$WL/mt_graph_walk/mt_graph_walk"                  4 2 1024 0 13
    run_one W14_branch_state "$WL/mt_branch_state_machine/mt_branch_state_machine" 4 2 64 0 14
    run_one W15_indirect     "$WL/mt_indirect_dispatch/mt_indirect_dispatch"    4 2 256 0 15
    ;;
  3m)
    run_one W11_stream_mix   "$WL/mt_stream_mix/mt_stream_mix"                  4 14 1024 1 11
    run_one W12_stencil2d    "$WL/mt_stencil2d/mt_stencil2d"                    4 3 1024 1 12
    run_one W13_graph_walk   "$WL/mt_graph_walk/mt_graph_walk"                  4 5 4096 1 13
    run_one W14_branch_state "$WL/mt_branch_state_machine/mt_branch_state_machine" 4 4 256 1 14
    run_one W15_indirect     "$WL/mt_indirect_dispatch/mt_indirect_dispatch"    4 3 1024 1 15
    ;;
  10m)
    run_one W11_stream_mix   "$WL/mt_stream_mix/mt_stream_mix"                  4 46 4096 2 11
    run_one W12_stencil2d    "$WL/mt_stencil2d/mt_stencil2d"                    4 9 4096 2 12
    run_one W13_graph_walk   "$WL/mt_graph_walk/mt_graph_walk"                  4 15 16384 2 13
    run_one W14_branch_state "$WL/mt_branch_state_machine/mt_branch_state_machine" 4 11 1024 2 14
    run_one W15_indirect     "$WL/mt_indirect_dispatch/mt_indirect_dispatch"    4 10 4096 2 15
    ;;
  *)
    echo "[err] PROFILE must be one of: 1m, 3m, 10m" >&2
    exit 2
    ;;
esac

echo "=== ALL DONE $(date +%H:%M:%S) ===" | tee -a "$SUMMARY"
echo "out: $OUT_BASE" | tee -a "$SUMMARY"
