#!/usr/bin/env bash
# step5_run_4workloads.sh — V9.5 A2/A3 后回归：跑 4 个 workload，
# 对每个产物做 oracle↔ref_sim 17/17 bit-exact + PMU 表。
set -euo pipefail

ROOT="${TAO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
TAOGEN="${TAO_DATAGEN_ROOT:-$ROOT/datagen}"
GEM5="${TAO_GEM5_ROOT:-$ROOT/gem5}/build/X86_MESI_Three_Level/gem5.opt"
CFG=$TAOGEN/configs/run_mt_mvp.py
REFSIM=$TAOGEN/mesi_ref_sim/build/mesi_ref_sim
COMPARE=$TAOGEN/mesi_ref_sim/scripts/compare_oracle.py
COMPARE_I=$TAOGEN/mesi_ref_sim/scripts/compare_ifetch.py
PMU=$TAOGEN/mesi_ref_sim/scripts/pmu_report.py
WL=$TAOGEN/workloads

export PATH=/opt/gcc-11/bin:$PATH
export LD_LIBRARY_PATH=/root/.pyenv/versions/3.8.0/lib:/opt/gcc-11/lib64:${LD_LIBRARY_PATH:-}

OUT_BASE=${1:-$ROOT/tmp/step5_$(date +%Y%m%d_%H%M%S)}
mkdir -p "$OUT_BASE"
SUMMARY="$OUT_BASE/SUMMARY.txt"
: > "$SUMMARY"

run_one() {
    local NAME=$1 BIN=$2; shift 2
    local ARGS=("$@")
    local OUT="$OUT_BASE/$NAME"
    rm -rf "$OUT"
    mkdir -p "$OUT"
    echo "=== $NAME ==="
    "$GEM5" --outdir="$OUT" "$CFG" \
        --cmd "$BIN" --workload-args "${ARGS[@]}" --num-cores 4 \
        > "$OUT/gem5.log" 2>&1
    # 合并 4 个 core 的 mem_events.jsonl（按 commit_tick 排序）；
    # ifetch 行天然只在 core0 (global_mem_events_ 持有者)。
    cat "$OUT/tao_trace/"*.mem_events.jsonl \
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
    # ref_sim 重放（位置参数：profile, in.jsonl, out.jsonl）
    local PROFILE="$OUT/uarch_profile.json"
    "$REFSIM" "$PROFILE" "$OUT/mem_events.merged.jsonl" "$OUT/pred.jsonl" \
        2> "$OUT/refsim.log" || {
        echo "[$NAME] ref_sim FAILED" | tee -a "$SUMMARY"; return 1; }
    # bit-exact d-side
    python3 "$COMPARE" "$OUT/mem_events.merged.jsonl" "$OUT/pred.jsonl" \
        > "$OUT/compare.log" 2>&1 || true
    local M=$(grep -E "^matched\s" "$OUT/compare.log" | head -1)
    local X=$(grep -E "^mismatched\s" "$OUT/compare.log" | head -1)
    # bit-exact i-side
    python3 "$COMPARE_I" "$OUT/mem_events.merged.jsonl" "$OUT/pred.jsonl" \
        > "$OUT/compare.ifetch.log" 2>&1 || true
    local IM=$(grep -E "^matched\s" "$OUT/compare.ifetch.log" | head -1)
    local IX=$(grep -E "^mismatched\s" "$OUT/compare.ifetch.log" | head -1)
    # PMU
    python3 "$PMU" "$OUT/mem_events.merged.jsonl" "$OUT/pred.jsonl" \
        --uarch-profile "$PROFILE" \
        > "$OUT/pmu.log" 2>&1 || true
    echo "[$NAME] dside: $M | $X ; iside: $IM | $IX" | tee -a "$SUMMARY"
}

run_one W1_compute_int "$WL/mt_compute_int/mt_compute_int" 4 800
run_one W2_chase_dram  "$WL/mt_chase_dram/mt_chase_dram"   4 1500
run_one W3_micro_coh   "$WL/mt_micro_coh/mt_micro_coh"     4 5000
run_one W4_coh_stress  "$WL/mt_coh_stress/mt_coh_stress"   4 1000

echo
echo "=== SUMMARY ==="
cat "$SUMMARY"
echo "out: $OUT_BASE"
