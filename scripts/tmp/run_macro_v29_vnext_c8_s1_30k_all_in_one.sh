#!/usr/bin/env bash
# One detached command for c8 vNext, sequence_length=1, 30000 steps.
# The script owns nohup and always starts a fresh timing model.
set -euo pipefail

REPO=${REPO:-/data00/yinhaolang/LLMSim}
SCRIPT_PATH="$REPO/scripts/tmp/run_macro_v29_vnext_c8_s1_30k_all_in_one.sh"
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
SEMANTIC_CACHE=${SEMANTIC_CACHE:-$REPO/data/v29_macro_semantic_cache_c8}
RUN_NAME=${RUN_NAME:-macro_v29_vnext_d384_crossmacro_c8_s1_30k_$(date +%Y%m%d_%H%M%S)}
OUTPUT=${OUTPUT:-$REPO/ckpt/$RUN_NAME}
LOG_ROOT=${LOG_ROOT:-$REPO/logs/tmp/$RUN_NAME}
# Keep this project-local path short: Python multiprocessing appends its own
# AF_UNIX socket suffix, whose complete path must remain below 108 bytes.
TASK_TMPDIR=${TASK_TMPDIR:-$REPO/tmp/mv29}

if [[ "${MACRO_V29_VNEXT_S1_30K_NOHUP_CHILD:-0}" != "1" ]]; then
  mkdir -p "$LOG_ROOT" "$TASK_TMPDIR"
  LAUNCHER_LOG="$LOG_ROOT/launcher.log"
  PID_FILE="$LOG_ROOT/launcher.pid"
  nohup env \
    MACRO_V29_VNEXT_S1_30K_NOHUP_CHILD=1 \
    REPO="$REPO" \
    PY="$PY" \
    SEMANTIC_CACHE="$SEMANTIC_CACHE" \
    RUN_NAME="$RUN_NAME" \
    OUTPUT="$OUTPUT" \
    LOG_ROOT="$LOG_ROOT" \
    TASK_TMPDIR="$TASK_TMPDIR" \
    GPUS="${GPUS:-0,1,2,3,4,5,6,7}" \
    NPROC="${NPROC:-8}" \
    MASTER_PORT="${MASTER_PORT:-29649}" \
    RUN_PREFLIGHT="${RUN_PREFLIGHT:-1}" \
    BACKBONE_CORE_CHUNK_SIZE="${BACKBONE_CORE_CHUNK_SIZE:-8}" \
    CROSS_TARGET_BLOCK="${CROSS_TARGET_BLOCK:-8}" \
    BACKBONE_CHUNK_CHECKPOINT="${BACKBONE_CHUNK_CHECKPOINT:-1}" \
    GRADIENT_CHECKPOINTING="${GRADIENT_CHECKPOINTING:-0}" \
    bash "$SCRIPT_PATH" >"$LAUNCHER_LOG" 2>&1 < /dev/null &
  LAUNCHER_PID=$!
  echo "$LAUNCHER_PID" > "$PID_FILE"
  echo "started pid=$LAUNCHER_PID"
  echo "log=$LAUNCHER_LOG"
  echo "output=$OUTPUT"
  exit 0
fi

cd "$REPO"
mkdir -p "$LOG_ROOT" "$TASK_TMPDIR"
export TMPDIR="$TASK_TMPDIR"
export SEMANTIC_CACHE

if [[ "${RUN_PREFLIGHT:-1}" == "1" ]]; then
  echo "[1/3] vNext architecture and gradient contract tests"
  "$PY" -m unittest \
    tests.test_macro_v29_model \
    tests.test_macro_v29_semantic_model -v \
    2>&1 | tee "$LOG_ROOT/preflight.log"
fi

echo "[2/3] build or verify c8 semantic cache: $SEMANTIC_CACHE"
CORES=8 bash scripts/build_macro_v29_semantic_cache.sh \
  2>&1 | tee "$LOG_ROOT/build_semantic_cache.log"

echo "[3/3] start fresh c8 vNext sequence_length=1 30000-step training: $OUTPUT"
RUN_NAME="$RUN_NAME" \
OUTPUT="$OUTPUT" \
STEPS=30000 \
SEQUENCE_LENGTH=1 \
bash scripts/train_macro_v29_vnext_c8.sh \
  2>&1 | tee "$LOG_ROOT/train_30000.log"
