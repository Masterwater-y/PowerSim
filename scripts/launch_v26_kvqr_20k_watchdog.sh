#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TSim}
cd "$ROOT"

mkdir -p logs/watchdog

RUN_NAME=${RUN_NAME:-v26_kvqr_clean14_8gpu_20000_watch}
OUT=${OUT:-ckpt/v26_kvqr_clean14_8gpu_20000}
DATA=${DATA:-data/windows_v26_clean14_tail_local_all/windows.jsonl}
CACHE_PATH=${CACHE_PATH:-data/windows_v26_clean14_tail_local_all/windows.maxlen32768.tensor_cache}

nohup env \
  ROOT="$ROOT" \
  RUN_NAME="$RUN_NAME" \
  TRAIN_SCRIPT=scripts/run_v26_kvqr_ddp8.sh \
  DATA="$DATA" \
  CACHE_PATH="$CACHE_PATH" \
  OUT="$OUT" \
  TARGET_STEPS=${TARGET_STEPS:-20000} \
  GPUS=${GPUS:-0,1,2,3,4,5,6,7} \
  NPROC=${NPROC:-8} \
  BS=${BS:-1} \
  MAX_LEN=${MAX_LEN:-32768} \
  MAX_UOPS_PER_CORE=${MAX_UOPS_PER_CORE:-32768} \
  TRAIN_MAX_UOPS_PER_CORE=${TRAIN_MAX_UOPS_PER_CORE:-0} \
  TRAIN_MAX_TOTAL_UOPS=${TRAIN_MAX_TOTAL_UOPS:-32768} \
  LR=${LR:-3e-4} \
  WEIGHT_DECAY=${WEIGHT_DECAY:-0.05} \
  D_MODEL=${D_MODEL:-320} \
  N_HEADS=${N_HEADS:-8} \
  N_LAYERS=${N_LAYERS:-8} \
  FFN_DIM=${FFN_DIM:-1280} \
  FIELD_DIM=${FIELD_DIM:-96} \
  HEAD_HIDDEN=${HEAD_HIDDEN:-256} \
  DROPOUT=${DROPOUT:-0.1} \
  AMP_DTYPE=${AMP_DTYPE:-bf16} \
  REQUIRE_FLASH_ATTN=${REQUIRE_FLASH_ATTN:-0} \
  SDPA_BACKEND=${SDPA_BACKEND:-no_flash} \
  LENGTH_BUCKET_SIZE=${LENGTH_BUCKET_SIZE:-2048} \
  EVAL_EVERY=${EVAL_EVERY:-1000} \
  EVAL_BATCHES=${EVAL_BATCHES:-0} \
  SAVE_EVERY=${SAVE_EVERY:-500} \
  NUM_WORKERS=${NUM_WORKERS:-2} \
  PREFETCH_FACTOR=${PREFETCH_FACTOR:-2} \
  bash scripts/watch_train.sh \
  > "logs/watchdog/${RUN_NAME}.nohup.log" 2>&1 &

echo "started: $RUN_NAME"
echo "train log: logs/${RUN_NAME}.current.log"
echo "watchdog log: logs/watchdog/${RUN_NAME}.nohup.log"
