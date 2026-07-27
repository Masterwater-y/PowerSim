#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT=${ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
cd "$PROJECT_ROOT"

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
CKPT=${CKPT:-$PROJECT_ROOT/ckpt/tcsim_v29_memory_gated_e2_100m_8gpu_60000/best_post_coverage.pt}
MANIFEST=${MANIFEST:-$PROJECT_ROOT/data/v29_long_history_dataset/manifest.json}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
# Requested validation set: seed0 business heldout (28 traces) plus the complete
# seed1 deployment set (92 traces).  It intentionally excludes seed0 base.
SPLITS=${SPLITS:-development_heldout,deployment_inference}
CORE_COUNTS=${CORE_COUNTS:-4,8,16,32}
TARGET_STRIDE=${TARGET_STRIDE:-256}
MAX_STEP_CYCLES=${MAX_STEP_CYCLES:-1024}
MAX_RESTARTS=${MAX_RESTARTS:-3}
RUN_TAG=${RUN_TAG:-v29_memory_gated_e2_best50k_seed1_heldout_c04_c32_serial_s256_8gpu}
OUT_DIR=${OUT:-$PROJECT_ROOT/logs/$RUN_TAG}
WATCHDOG_LOG=${WATCHDOG_LOG:-$OUT_DIR/watchdog.log}
PID_FILE=${PID_FILE:-$PROJECT_ROOT/scripts/tmp/$RUN_TAG.watchdog.pid}
LOCK_FILE=${LOCK_FILE:-$PROJECT_ROOT/scripts/tmp/$RUN_TAG.watchdog.lock}

fail() {
  echo "[v29-e2-infer][ERROR] $*" >&2
  exit 2
}

[[ -x "$PY" ]] || fail "missing Python: $PY"
[[ -f "$CKPT" ]] || fail "missing checkpoint: $CKPT"
[[ -f "$MANIFEST" ]] || fail "missing manifest: $MANIFEST"

print_configuration() {
  "$PY" - "$MANIFEST" "$SPLITS" "$CORE_COUNTS" <<'PY'
import json
import sys

manifest_path, split_csv, core_csv = sys.argv[1:]
manifest = json.load(open(manifest_path, "r", encoding="utf-8"))
splits = [value.strip() for value in split_csv.split(",") if value.strip()]
cores = {int(value) for value in core_csv.split(",") if value.strip()}
missing = [name for name in splits if name not in manifest.get("splits", {})]
if missing:
    raise SystemExit(f"manifest is missing splits: {missing}")
selected = [
    item
    for name in splits
    for item in manifest["splits"][name]
    if int(item.get("n_cores", -1)) in cores
]
missing_history = [item.get("trace_id", "<unknown>") for item in selected if not item.get("long_history_dir")]
if missing_history:
    raise SystemExit(
        f"{len(missing_history)} selected traces have no long-history sidecar; "
        f"first={missing_history[0]}"
    )
by_split = {
    name: sum(int(item.get("n_cores", -1)) in cores for item in manifest["splits"][name])
    for name in splits
}
print(f"[v29-e2-infer] selected_traces={len(selected)} by_split={by_split}")
PY
  echo "[v29-e2-infer] checkpoint=$CKPT"
  echo "[v29-e2-infer] manifest=$MANIFEST"
  echo "[v29-e2-infer] splits=$SPLITS cores=$CORE_COUNTS"
  echo "[v29-e2-infer] gpus=$GPUS mode=free window_parallel=serial depth=1 stride=$TARGET_STRIDE"
  echo "[v29-e2-infer] out=$OUT_DIR"
  echo "[v29-e2-infer] log=$WATCHDOG_LOG"
}

run_watchdog() {
  mkdir -p "$OUT_DIR" "$PROJECT_ROOT/scripts/tmp"
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    fail "watchdog lock is already held: $LOCK_FILE"
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
    printf '\n[%s] watchdog_attempt=%d/%d splits=%s cores=%s mode=free window_parallel=serial stride=%s\n' \
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
      WINDOW_CONTEXT_BACKEND=process \
      CORE_COUNTS="$CORE_COUNTS" \
      WORKLOADS= \
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
      [[ -s "$OUT_DIR/report.json" ]] || fail "evaluator exited successfully but report.json is missing"
      [[ -s "$OUT_DIR/report.txt" ]] || fail "evaluator exited successfully but report.txt is missing"
      echo "[v29-e2-infer] PASS report=$OUT_DIR/report.txt"
      return 0
    fi

    echo "[v29-e2-infer][WARN] evaluator exit_status=$status"
    restart=$((restart + 1))
    if (( restart > MAX_RESTARTS )); then
      echo "[v29-e2-infer][ERROR] restart budget exhausted" >&2
      return "$status"
    fi
    echo "[v29-e2-infer] restarting in 10s; completed traces will be skipped"
    sleep 10
  done
}

case "${1:-}" in
  --check)
    print_configuration
    exit 0
    ;;
  --watchdog)
    run_watchdog
    exit $?
    ;;
  "")
    ;;
  *)
    echo "usage: $0 [--check]" >&2
    exit 2
    ;;
esac

if [[ -f "$PID_FILE" ]]; then
  existing_pid=$(<"$PID_FILE")
  if [[ "$existing_pid" =~ ^[0-9]+$ ]] && kill -0 "$existing_pid" 2>/dev/null; then
    fail "evaluation watchdog is already running: pid=$existing_pid log=$WATCHDOG_LOG"
  fi
fi

mkdir -p "$OUT_DIR" "$PROJECT_ROOT/scripts/tmp"
print_configuration
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

echo "[v29-e2-infer] started watchdog_pid=$watchdog_pid"
echo "[v29-e2-infer] monitor: tail -f $WATCHDOG_LOG"
echo "[v29-e2-infer] final report: $OUT_DIR/report.txt"
