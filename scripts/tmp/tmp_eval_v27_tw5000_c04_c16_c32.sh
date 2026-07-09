#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TSim}
cd "$ROOT"

CKPT=${CKPT:-ckpt/v27_ss_tw5000_8l_t32768_bs1_20k_20260709_015028}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
COMMON_ENV=(
  "CKPT=$CKPT"
  "GPUS=$GPUS"
  "DEVICE=${DEVICE:-cuda}"
  "MAX_WINDOWS=${MAX_WINDOWS:-0}"
  "QUERY_PLACEMENT=${QUERY_PLACEMENT:-tail_local}"
  "PLANNER_STATE_SOURCE=${PLANNER_STATE_SOURCE:-pred}"
  "PROGRESS_EVERY=${PROGRESS_EVERY:-30}"
)

mkdir -p logs

for C in 04 16 32; do
  RAW="data/raw_trace_pool/activecore_eval/c${C}_seedB_infer17"
  TAG="v27_ss_tw5000_20k_best_c${C}_seedB_full"
  LOG="logs/eval_${TAG}.nohup.log"
  echo "[run] c${C} raw=$RAW log=$LOG"
  env "${COMMON_ENV[@]}" RAW="$RAW" TAG="$TAG" \
    bash scripts/eval_parallel.sh > "$LOG" 2>&1
  echo "[done] c${C}"
done

echo "[logs]"
echo "  logs/eval_v27_ss_tw5000_20k_best_c04_seedB_full.nohup.log"
echo "  logs/eval_v27_ss_tw5000_20k_best_c16_seedB_full.nohup.log"
echo "  logs/eval_v27_ss_tw5000_20k_best_c32_seedB_full.nohup.log"
