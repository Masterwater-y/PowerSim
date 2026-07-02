#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
cd "$ROOT"

CKPT=${CKPT:-ckpt/v15_v9core_delta_rank_queryseg_8gpu_8000}
RAW_BASE=${RAW_BASE:-data/raw_trace_pool/activecore_eval}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
MAX_LEN=${MAX_LEN:-32768}
MAX_WINDOWS=${MAX_WINDOWS:-0}
PROGRESS_EVERY=${PROGRESS_EVERY:-30}
TAG_PREFIX=${TAG_PREFIX:-v15_v9core_delta_rank_queryseg}
CORES=${CORES:-04 08 16}
SEED=${SEED:-B}

mkdir -p logs

echo "[v15-eval] start $(date '+%F %T')"
echo "[v15-eval] CKPT=$CKPT"
echo "[v15-eval] CORES=$CORES default_seed=$SEED"
echo "[v15-eval] MAX_LEN=$MAX_LEN MAX_WINDOWS=$MAX_WINDOWS GPUS=$GPUS"
echo "[v15-eval] QUERY_PLACEMENT=segment"
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
    echo "[v15-eval][error] missing raw root: $RAW" >&2
    exit 2
  fi

  echo "============================================================"
  echo "[v15-eval] c${C}/seed${seed} start $(date '+%F %T')"
  echo "[v15-eval] RAW=$RAW"
  echo "[v15-eval] TAG=$TAG"
  echo "============================================================"

  CKPT="$CKPT" \
  RAW="$RAW" \
  TAG="$TAG" \
  GPUS="$GPUS" \
  MAX_LEN="$MAX_LEN" \
  MAX_WINDOWS="$MAX_WINDOWS" \
  PROGRESS_EVERY="$PROGRESS_EVERY" \
  QUERY_PLACEMENT=segment \
    bash scripts/eval_parallel.sh

  STAGE_T1=$(date +%s)
  echo
  echo "[v15-eval] c${C}/seed${seed} done $(date '+%F %T') elapsed=$((STAGE_T1 - STAGE_T0))s"
  echo
done

echo "[v15-eval] all done $(date '+%F %T')"
