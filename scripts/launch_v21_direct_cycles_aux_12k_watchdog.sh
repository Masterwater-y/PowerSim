#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
cd "$ROOT"

mkdir -p logs/watchdog

RUN_NAME=${RUN_NAME:-v21_direct_cycles_aux_scratch_8gpu_12000_watch}
OUT=${OUT:-ckpt/v21_local_core_direct_cycles_aux_scratch_8gpu_12000}

# Scratch run: leave INIT_CKPT unset. Re-running the same OUT resumes from the
# latest step_XXXXXX/head_best checkpoint through scripts/watch_train.sh.
nohup env \
  RUN_NAME="$RUN_NAME" \
  TRAIN_SCRIPT=scripts/run_v21_local_core_direct_cycles_aux_qwen3_0p6b.sh \
  OUT="$OUT" \
  TARGET_STEPS=${TARGET_STEPS:-12000} \
  GPUS=${GPUS:-0,1,2,3,4,5,6,7} \
  NPROC=${NPROC:-8} \
  BS=${BS:-1} \
  GRAD_ACCUM=${GRAD_ACCUM:-1} \
  MAX_LEN=${MAX_LEN:-32768} \
  LR_LORA=${LR_LORA:-3e-5} \
  LR_HEAD=${LR_HEAD:-1e-4} \
  LR_EMB=${LR_EMB:-1e-4} \
  EVAL_EVERY=${EVAL_EVERY:-500} \
  SAVE_EVERY=${SAVE_EVERY:-500} \
  EVAL_BATCHES=${EVAL_BATCHES:-0} \
  NUM_WORKERS=${NUM_WORKERS:-2} \
  CPI_HEAD_MODE=direct \
  LOSS_WEIGHT_MODE=fixed \
  INIT_CKPT= \
  SKIP_TRAIN_BATCHES=0 \
  STEP_OFFSET=0 \
  RESET_HEAD=0 \
  RESET_LOSS_STATE=1 \
  LAMBDA_CPI_ABS=1.0 \
  LAMBDA_CYCLES_WINDOW=1.0 \
  LAMBDA_AUX_PMU=0.05 \
  LAMBDA_DELTA=0.0 \
  LAMBDA_RANK=0.0 \
  LAMBDA_SPREAD=0.0 \
  LAMBDA_SLOWEST=0.0 \
  LAMBDA_FASTEST=0.0 \
  LAMBDA_INV=0.0 \
  LAMBDA_PHYS=0.0 \
  bash scripts/watch_train.sh \
  > "logs/watchdog/${RUN_NAME}.nohup.log" 2>&1 &

echo "started: $RUN_NAME"
echo "out: $OUT"
echo "train log: logs/${RUN_NAME}.current.log"
echo "watchdog log: logs/watchdog/${RUN_NAME}.nohup.log"
