#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
cd "$ROOT"
mkdir -p logs/cache

RUN_NAME=${RUN_NAME:-v30_exposure_v1_training_cache_w128}
LOG=${LOG:-logs/cache/${RUN_NAME}.log}
PID_FILE=${PID_FILE:-logs/cache/${RUN_NAME}.pid}
WORKERS=${WORKERS:-128}

if [[ -f "$PID_FILE" ]]; then
  OLD_PID=$(cat "$PID_FILE")
  if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "already running: pid=$OLD_PID log=$LOG"
    exit 0
  fi
fi

nohup setsid env \
  ROOT="$ROOT" \
  WORKERS="$WORKERS" \
  SPLITS="${SPLITS:-train,validation}" \
  CORE_COUNTS="${CORE_COUNTS:-1,4,8,16,32}" \
  BASE_MANIFEST="${BASE_MANIFEST:-data/v29_global_time_dataset/manifest.json}" \
  GSS_ROOT="${GSS_ROOT:-data/v30_gss_commit_sidecars}" \
  GSS_MANIFEST="${GSS_MANIFEST:-data/v30_gss_commit_dataset/manifest.json}" \
  EXPOSURE_ROOT="${EXPOSURE_ROOT:-data/v30_exposure_v1_sidecars}" \
  FINAL_MANIFEST="${FINAL_MANIFEST:-data/v30_exposure_v1_dataset/manifest.json}" \
  bash scripts/run_v30_exposure_training_cache.sh \
  > "$LOG" 2>&1 < /dev/null &

PID=$!
echo "$PID" > "$PID_FILE"
echo "started: $RUN_NAME pid=$PID workers=$WORKERS"
echo "log: $LOG"
echo "final manifest: ${FINAL_MANIFEST:-data/v30_exposure_v1_dataset/manifest.json}"
