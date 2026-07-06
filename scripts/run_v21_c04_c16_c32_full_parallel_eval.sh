#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
cd "$ROOT"

CKPT=${CKPT:-ckpt/v21_local_core_direct_cycles_aux_scratch_8gpu_12000}
CORES=${CORES:-"04 16 32"}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
MAX_WINDOWS=${MAX_WINDOWS:-0}
MAX_LEN=${MAX_LEN:-32768}
QUERY_PLACEMENT=${QUERY_PLACEMENT:-tail_local}
PROGRESS_EVERY=${PROGRESS_EVERY:-30}
PRINT_LOG=${PRINT_LOG:-1}

if [[ ! -f "$CKPT/head_best.pt" || ! -d "$CKPT/lora_best" ]]; then
  echo "[v21-c04-c16-c32][error] missing checkpoint files: $CKPT" >&2
  exit 1
fi

mkdir -p logs

RUN_TS=${RUN_TS:-$(date +%Y%m%d_%H%M%S)}
echo "[v21-c04-c16-c32] CKPT=$CKPT"
echo "[v21-c04-c16-c32] CORES=$CORES"
echo "[v21-c04-c16-c32] GPUS=$GPUS"
echo "[v21-c04-c16-c32] MAX_WINDOWS=$MAX_WINDOWS"
echo "[v21-c04-c16-c32] RUN_TS=$RUN_TS"

for C in $CORES; do
  RAW="data/raw_v7_seedB_c${C}_infer17"
  if [[ ! -d "$RAW" ]]; then
    echo "[v21-c04-c16-c32][error] raw root not found: $RAW" >&2
    exit 1
  fi

  TAG="v21_direct_cycles_aux_best_c${C}_full_${RUN_TS}"
  LOG="logs/eval_${TAG}.nohup.log"
  PID_FILE="logs/eval_${TAG}.pid"

  echo
  echo "===== start c${C} TAG=$TAG ====="
  echo "[v21-c04-c16-c32] log=$LOG"

  if [[ "$PRINT_LOG" == "1" ]]; then
    CKPT="$CKPT" \
    RAW="$RAW" \
    TAG="$TAG" \
    GPUS="$GPUS" \
    MAX_LEN="$MAX_LEN" \
    MAX_WINDOWS="$MAX_WINDOWS" \
    QUERY_PLACEMENT="$QUERY_PLACEMENT" \
    PROGRESS_EVERY="$PROGRESS_EVERY" \
    FOREGROUND=1 \
      bash scripts/run_cpi_only_c08_full_eval.sh 2>&1 | tee "$LOG"
  else
    CKPT="$CKPT" \
    RAW="$RAW" \
    TAG="$TAG" \
    GPUS="$GPUS" \
    MAX_LEN="$MAX_LEN" \
    MAX_WINDOWS="$MAX_WINDOWS" \
    QUERY_PLACEMENT="$QUERY_PLACEMENT" \
    PROGRESS_EVERY="$PROGRESS_EVERY" \
    FOREGROUND=1 \
      bash scripts/run_cpi_only_c08_full_eval.sh > "$LOG" 2>&1
  fi
  echo "===== done c${C} TAG=$TAG ====="
done

echo
echo "[v21-c04-c16-c32] all requested core counts launched/completed"
echo "[v21-c04-c16-c32] summaries:"
for C in $CORES; do
  echo "  rg \"FINAL SUMMARY|AGG|logs in\" logs/eval_v21_direct_cycles_aux_best_c${C}_full_${RUN_TS}.nohup.log"
done
