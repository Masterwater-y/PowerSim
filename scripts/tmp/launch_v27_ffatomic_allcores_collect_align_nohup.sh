#!/usr/bin/env bash
# One-key nohup launcher for v27 ff-atomic raw trace collection + aligned
# parquet conversion across c01/c04/c08/c16/c32.
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TSim}
cd "$ROOT"

RUN_TAG=${RUN_TAG:-v27_ffatomic_seed${SEED:-0}}
TS=$(date +%Y%m%d_%H%M%S)
LOG_DIR=${LOG_DIR:-logs/tmp/${RUN_TAG}_${TS}}
mkdir -p "$LOG_DIR"

LOG_FILE="$LOG_DIR/nohup.out"
PID_FILE="$LOG_DIR/pid"

echo "[launch] ROOT=$ROOT"
echo "[launch] LOG_DIR=$LOG_DIR"
echo "[launch] LOG_FILE=$LOG_FILE"

nohup env \
  ROOT="$ROOT" \
  RUN_TAG="$RUN_TAG" \
  LOG_ROOT="$LOG_DIR" \
  "${@}" \
  bash scripts/tmp/tmp_v27_ffatomic_allcores_collect_align.sh \
  > "$LOG_FILE" 2>&1 &

pid=$!
echo "$pid" > "$PID_FILE"
echo "[launch] pid=$pid"
echo "[launch] tail -f $LOG_FILE"
