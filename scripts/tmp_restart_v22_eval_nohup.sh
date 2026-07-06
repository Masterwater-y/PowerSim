#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
cd "$ROOT"

export TMPDIR="${TMPDIR:-$ROOT/tmp}"
mkdir -p "$TMPDIR" logs

echo "[restart] stop old v22 eval processes"
pkill -TERM -f "eval_quota_cycles.py --raw-root data/raw_v7_seedB_c" 2>/dev/null || true
pkill -TERM -f "run_v22_bind_split_direct_c04_c08_c16_c32_eval.sh" 2>/dev/null || true
sleep 5

echo "[restart] delete old v22 eval logs"
rm -rf \
  logs/eval_parallel_v22_bind_split_direct_best_c*_full_* \
  logs/eval_v22_bind_split_direct_c04_c08_c16_c32_*.driver.log \
  logs/eval_v22_bind_split_direct_c04_c08_c16_c32_*.nohup.log \
  logs/eval_v22_bind_split_direct_c04_c08_c16_c32.nohup.log

RUN_TS=$(date +%Y%m%d_%H%M%S)
LOG="logs/eval_v22_bind_split_direct_c04_c08_c16_c32_${RUN_TS}.nohup.log"

echo "[restart] launch nohup"
nohup bash scripts/run_v22_bind_split_direct_c04_c08_c16_c32_eval.sh \
  > "$LOG" 2>&1 &

PID=$!
echo "$PID" > logs/eval_v22_bind_split_direct_c04_c08_c16_c32.pid
echo "[restart] pid=$PID"
echo "[restart] log=$LOG"
echo "tail -f $LOG"
