#!/usr/bin/env bash
# One-click c8 mainline: build/reuse semantic cache, then train 8000 steps.
set -euo pipefail

REPO=${REPO:-/data00/yinhaolang/LLMSim}
SCRIPT_PATH="$REPO/scripts/tmp/run_macro_v29_semantic_c8_8k_all_in_one.sh"
SEMANTIC_CACHE=${SEMANTIC_CACHE:-$REPO/data/v29_macro_semantic_cache_c8}
RUN_NAME=${RUN_NAME:-macro_v29_semantic_c8_8k_$(date +%Y%m%d_%H%M%S)}
OUTPUT=${OUTPUT:-$REPO/ckpt/$RUN_NAME}
LOG_ROOT=${LOG_ROOT:-$REPO/logs/tmp/$RUN_NAME}
TASK_TMPDIR=${TASK_TMPDIR:-$REPO/tmp/$RUN_NAME}

if [[ "${MACRO_V29_NOHUP_CHILD:-0}" != "1" ]]; then
  mkdir -p "$LOG_ROOT" "$TASK_TMPDIR"
  LAUNCHER_LOG="$LOG_ROOT/launcher.log"
  PID_FILE="$LOG_ROOT/launcher.pid"
  nohup env \
    MACRO_V29_NOHUP_CHILD=1 \
    REPO="$REPO" \
    SEMANTIC_CACHE="$SEMANTIC_CACHE" \
    RUN_NAME="$RUN_NAME" \
    OUTPUT="$OUTPUT" \
    LOG_ROOT="$LOG_ROOT" \
    TASK_TMPDIR="$TASK_TMPDIR" \
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

echo "[1/2] build or verify c8 semantic cache: $SEMANTIC_CACHE"
bash scripts/build_macro_v29_semantic_cache.sh \
  2>&1 | tee "$LOG_ROOT/build_semantic_cache.log"

echo "[2/2] start fresh c8 8000-step training: $OUTPUT"
RUN_NAME="$RUN_NAME" \
OUTPUT="$OUTPUT" \
STEPS=8000 \
bash scripts/train_macro_v29_semantic_supervised.sh \
  2>&1 | tee "$LOG_ROOT/train_8000.log"
