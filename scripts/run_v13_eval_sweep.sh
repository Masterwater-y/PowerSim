#!/usr/bin/env bash
set -euo pipefail

ROOT=/data00/yinhaolang/LLMSim
cd "$ROOT"

CKPT=${CKPT:-ckpt/v13_tail_side_qwen3_0p6b_c01_c04_c08_c16_8000}
BASE_MODEL=${BASE_MODEL:-Qwen/Qwen3-0.6B-Base}
MAX_LEN=${MAX_LEN:-32768}
TRAIN_MAX_LEN=${TRAIN_MAX_LEN:-32768}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
MAX_WINDOWS=${MAX_WINDOWS:-0}
PROGRESS_EVERY=${PROGRESS_EVERY:-30}
TAG_PREFIX=${TAG_PREFIX:-v13_qwen3_0p6b_step8000}
CORES=${CORES:-04 08 16 32}

mkdir -p logs

echo "[sweep] start $(date '+%F %T')"
echo "[sweep] CKPT=$CKPT"
echo "[sweep] BASE_MODEL=$BASE_MODEL"
echo "[sweep] MAX_LEN=$MAX_LEN TRAIN_MAX_LEN=$TRAIN_MAX_LEN"
echo "[sweep] GPUS=$GPUS MAX_WINDOWS=$MAX_WINDOWS"
echo "[sweep] CORES=$CORES"
echo

for C in $CORES; do
  RAW="data/raw_trace_pool/activecore_eval/c${C}_seedB_infer17"
  TAG="${TAG_PREFIX}_c${C}_seedB_full_ctx${MAX_LEN}"
  STAGE_T0=$(date +%s)

  if [[ ! -d "$RAW" ]]; then
    echo "[sweep][error] missing raw root: $RAW" >&2
    exit 2
  fi

  echo "============================================================"
  echo "[sweep] c${C} start $(date '+%F %T')"
  echo "[sweep] RAW=$RAW"
  echo "[sweep] TAG=$TAG"
  echo "============================================================"

  CKPT="$CKPT" \
  BASE_MODEL="$BASE_MODEL" \
  RAW="$RAW" \
  TAG="$TAG" \
  MAX_LEN="$MAX_LEN" \
  TRAIN_MAX_LEN="$TRAIN_MAX_LEN" \
  GPUS="$GPUS" \
  MAX_WINDOWS="$MAX_WINDOWS" \
  PROGRESS_EVERY="$PROGRESS_EVERY" \
    bash scripts/eval_parallel.sh

  STAGE_T1=$(date +%s)
  echo
  echo "[sweep] c${C} done $(date '+%F %T') elapsed=$((STAGE_T1 - STAGE_T0))s"
  echo
done

echo "[sweep] all done $(date '+%F %T')"
