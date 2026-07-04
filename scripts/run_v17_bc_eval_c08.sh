#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
cd "$ROOT"

MODE=${1:-${MODE:-full}}
CKPT_ROOT=${CKPT_ROOT:-ckpt/v17_bc_split_heads_nophase_8gpu_8000_resume500}
RAW=${RAW:-data/raw_trace_pool/activecore_eval/c08_seedB_infer17}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
MAX_LEN=${MAX_LEN:-32768}
PROGRESS_EVERY=${PROGRESS_EVERY:-30}
QUERY_PLACEMENT=${QUERY_PLACEMENT:-tail_local}
FOREGROUND=${FOREGROUND:-0}

if [[ -z "${CKPT:-}" ]]; then
  CKPT=$(find "$CKPT_ROOT" -maxdepth 1 -type d -name 'step_*' | sort -V | tail -n 1)
fi

case "$MODE" in
  full)
    MAX_WINDOWS=${MAX_WINDOWS:-0}
    ;;
  smoke)
    MAX_WINDOWS=${MAX_WINDOWS:-10}
    ;;
  *)
    echo "usage: $0 [full|smoke]" >&2
    exit 2
    ;;
esac

if [[ -z "$CKPT" || ! -d "$CKPT" ]]; then
  echo "[error] checkpoint not found: ${CKPT:-<empty>}" >&2
  exit 1
fi
if [[ ! -f "$CKPT/head_best.pt" || ! -d "$CKPT/lora_best" ]]; then
  echo "[error] checkpoint is missing head_best.pt or lora_best: $CKPT" >&2
  exit 1
fi
if [[ ! -d "$RAW" ]]; then
  echo "[error] raw eval root not found: $RAW" >&2
  exit 1
fi

STEP_NAME=$(basename "$CKPT")
if [[ "$MAX_WINDOWS" == "0" ]]; then
  RUN_KIND=full
else
  RUN_KIND="smoke${MAX_WINDOWS}"
fi
TAG=${TAG:-v17_bc_split_${STEP_NAME}_c08_seedB_${RUN_KIND}_ctx${MAX_LEN}}
LOG=${LOG:-logs/eval_${TAG}.nohup.log}
PID_FILE=${PID_FILE:-logs/eval_${TAG}.pid}

mkdir -p logs

echo "[v17-c08-eval] CKPT=$CKPT"
echo "[v17-c08-eval] RAW=$RAW"
echo "[v17-c08-eval] TAG=$TAG"
echo "[v17-c08-eval] GPUS=$GPUS MAX_LEN=$MAX_LEN MAX_WINDOWS=$MAX_WINDOWS"
echo "[v17-c08-eval] QUERY_PLACEMENT=$QUERY_PLACEMENT"

if [[ "$FOREGROUND" == "1" ]]; then
  CKPT="$CKPT" \
  RAW="$RAW" \
  TAG="$TAG" \
  GPUS="$GPUS" \
  MAX_LEN="$MAX_LEN" \
  MAX_WINDOWS="$MAX_WINDOWS" \
  PROGRESS_EVERY="$PROGRESS_EVERY" \
  QUERY_PLACEMENT="$QUERY_PLACEMENT" \
    bash scripts/eval_parallel.sh
else
  CKPT="$CKPT" \
  RAW="$RAW" \
  TAG="$TAG" \
  GPUS="$GPUS" \
  MAX_LEN="$MAX_LEN" \
  MAX_WINDOWS="$MAX_WINDOWS" \
  PROGRESS_EVERY="$PROGRESS_EVERY" \
  QUERY_PLACEMENT="$QUERY_PLACEMENT" \
    nohup bash scripts/eval_parallel.sh > "$LOG" 2>&1 &
  PID=$!
  echo "$PID" > "$PID_FILE"
  echo "[v17-c08-eval] started pid=$PID"
  echo "[v17-c08-eval] log=$LOG"
  echo "[v17-c08-eval] pid_file=$PID_FILE"
  echo "tail -f $LOG"
fi
