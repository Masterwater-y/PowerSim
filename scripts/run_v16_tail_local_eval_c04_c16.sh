#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
cd "$ROOT"

CKPT=${CKPT:-ckpt/v16_v9core_tail_local_delta_rank_8gpu_8000/step_005000}
RAW_BASE=${RAW_BASE:-data/raw_trace_pool/activecore_eval}
TAG_PREFIX=${TAG_PREFIX:-v16_tail_local_step5000}
CORES=${CORES:-04 16}
SEED=${SEED:-B}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
MAX_LEN=${MAX_LEN:-32768}
MAX_WINDOWS=${MAX_WINDOWS:-0}
PROGRESS_EVERY=${PROGRESS_EVERY:-30}

mkdir -p logs

echo "[v16-eval] start $(date '+%F %T')"
echo "[v16-eval] CKPT=$CKPT"
echo "[v16-eval] CORES=$CORES default_seed=$SEED"
echo "[v16-eval] GPUS=$GPUS MAX_LEN=$MAX_LEN MAX_WINDOWS=$MAX_WINDOWS"
echo "[v16-eval] QUERY_PLACEMENT=tail_local"
echo

for C in $CORES; do
  seed="$SEED"
  if [[ "$C" == "06" && "$SEED" == "B" ]]; then
    seed="C"
  fi
  RAW="${RAW_BASE}/c${C}_seed${seed}_infer17"
  TAG="${TAG_PREFIX}_c${C}_seed${seed}_full"
  STAGE_T0=$(date +%s)

  if [[ ! -d "$RAW" ]]; then
    echo "[v16-eval][error] missing raw root: $RAW" >&2
    exit 2
  fi

  echo "============================================================"
  echo "[v16-eval] c${C}/seed${seed} start $(date '+%F %T')"
  echo "[v16-eval] RAW=$RAW"
  echo "[v16-eval] TAG=$TAG"
  echo "============================================================"

  CKPT="$CKPT" \
  RAW="$RAW" \
  TAG="$TAG" \
  GPUS="$GPUS" \
  MAX_LEN="$MAX_LEN" \
  MAX_WINDOWS="$MAX_WINDOWS" \
  PROGRESS_EVERY="$PROGRESS_EVERY" \
  QUERY_PLACEMENT=tail_local \
    bash scripts/eval_parallel.sh

  STAGE_T1=$(date +%s)
  echo
  echo "[v16-eval] c${C}/seed${seed} done $(date '+%F %T') elapsed=$((STAGE_T1 - STAGE_T0))s"
  echo
done

echo "[v16-eval] all done $(date '+%F %T')"
