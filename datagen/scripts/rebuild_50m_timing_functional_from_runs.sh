#!/usr/bin/env bash
# Rebuild 50M training parquet from existing gem5 run directories, replacing
# Ruby oracle input features with timing-functional deploy-side features.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

SOURCE_RUN_BASE="${SOURCE_RUN_BASE:-${TAO_DATAGEN_ROOT:-${TAO_ROOT}/datagen}/tmp/exp_w11_w15_parallel_20260604_215144/runs}"
OUT_BASE="${1:-${OUT_BASE:-${TAO_DATAGEN_ROOT:-${TAO_ROOT}/datagen}/tmp/timing_functional_50m_$(date +%Y%m%d_%H%M%S)}}"
WORKLOADS="${WORKLOADS:-W11_stream_mix W12_stencil2d W13_graph_walk W14_branch_state W15_indirect}"
PARALLEL="${PARALLEL:-5}"
MIN_DEDUP_PER_WORKLOAD="${MIN_DEDUP_PER_WORKLOAD:-10000000}"
FINAL_TARGET="${FINAL_TARGET:-50000000}"
DEDUP_SAFETY="${DEDUP_SAFETY:-1.05}"
CTX_LEN="${CTX_LEN:-128}"
CTX_WARMUP_SKIP="${CTX_WARMUP_SKIP:-128}"
HEAD_SKIP="${HEAD_SKIP:-0.05}"
TAIL_SKIP="${TAIL_SKIP:-0.05}"
TAO_CPU_SIM_ROOT="${TAO_CPU_SIM_ROOT:-${TAO_ROOT}}"
PY="${PYTHON:-${PYTHON:-python3}}"

LOG_DIR="$OUT_BASE/logs"
SAMPLE_DIR="$OUT_BASE/sampled_timing_functional"
PACK_DIR="$OUT_BASE/packed_by_workload"
DEDUP_DIR="$OUT_BASE/dedup_by_workload"
FINAL_DIR="$OUT_BASE/final_balanced_${FINAL_TARGET}_pq"
STATE_DIR="$OUT_BASE/state"
mkdir -p "$LOG_DIR" "$SAMPLE_DIR" "$PACK_DIR" "$DEDUP_DIR" "$STATE_DIR"

log() {
  echo "[$(date '+%F %T')] $*" | tee -a "$LOG_DIR/progress.log"
}

keep_rate_for() {
  case "$1" in
    W11_stream_mix)   echo 0.999988 ;;
    W12_stencil2d)    echo 0.94 ;;
    W13_graph_walk)   echo 0.999998 ;;
    W14_branch_state) echo 1.0 ;;
    W15_indirect)     echo 0.999988 ;;
    *) echo 0.95 ;;
  esac
}

pack_target_for() {
  local keep
  keep="$(keep_rate_for "$1")"
  "$PY" - <<PY
import math
keep=float("$keep")
target=int("$MIN_DEDUP_PER_WORKLOAD")
safety=float("$DEDUP_SAFETY")
print(math.ceil(target * safety / keep))
PY
}

write_state() {
  local w="$1" event="$2" phase="$3" extra="${4:-}"
  printf '%s\t%s\t%s\t%s\n' "$(date +%s)" "$event" "$phase" "$extra" \
    >> "$STATE_DIR/$w.log"
}

