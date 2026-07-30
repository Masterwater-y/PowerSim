#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
cd "$ROOT"
mkdir -p logs/cache

RUN_NAME=${RUN_NAME:-v30_exposure_v1_cache}
LOG=${LOG:-logs/cache/${RUN_NAME}.log}
PID_FILE=${PID_FILE:-logs/cache/${RUN_NAME}.pid}

if [[ -f "$PID_FILE" ]]; then
  OLD_PID=$(cat "$PID_FILE")
  if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "already running: pid=$OLD_PID log=$LOG"
    exit 0
  fi
fi

nohup setsid /data00/yinhaolang/infer/.venv/bin/python \
  scripts/build_v30_exposure_sidecar.py \
  --manifest "${MANIFEST:-data/v30_gss_ready_dataset/manifest.json}" \
  --splits "${SPLITS:-train,validation}" \
  --core-counts "${CORE_COUNTS:-1,4,8,16,32}" \
  --workers "${WORKERS:-8}" \
  --out-root "${OUT_ROOT:-data/v30_exposure_v1_sidecars}" \
  --write-manifest "${OUT_MANIFEST:-data/v30_exposure_v1_dataset/manifest.json}" \
  > "$LOG" 2>&1 < /dev/null &

PID=$!
echo "$PID" > "$PID_FILE"
echo "started: $RUN_NAME pid=$PID"
echo "log: $LOG"
echo "manifest: ${OUT_MANIFEST:-data/v30_exposure_v1_dataset/manifest.json}"
