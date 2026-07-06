#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
cd "$ROOT"

mkdir -p logs/watchdog tmp

RUN_NAME=${RUN_NAME:-v22_v16_bind_split_direct_no_tstart_8gpu_12000_watch}
OUT=${OUT:-ckpt/v22_v16_bind_split_direct_no_tstart_8gpu_12000}

# Scratch run by default. Re-running the same OUT resumes from the latest
# step_XXXXXX/head_best checkpoint through scripts/watch_train.sh.
nohup env \
  ROOT="$ROOT" \
  TMPDIR="${TMPDIR:-$ROOT/tmp}" \
  RUN_NAME="$RUN_NAME" \
  TRAIN_SCRIPT=scripts/run_v22_v16_local_binding_fuse_no_tstart_qwen3_0p6b.sh \
  OUT="$OUT" \
  TARGET_STEPS=${TARGET_STEPS:-12000} \
  GPUS=${GPUS:-0,1,2,3,4,5,6,7} \
  NPROC=${NPROC:-8} \
  BS=${BS:-1} \
  GRAD_ACCUM=${GRAD_ACCUM:-1} \
  MAX_LEN=${MAX_LEN:-32768} \
  LR_LORA=${LR_LORA:-2e-4} \
  LR_HEAD=${LR_HEAD:-1e-3} \
  LR_EMB=${LR_EMB:-1e-3} \
  EVAL_EVERY=${EVAL_EVERY:-500} \
  SAVE_EVERY=${SAVE_EVERY:-500} \
  EVAL_BATCHES=${EVAL_BATCHES:-0} \
  NUM_WORKERS=${NUM_WORKERS:-2} \
  LOCAL_FUSE_MODE=bind_concat \
  RANK_GAP=${RANK_GAP:-0.10} \
  RANK_TAU=${RANK_TAU:-0.10} \
  SPREAD_MIN_STD=${SPREAD_MIN_STD:-0.03} \
  INIT_CKPT= \
  SKIP_TRAIN_BATCHES=0 \
  STEP_OFFSET=0 \
  bash scripts/watch_train.sh \
  > "logs/watchdog/${RUN_NAME}.nohup.log" 2>&1 &

echo "started: $RUN_NAME"
echo "out: $OUT"
echo "train log: logs/${RUN_NAME}.current.log"
echo "watchdog log: logs/watchdog/${RUN_NAME}.nohup.log"
