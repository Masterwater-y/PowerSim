#!/usr/bin/env bash
# Build eval_quota_cycles cache by converting raw records/labels JSONL into
# per-core aligned parquet files. eval/eval_quota_cycles.py automatically
# prefers *.aligned.parquet when present via data.roi_stats.load_workload_rows().
set -uo pipefail

ROOT=/data00/yinhaolang/LLMSim
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
cd "$ROOT"

RAW=${RAW:-data/raw_v7_seedA_c08}
NUM_CORES=${NUM_CORES:-8}
PARALLEL=${PARALLEL:-4}
OVERWRITE=${OVERWRITE:-0}
ROW_GROUP_SIZE=${ROW_GROUP_SIZE:-65536}
PROGRESS_EVERY=${PROGRESS_EVERY:-30}

DEFAULT_WORKLOADS=(
  W_ads_ctr W_ads_ranking_proxy W_branch_storm W_chase_dram
  W_compute_int W_false_sharing W_feed_ranking W_fp_compute_dense
  W_fp_lite W_graph_recall_proxy W_indirect W_int_div
  W_interest_graph_recall W_mlp_light W_search_index_proxy W_stream
)
read -r -a WORKLOADS <<< "${WORKLOADS:-${DEFAULT_WORKLOADS[*]}}"

TS=$(date +%Y%m%d_%H%M%S)
LOGDIR=${LOGDIR:-logs/quota_eval_cache_${TS}}
mkdir -p "$LOGDIR"

echo "[cache] RAW=$RAW"
echo "[cache] NUM_CORES=$NUM_CORES PARALLEL=$PARALLEL OVERWRITE=$OVERWRITE ROW_GROUP_SIZE=$ROW_GROUP_SIZE"
echo "[cache] WORKLOADS=${WORKLOADS[*]}"
echo "[cache] LOGDIR=$LOGDIR"

declare -A PID_TO_WORKLOAD
FAILS=0

run_one() {
  local wl=$1
  local log="$LOGDIR/${wl}.log"
  local -a args=(
    scripts/convert_trace_to_aligned_parquet.py
    --raw-root "$RAW"
    --workloads "$wl"
    --row-group-size "$ROW_GROUP_SIZE"
  )
  if [[ "$OVERWRITE" == "1" ]]; then
    args+=(--overwrite)
  fi
  echo "[launch] $wl -> $log"
  "$PY" "${args[@]}" > "$log" 2>&1
}

progress_loop() {
  while true; do
    sleep "$PROGRESS_EVERY"
    echo
    echo "============ cache progress @ $(date +%H:%M:%S) ============"
    local done=0
    local total=${#WORKLOADS[@]}
    for wl in "${WORKLOADS[@]}"; do
      local n
      n=$(find "$RAW/$wl/tao_trace" -maxdepth 1 -type f \
        -name "*.aligned.parquet" 2>/dev/null | wc -l | tr -d ' ')
      if (( n >= NUM_CORES )); then
        done=$((done + 1))
      fi
      printf "%-28s aligned=%s/%s\n" "$wl" "$n" "$NUM_CORES"
    done
    echo "[cache] done_workloads=$done/$total"
  done
}

progress_loop &
PROG_PID=$!
trap 'kill "$PROG_PID" 2>/dev/null || true' EXIT

for wl in "${WORKLOADS[@]}"; do
  while (( $(jobs -pr | wc -l) - 1 >= PARALLEL )); do
    if ! wait -n; then
      FAILS=$((FAILS + 1))
    fi
  done
  run_one "$wl" &
done

while (( $(jobs -pr | wc -l) > 1 )); do
  if ! wait -n; then
    FAILS=$((FAILS + 1))
  fi
done

kill "$PROG_PID" 2>/dev/null || true
trap - EXIT

echo
echo "============ cache verify @ $(date +%H:%M:%S) ============"
VERIFY_FAILS=0
for wl in "${WORKLOADS[@]}"; do
  n=$(find "$RAW/$wl/tao_trace" -maxdepth 1 -type f \
    -name "*.aligned.parquet" 2>/dev/null | wc -l | tr -d ' ')
  if (( n < NUM_CORES )); then
    echo "[verify][FAIL] $wl aligned=$n/$NUM_CORES"
    VERIFY_FAILS=$((VERIFY_FAILS + 1))
  else
    echo "[verify][OK]   $wl aligned=$n/$NUM_CORES"
  fi
done

echo "[cache] process_fails=$FAILS verify_fails=$VERIFY_FAILS logs=$LOGDIR"
exit $(( FAILS + VERIFY_FAILS ))
