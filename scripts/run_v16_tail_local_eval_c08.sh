#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
cd "$ROOT"

CKPT=${CKPT:-ckpt/v16_v9core_tail_local_delta_rank_8gpu_8000/step_005000}
RAW=${RAW:-data/raw_trace_pool/activecore_eval/c08_seedB_infer17}
TAG=${TAG:-v16_tail_local_step5000_c08_seedB_full}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
MAX_LEN=${MAX_LEN:-32768}
MAX_WINDOWS=${MAX_WINDOWS:-0}
PROGRESS_EVERY=${PROGRESS_EVERY:-30}

mkdir -p logs

echo "[v16-eval-c08] CKPT=$CKPT"
echo "[v16-eval-c08] RAW=$RAW"
echo "[v16-eval-c08] TAG=$TAG"
echo "[v16-eval-c08] GPUS=$GPUS MAX_LEN=$MAX_LEN MAX_WINDOWS=$MAX_WINDOWS"
echo "[v16-eval-c08] QUERY_PLACEMENT=tail_local"

CKPT="$CKPT" \
RAW="$RAW" \
TAG="$TAG" \
GPUS="$GPUS" \
MAX_LEN="$MAX_LEN" \
MAX_WINDOWS="$MAX_WINDOWS" \
PROGRESS_EVERY="$PROGRESS_EVERY" \
QUERY_PLACEMENT=tail_local \
  bash scripts/eval_parallel.sh
