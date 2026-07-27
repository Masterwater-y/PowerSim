#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT=${ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
cd "$PROJECT_ROOT"

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
CKPT=${CKPT:-$PROJECT_ROOT/ckpt/tcsim_v29_long_history_100m_8gpu_60000/last.pt}
MANIFEST=${MANIFEST:-$PROJECT_ROOT/data/v29_long_history_dataset/manifest.json}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
SPLITS=${SPLITS:-seed0_inference,development_heldout,deployment_inference}
CORE_COUNTS=${CORE_COUNTS:-4,8,16,32}
TARGET_STRIDE=${TARGET_STRIDE:-256}
MAX_STEP_CYCLES=${MAX_STEP_CYCLES:-1024}
MAX_RESTARTS=${MAX_RESTARTS:-3}
RUN_TAG=${RUN_TAG:-v29_long_history_last60k_serial_free_s256_seed0_seed1_c04_c32_full}
OUT_DIR=${OUT:-$PROJECT_ROOT/logs/$RUN_TAG}
WATCHDOG_LOG=${WATCHDOG_LOG:-$OUT_DIR/watchdog.log}
PID_FILE=${PID_FILE:-$PROJECT_ROOT/scripts/tmp/$RUN_TAG.watchdog.pid}
LOCK_FILE=${LOCK_FILE:-$PROJECT_ROOT/scripts/tmp/$RUN_TAG.watchdog.lock}

[[ -x "$PY" ]] || { echo "[v29-long-serial][ERROR] missing Python: $PY" >&2; exit 2; }
[[ -f "$CKPT" ]] || { echo "[v29-long-serial][ERROR] missing checkpoint: $CKPT" >&2; exit 2; }
[[ -f "$MANIFEST" ]] || { echo "[v29-long-serial][ERROR] missing manifest: $MANIFEST" >&2; exit 2; }

run_watchdog() {
  mkdir -p "$OUT_DIR" "$PROJECT_ROOT/scripts/tmp"
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    echo "[v29-long-serial][ERROR] watchdog lock is already held: $LOCK_FILE" >&2
    exit 2
  fi
  printf '%s\n' "$$" >"$PID_FILE"

  eval_pid=""
  stop_children() {
    if [[ "$eval_pid" =~ ^[0-9]+$ ]]; then
      kill -TERM "$eval_pid" 2>/dev/null || true
      wait "$eval_pid" 2>/dev/null || true
    fi
    exit 130
  }
  trap stop_children INT TERM

  restart=0
  while (( restart <= MAX_RESTARTS )); do
    printf '\n[%s] watchdog_attempt=%d/%d mode=free window_parallel=serial splits=%s cores=%s stride=%s\n' \
      "$(date '+%Y-%m-%d %H:%M:%S')" "$((restart + 1))" "$((MAX_RESTARTS + 1))" \
      "$SPLITS" "$CORE_COUNTS" "$TARGET_STRIDE"

    env \
      ROOT="$PROJECT_ROOT" \
      PY="$PY" \
      CKPT="$CKPT" \
      MANIFEST="$MANIFEST" \
      OUT="$OUT_DIR" \
      GPUS="$GPUS" \
      SPLITS="$SPLITS" \
      MODE=free \
      WINDOW_PARALLEL_MODE=serial \
      WINDOW_PARALLEL_DEVICES= \
      CORE_COUNTS="$CORE_COUNTS" \
      MAX_ORACLE_SAMPLES=0 \
      MAX_FREE_STEPS=0 \
      TARGET_STRIDE="$TARGET_STRIDE" \
      MIN_STEP_CYCLES=4 \
      MAX_STEP_CYCLES="$MAX_STEP_CYCLES" \
      MAX_NO_PROGRESS_STEPS=64 \
      AMP_DTYPE=bf16 \
      SDPA_BACKEND=auto \
      PROGRESS_EVERY=100 \
      ORACLE_DRIFT_DIAGNOSTICS=0 \
      RESUME=1 \
      bash scripts/run_v29_eval_8gpu.sh &
    eval_pid=$!

    set +e
    wait "$eval_pid"
    status=$?
    set -e
    eval_pid=""
    if (( status == 0 )); then
      echo "[v29-long-serial] PASS report=$OUT_DIR/report.txt"
      return 0
    fi

    echo "[v29-long-serial][WARN] evaluator exit_status=$status"
    restart=$((restart + 1))
    if (( restart > MAX_RESTARTS )); then
      echo "[v29-long-serial][ERROR] restart budget exhausted" >&2
      return "$status"
    fi
    echo "[v29-long-serial] restarting in 10s; completed traces will resume"
    sleep 10
  done
}

if [[ "${1:-}" == "--watchdog" ]]; then
  run_watchdog
  exit $?
fi

if [[ -f "$PID_FILE" ]]; then
  existing_pid=$(<"$PID_FILE")
  if [[ "$existing_pid" =~ ^[0-9]+$ ]] && kill -0 "$existing_pid" 2>/dev/null; then
    echo "[v29-long-serial][ERROR] evaluation watchdog is already running: pid=$existing_pid" >&2
    echo "[v29-long-serial] log=$WATCHDOG_LOG" >&2
    exit 2
  fi
fi

mkdir -p "$OUT_DIR" "$PROJECT_ROOT/scripts/tmp"
nohup env \
  ROOT="$PROJECT_ROOT" \
  PY="$PY" \
  CKPT="$CKPT" \
  MANIFEST="$MANIFEST" \
  GPUS="$GPUS" \
  SPLITS="$SPLITS" \
  CORE_COUNTS="$CORE_COUNTS" \
  TARGET_STRIDE="$TARGET_STRIDE" \
  MAX_STEP_CYCLES="$MAX_STEP_CYCLES" \
  MAX_RESTARTS="$MAX_RESTARTS" \
  RUN_TAG="$RUN_TAG" \
  OUT="$OUT_DIR" \
  WATCHDOG_LOG="$WATCHDOG_LOG" \
  PID_FILE="$PID_FILE" \
  LOCK_FILE="$LOCK_FILE" \
  bash "$0" --watchdog \
  >>"$WATCHDOG_LOG" 2>&1 &

watchdog_pid=$!
printf '%s\n' "$watchdog_pid" >"$PID_FILE"

printf '[v29-long-serial] watchdog_pid=%s\n' "$watchdog_pid"
printf '[v29-long-serial] checkpoint=%s\n' "$CKPT"
printf '[v29-long-serial] traces=184 (seed0=92 seed1=92) cores=%s\n' "$CORE_COUNTS"
printf '[v29-long-serial] mode=free window_parallel=serial depth=1 stride=%s\n' "$TARGET_STRIDE"
printf '[v29-long-serial] out=%s\n' "$OUT_DIR"
printf '[v29-long-serial] log=%s\n' "$WATCHDOG_LOG"
printf '[v29-long-serial] report=%s/report.txt\n' "$OUT_DIR"
