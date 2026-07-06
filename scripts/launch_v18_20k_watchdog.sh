#!/usr/bin/env bash
set -euo pipefail

mkdir -p logs/watchdog

RUN_NAME=${RUN_NAME:-v18_fastslow_adapter_scratch_8gpu_20000_watch}
OUT=${OUT:-ckpt/v18_fastslow_adapter_scratch_8gpu_20000}

nohup env \
  RUN_NAME="$RUN_NAME" \
  TRAIN_SCRIPT=scripts/run_v18_fastslow_adapter_qwen3_0p6b.sh \
  OUT="$OUT" \
  TARGET_STEPS=${TARGET_STEPS:-20000} \
  GPUS=${GPUS:-0,1,2,3,4,5,6,7} \
  NPROC=${NPROC:-8} \
  BS=${BS:-1} \
  GRAD_ACCUM=${GRAD_ACCUM:-1} \
  MAX_LEN=${MAX_LEN:-32768} \
  LR_LORA=${LR_LORA:-1e-4} \
  LR_HEAD=${LR_HEAD:-3e-4} \
  LR_EMB=${LR_EMB:-3e-4} \
  EVAL_EVERY=${EVAL_EVERY:-500} \
  SAVE_EVERY=${SAVE_EVERY:-500} \
  EVAL_BATCHES=${EVAL_BATCHES:-0} \
  NUM_WORKERS=${NUM_WORKERS:-2} \
  bash scripts/watch_train.sh \
  > "logs/watchdog/${RUN_NAME}.nohup.log" 2>&1 &

echo "started: $RUN_NAME"
echo "train log: logs/${RUN_NAME}.current.log"
echo "watchdog log: logs/watchdog/${RUN_NAME}.nohup.log"
