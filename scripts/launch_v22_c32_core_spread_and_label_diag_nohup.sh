#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
cd "$ROOT"

TS=${TS:-$(date +%Y%m%d_%H%M%S)}
OUT_ROOT=${OUT_ROOT:-logs/v22_c32_core_spread_label_diag_${TS}}
DRIVER_LOG=${DRIVER_LOG:-${OUT_ROOT}.driver.log}
PID_FILE=${PID_FILE:-${OUT_ROOT}.pid}
FOREGROUND=${FOREGROUND:-0}

if [[ "${1:-}" == "--foreground" ]]; then
  FOREGROUND=1
fi

mkdir -p "$(dirname "$DRIVER_LOG")"

if [[ "$FOREGROUND" == "1" ]]; then
  echo "[launch] foreground=1"
  echo "[launch] out_root=$OUT_ROOT"
  echo "[launch] driver_log=$DRIVER_LOG"
  OUT_ROOT="$OUT_ROOT" \
    bash scripts/run_v22_c32_core_spread_and_label_diag.sh \
    2>&1 | tee "$DRIVER_LOG"
  exit "${PIPESTATUS[0]}"
fi

OUT_ROOT="$OUT_ROOT" \
  nohup bash scripts/run_v22_c32_core_spread_and_label_diag.sh \
  > "$DRIVER_LOG" 2>&1 &

pid=$!
echo "$pid" > "$PID_FILE"

echo "[launch] pid=$pid"
echo "[launch] out_root=$OUT_ROOT"
echo "[launch] driver_log=$DRIVER_LOG"
echo "[launch] pid_file=$PID_FILE"
echo "[launch] tail -f $DRIVER_LOG"
