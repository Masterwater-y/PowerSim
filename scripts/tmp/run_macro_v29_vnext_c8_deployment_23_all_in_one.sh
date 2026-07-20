#!/usr/bin/env bash
# Full seed1 c8 deployment validation over 23 traces; owns nohup.
set -euo pipefail

REPO=${REPO:-/data00/yinhaolang/LLMSim}
SCRIPT_PATH="$REPO/scripts/tmp/run_macro_v29_vnext_c8_deployment_23_all_in_one.sh"
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
RUN_DIR=${RUN_DIR:-$REPO/ckpt/macro_v29_vnext_d384_crossmacro_c8_s1_30k_20260719_025211}
DATASET_MANIFEST=${DATASET_MANIFEST:-/data00/yinhaolang/TCSim/data/v29_global_time_dataset/manifest.json}
STATIC_MANIFEST=${STATIC_MANIFEST:-$REPO/data/v28_1/static_dict/manifest.jsonl}
SEMANTIC_CACHE=${SEMANTIC_CACHE:-$REPO/data/v29_macro_semantic_cache_c8}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
MAX_STEPS=${MAX_STEPS:-0}
STRIDE_MACRO=${STRIDE_MACRO:-256}
PROGRESS_EVERY=${PROGRESS_EVERY:-100}
EVAL_NAME=${EVAL_NAME:-macro_v29_vnext_c8_seed1_deploy23_$(date +%Y%m%d_%H%M%S)}
OUTPUT_ROOT=${OUTPUT_ROOT:-$REPO/eval_results/$EVAL_NAME}
LOG_ROOT=${LOG_ROOT:-$REPO/logs/tmp/$EVAL_NAME}
# Project-local and short enough for any Python multiprocessing socket.
TASK_TMPDIR=${TASK_TMPDIR:-$REPO/tmp/mv29eval}

if [[ "${MACRO_V29_C8_DEPLOY_NOHUP_CHILD:-0}" != "1" ]]; then
  mkdir -p "$LOG_ROOT" "$OUTPUT_ROOT" "$TASK_TMPDIR"
  LAUNCHER_LOG="$LOG_ROOT/launcher.log"
  PID_FILE="$LOG_ROOT/launcher.pid"
  nohup env \
    MACRO_V29_C8_DEPLOY_NOHUP_CHILD=1 \
    REPO="$REPO" PY="$PY" RUN_DIR="$RUN_DIR" \
    DATASET_MANIFEST="$DATASET_MANIFEST" \
    STATIC_MANIFEST="$STATIC_MANIFEST" \
    SEMANTIC_CACHE="$SEMANTIC_CACHE" \
    GPUS="$GPUS" MAX_STEPS="$MAX_STEPS" \
    STRIDE_MACRO="$STRIDE_MACRO" PROGRESS_EVERY="$PROGRESS_EVERY" \
    EVAL_NAME="$EVAL_NAME" OUTPUT_ROOT="$OUTPUT_ROOT" \
    LOG_ROOT="$LOG_ROOT" TASK_TMPDIR="$TASK_TMPDIR" \
    bash "$SCRIPT_PATH" >"$LAUNCHER_LOG" 2>&1 < /dev/null &
  LAUNCHER_PID=$!
  echo "$LAUNCHER_PID" > "$PID_FILE"
  echo "started pid=$LAUNCHER_PID"
  echo "log=$LAUNCHER_LOG"
  echo "output=$OUTPUT_ROOT"
  exit 0
fi

cd "$REPO"
mkdir -p "$LOG_ROOT" "$OUTPUT_ROOT" "$TASK_TMPDIR"
export TMPDIR="$TASK_TMPDIR"
export PYTHONPATH="$REPO:/data00/yinhaolang/TCSim:${PYTHONPATH:-}"
export HF_HUB_OFFLINE=${HF_HUB_OFFLINE:-1}
export TRANSFORMERS_OFFLINE=${TRANSFORMERS_OFFLINE:-1}
export TOKENIZERS_PARALLELISM=false

exec "$PY" scripts/run_macro_v29_c8_deployment_suite.py \
  --run-dir "$RUN_DIR" \
  --dataset-manifest "$DATASET_MANIFEST" \
  --static-manifest "$STATIC_MANIFEST" \
  --semantic-cache "$SEMANTIC_CACHE" \
  --output-root "$OUTPUT_ROOT" \
  --gpus "$GPUS" \
  --split deployment_inference \
  --cores 8 \
  --max-steps "$MAX_STEPS" \
  --stride-macro "$STRIDE_MACRO" \
  --progress-every "$PROGRESS_EVERY" \
  --tmp-root "$TASK_TMPDIR"