run_one() {
  local wl="$1"
  local run_dir="$SOURCE_RUN_BASE/$wl"
  local sample_pq="$SAMPLE_DIR/$wl.parquet"
  local pack_dir="$PACK_DIR/$wl"
  local dedup_dir="$DEDUP_DIR/$wl"
  local target
  target="$(pack_target_for "$wl")"

  if [[ ! -d "$run_dir" ]]; then
    log "[FAIL] missing run_dir for $wl: $run_dir"
    return 2
  fi
  rm -rf "$sample_pq" "$pack_dir" "$dedup_dir"

  log "[$wl] sample target=$target feature_generator=timing-functional"
  write_state "$wl" start sample
  "$PY" "$REPO/tools/sample_steady_balanced.py" \
    --run "$wl=$run_dir" \
    --target "$target" \
    --out "$sample_pq" \
    --head-skip "$HEAD_SKIP" \
    --tail-skip "$TAIL_SKIP" \
    --context-warmup-skip "$CTX_WARMUP_SKIP" \
    --feature-generator timing-functional \
    --tao-cpu-sim-root "$TAO_CPU_SIM_ROOT" \
    > "$LOG_DIR/$wl.sample.log" 2>&1
  write_state "$wl" end sample

  log "[$wl] pack"
  write_state "$wl" start pack
  "$PY" "$REPO/tools/pack_to_parquet.py" \
    --from-parquet "$sample_pq" \
    --out-dir "$pack_dir" \
    --uarch-profile "$run_dir/uarch_profile.json" \
    > "$LOG_DIR/$wl.pack.log" 2>&1
  write_state "$wl" end pack

  log "[$wl] dedup"
  write_state "$wl" start dedup
  "$PY" "$REPO/tools/dedup_context.py" \
    --in-dir "$pack_dir" \
    --out-dir "$dedup_dir" \
    --context-len "$CTX_LEN" \
    --latency-bins 16 \
    > "$LOG_DIR/$wl.dedup.log" 2>&1
  write_state "$wl" end dedup

  "$PY" - <<PY > "$LOG_DIR/$wl.rows.txt"
import json
from pathlib import Path
m=json.loads((Path("$dedup_dir")/"meta.json").read_text())
print(m.get("n_total", m.get("total_rows", m.get("rows", 0))))
PY
  log "[$wl] done dedup_rows=$(cat "$LOG_DIR/$wl.rows.txt")"
}

log "SOURCE_RUN_BASE=$SOURCE_RUN_BASE"
log "OUT_BASE=$OUT_BASE"
log "WORKLOADS=$WORKLOADS"
log "PARALLEL=$PARALLEL"

declare -a PIDS=()
declare -a PID_WLS=()
rc=0
for wl in $WORKLOADS; do
  run_one "$wl" &
  PIDS+=("$!")
  PID_WLS+=("$wl")
  while (( ${#PIDS[@]} >= PARALLEL )); do
    new_pids=()
    new_wls=()
    for i in "${!PIDS[@]}"; do
      pid="${PIDS[$i]}"
      pwl="${PID_WLS[$i]}"
      if kill -0 "$pid" 2>/dev/null; then
        new_pids+=("$pid")
        new_wls+=("$pwl")
      else
        if wait "$pid"; then
          log "[$pwl] worker finished"
        else
          wrc=$?
          log "[FAIL] [$pwl] worker rc=$wrc"
          rc=1
        fi
      fi
    done
    PIDS=("${new_pids[@]}")
    PID_WLS=("${new_wls[@]}")
    (( ${#PIDS[@]} < PARALLEL )) || sleep 5
  done
done

for i in "${!PIDS[@]}"; do
  pid="${PIDS[$i]}"
  pwl="${PID_WLS[$i]}"
  if wait "$pid"; then
    log "[$pwl] worker finished"
  else
    wrc=$?
    log "[FAIL] [$pwl] worker rc=$wrc"
    rc=1
  fi
done

if (( rc != 0 )); then
  log "[FAIL] one or more workloads failed; skip final sample"
  exit "$rc"
fi

log "[final] balanced sample target=$FINAL_TARGET"
"$PY" "$REPO/tools/sample_balanced_dedup_parquet.py" \
  --in-root "$DEDUP_DIR" \
  --out-dir "$FINAL_DIR" \
  --target-total "$FINAL_TARGET" \
  --block-size 4096 \
  > "$LOG_DIR/final_balanced_sample.log" 2>&1

log "[DONE] final_dataset=$FINAL_DIR"
