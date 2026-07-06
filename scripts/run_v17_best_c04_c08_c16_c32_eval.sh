#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
cd "$ROOT"

export TMPDIR="${TMPDIR:-$ROOT/tmp}"
mkdir -p "$TMPDIR" logs

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "[v17-eval][error] tracked working tree is not clean; commit/stash first" >&2
  exit 1
fi

# v17 checkpoint needs the saved v17-v21 code state. Stay there after eval.
git checkout c27a932

CKPT=${CKPT:-ckpt/v17_bc_split_heads_nophase_8gpu_8000_resume500/step_008000}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
MAX_LEN=${MAX_LEN:-32768}
MAX_WINDOWS=${MAX_WINDOWS:-0}
PROGRESS_EVERY=${PROGRESS_EVERY:-30}
QUERY_PLACEMENT=${QUERY_PLACEMENT:-tail_local}
CORES=${CORES:-04 08 16 32}
TS=${TS:-$(date +%Y%m%d_%H%M%S)}
DRIVER_LOG=${DRIVER_LOG:-logs/eval_v17_best_c04_c08_c16_c32_${TS}.driver.log}

if [[ ! -f "$CKPT/head_best.pt" || ! -d "$CKPT/lora_best" ]]; then
  echo "[v17-eval][error] checkpoint missing head_best.pt or lora_best: $CKPT" >&2
  exit 2
fi

{
  echo "[v17-eval] start $(date '+%F %T')"
  echo "[v17-eval] CKPT=$CKPT"
  echo "[v17-eval] CORES=$CORES"
  echo "[v17-eval] GPUS=$GPUS MAX_LEN=$MAX_LEN MAX_WINDOWS=$MAX_WINDOWS"
  echo "[v17-eval] QUERY_PLACEMENT=$QUERY_PLACEMENT"
  echo "[v17-eval] workload set: v17 common16 from scripts/eval_parallel.sh"
  echo

  for C in $CORES; do
    RAW="data/raw_trace_pool/activecore_eval/c${C}_seedB_infer17"
    TAG="v17_best_step008000_c${C}_seedB_common16_ctx${MAX_LEN}_${TS}"

    if [[ ! -d "$RAW" ]]; then
      echo "[v17-eval][error] missing raw root: $RAW" >&2
      exit 3
    fi

    echo "============================================================"
    echo "[v17-eval] c${C} start $(date '+%F %T')"
    echo "[v17-eval] RAW=$RAW"
    echo "[v17-eval] TAG=$TAG"
    echo "============================================================"

    CKPT="$CKPT" \
    RAW="$RAW" \
    TAG="$TAG" \
    GPUS="$GPUS" \
    MAX_LEN="$MAX_LEN" \
    MAX_WINDOWS="$MAX_WINDOWS" \
    PROGRESS_EVERY="$PROGRESS_EVERY" \
    QUERY_PLACEMENT="$QUERY_PLACEMENT" \
      bash scripts/eval_parallel.sh

    echo "[v17-eval] c${C} done $(date '+%F %T')"
    echo
  done

  echo "[v17-eval] all done $(date '+%F %T')"
} 2>&1 | tee "$DRIVER_LOG"

echo "[v17-eval] driver_log=$DRIVER_LOG"
