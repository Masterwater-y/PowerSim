#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
cd "$ROOT"

mkdir -p logs/watchdog

CPI_HEAD_MODE=${CPI_HEAD_MODE:-direct}
RUN_NAME=${RUN_NAME:-cpi_only_${CPI_HEAD_MODE}_scratch_8gpu_12000_watch}
OUT=${OUT:-ckpt/local_core_cpi_only_${CPI_HEAD_MODE}_scratch_8gpu_12000}

# Scratch run: leave INIT_CKPT unset.  RESET_HEAD must be 0 so watchdog resumes
# keep the trained head from OUT/step_XXXXXX instead of reinitializing it.
nohup env \
  RUN_NAME="$RUN_NAME" \
  TRAIN_SCRIPT=scripts/run_local_core_cpi_only_qwen3_0p6b.sh \
  OUT="$OUT" \
  TARGET_STEPS=${TARGET_STEPS:-12000} \
  GPUS=${GPUS:-0,1,2,3,4,5,6,7} \
  NPROC=${NPROC:-8} \
  BS=${BS:-1} \
  GRAD_ACCUM=${GRAD_ACCUM:-1} \
  MAX_LEN=${MAX_LEN:-32768} \
  LR_LORA=${LR_LORA:-1e-4} \
  LR_HEAD=${LR_HEAD:-5e-4} \
  LR_EMB=${LR_EMB:-3e-4} \
  EVAL_EVERY=${EVAL_EVERY:-500} \
  SAVE_EVERY=${SAVE_EVERY:-500} \
  EVAL_BATCHES=${EVAL_BATCHES:-0} \
  NUM_WORKERS=${NUM_WORKERS:-2} \
  CPI_HEAD_MODE="$CPI_HEAD_MODE" \
  INIT_CKPT= \
  SKIP_TRAIN_BATCHES=0 \
  STEP_OFFSET=0 \
  RESET_HEAD=0 \
  RESET_LOSS_STATE=1 \
  LAMBDA_CPI_ABS=1.0 \
  LAMBDA_AUX_PMU=0.0 \
  LAMBDA_DELTA=0.0 \
  LAMBDA_CYCLES_WINDOW=0.0 \
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
