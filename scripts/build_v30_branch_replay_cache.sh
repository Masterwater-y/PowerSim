#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
BASE_MANIFEST=${BASE_MANIFEST:-data/v29_global_time_dataset/manifest.json}
OUT=${OUT:-data/v30_branch_replay_dataset}
SPLITS=${SPLITS:-train,validation}
WORKERS=${WORKERS:-32}
LOG_DIR=${LOG_DIR:-$ROOT/logs/v30_branch_replay_cache}
PID_FILE=${PID_FILE:-$LOG_DIR/current.pid}
SCRIPT_PATH=$(readlink -f "$0")

# The user-facing invocation returns immediately.  The child re-enters this
# script with a guard variable, writes all output to one immutable timestamped
# log, and retains the same PID across bash -> Python exec.
if [[ "${FOREGROUND:-0}" != "1" && "${V30_BRANCH_CACHE_NOHUP_CHILD:-0}" != "1" ]]; then
  mkdir -p "$LOG_DIR"
  if [[ -s "$PID_FILE" ]]; then
    existing_pid=$(<"$PID_FILE")
    if [[ "$existing_pid" =~ ^[0-9]+$ ]] && kill -0 "$existing_pid" 2>/dev/null; then
      echo "v30 branch cache build is already running: pid=$existing_pid"
      echo "log directory: $LOG_DIR"
      exit 0
    fi
  fi
  run_tag=$(date +%Y%m%d_%H%M%S)
  log_file="$LOG_DIR/build_${run_tag}.log"
  nohup env \
    V30_BRANCH_CACHE_NOHUP_CHILD=1 \
    ROOT="$ROOT" \
    PY="$PY" \
    BASE_MANIFEST="$BASE_MANIFEST" \
    OUT="$OUT" \
    SPLITS="$SPLITS" \
    WORKERS="$WORKERS" \
    LOG_DIR="$LOG_DIR" \
    PID_FILE="$PID_FILE" \
    "$SCRIPT_PATH" "$@" >"$log_file" 2>&1 &
  child_pid=$!
  echo "v30 branch cache build started: pid=$child_pid"
  echo "log: $log_file"
  echo "status: tail -f $log_file"
  exit 0
fi

mkdir -p "$LOG_DIR"
if [[ "${V30_BRANCH_CACHE_NOHUP_CHILD:-0}" == "1" ]]; then
  printf '%s\n' "$$" >"$PID_FILE"
  cleanup_pid_file() {
    if [[ -s "$PID_FILE" ]] && [[ "$(<"$PID_FILE")" == "$$" ]]; then
      rm -f "$PID_FILE"
    fi
  }
  trap cleanup_pid_file EXIT
fi

cd "$ROOT"
export PYTHONUNBUFFERED=1

echo "[v30-branch-cache] pid=$$ started=$(date --iso-8601=seconds) workers=$WORKERS"
set +e
"$PY" scripts/build_v30_branch_replay_cache.py \
  --manifest "$BASE_MANIFEST" \
  --out "$OUT" \
  --splits "$SPLITS" \
  --workers "$WORKERS" \
  "$@"
exit_code=$?
set -e
echo "[v30-branch-cache] pid=$$ finished=$(date --iso-8601=seconds) exit_code=$exit_code"
exit "$exit_code"
