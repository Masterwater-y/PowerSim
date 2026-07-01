#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
cd "$ROOT"

CKPT=${CKPT:-ckpt/v9_tq_train600_8gpu_4000_resume1840_fastskip}
RAW_BASE=${RAW_BASE:-data/raw_trace_pool/activecore_eval}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
MAX_LEN=${MAX_LEN:-32768}
MAX_WINDOWS=${MAX_WINDOWS:-0}
PROGRESS_EVERY=${PROGRESS_EVERY:-30}
TAG_PREFIX=${TAG_PREFIX:-v9_final}
CORES=${CORES:-04 08}
SEED=${SEED:-B}

mkdir -p logs

echo "[v9-eval] start $(date '+%F %T')"
echo "[v9-eval] CKPT=$CKPT"
echo "[v9-eval] CORES=$CORES default_seed=$SEED"
echo "[v9-eval] MAX_LEN=$MAX_LEN MAX_WINDOWS=$MAX_WINDOWS GPUS=$GPUS"
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
    echo "[v9-eval][error] missing raw root: $RAW" >&2
    exit 2
  fi

  echo "============================================================"
  echo "[v9-eval] c${C}/seed${seed} start $(date '+%F %T')"
  echo "[v9-eval] RAW=$RAW"
  echo "[v9-eval] TAG=$TAG"
  echo "============================================================"

  CKPT="$CKPT" \
  RAW="$RAW" \
  TAG="$TAG" \
  GPUS="$GPUS" \
  MAX_LEN="$MAX_LEN" \
  MAX_WINDOWS="$MAX_WINDOWS" \
  PROGRESS_EVERY="$PROGRESS_EVERY" \
    bash scripts/eval_parallel.sh

  STAGE_T1=$(date +%s)
  echo
  echo "[v9-eval] c${C}/seed${seed} done $(date '+%F %T') elapsed=$((STAGE_T1 - STAGE_T0))s"
  echo
done

echo "[v9-eval] all done $(date '+%F %T')"
