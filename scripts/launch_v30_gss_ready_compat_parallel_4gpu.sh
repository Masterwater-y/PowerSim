#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT=${ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
cd "$PROJECT_ROOT"

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
CKPT=${CKPT:-$PROJECT_ROOT/ckpt/tcsim_v30_gss_joint_v2_60k_seed1234/last.pt}
MANIFEST=${MANIFEST:-$PROJECT_ROOT/data/v30_gss_ready_dataset/manifest.json}
GPUS=${GPUS:-0,1,2,3}
SPLITS=${SPLITS:-deployment_inference,development_heldout}
CORE_COUNTS=${CORE_COUNTS:-4,8,16,32}
MODES=${MODES:-speculative}
WINDOW_SHIFT=${WINDOW_SHIFT:-64}
TARGET_STRIDE=${TARGET_STRIDE:-256}
MAX_RESTARTS=${MAX_RESTARTS:-3}
RUN_TAG=${RUN_TAG:-v30_gss_step38000_readycompat_parallel_relaxed_4gpu}
OUT_DIR=${OUT:-$PROJECT_ROOT/logs/$RUN_TAG}
WATCHDOG_LOG=${WATCHDOG_LOG:-$OUT_DIR/watchdog.log}
PID_FILE=${PID_FILE:-$PROJECT_ROOT/scripts/tmp/$RUN_TAG.watchdog.pid}
LOCK_FILE=${LOCK_FILE:-$PROJECT_ROOT/scripts/tmp/$RUN_TAG.watchdog.lock}

fail() {
  echo "[v30-ready-compat][ERROR] $*" >&2
  exit 2
}

[[ -x "$PY" ]] || fail "missing Python: $PY"
[[ -f "$CKPT" ]] || fail "missing checkpoint: $CKPT"
[[ -f "$MANIFEST" ]] || fail "missing manifest: $MANIFEST"

print_configuration() {
  "$PY" - "$CKPT" "$MANIFEST" "$SPLITS" "$CORE_COUNTS" <<'PY'
import json
import sys
import torch

checkpoint_path, manifest_path, split_csv, core_csv = sys.argv[1:]
try:
    checkpoint = torch.load(checkpoint_path, map_location="cpu", weights_only=False)
except TypeError:
    checkpoint = torch.load(checkpoint_path, map_location="cpu")
step = int(checkpoint.get("step") or 0)
gss = checkpoint.get("contract", {}).get("gss", {})
if gss.get("clock_source") != "ready":
    raise SystemExit(
        f"compat launcher requires a ready-clock checkpoint, got {gss.get('clock_source')}"
    )
if gss.get("order_policy") != "ready_tick_then_core_then_uop_v1":
    raise SystemExit(f"unexpected ready-clock order: {gss.get('order_policy')}")
with open(manifest_path, "r", encoding="utf-8") as handle:
    manifest = json.load(handle)
splits = [value.strip() for value in split_csv.split(",") if value.strip()]
cores = {int(value) for value in core_csv.split(",") if value.strip()}
missing = [name for name in splits if name not in manifest.get("splits", {})]
if missing:
    raise SystemExit(f"manifest is missing splits: {missing}")
selected = [
    row for split in splits for row in manifest["splits"][split]
    if int(row.get("n_cores", -1)) in cores
]
if len(selected) != 120:
    raise SystemExit(f"expected 120 seed1+heldout traces, found {len(selected)}")
print(f"[v30-ready-compat] checkpoint_step={step} training_clock=ready")
print(f"[v30-ready-compat] traces={len(selected)} splits={splits} cores={sorted(cores)}")
print("[v30-ready-compat] accuracy=exploratory-approximate formal=false throughput=true")
PY
  echo "[v30-ready-compat] checkpoint=$CKPT"
  echo "[v30-ready-compat] gpus=$GPUS mode=$MODES shift=$WINDOW_SHIFT stride=$TARGET_STRIDE"
  echo "[v30-ready-compat] out=$OUT_DIR"
  echo "[v30-ready-compat] log=$WATCHDOG_LOG"
}

run_watchdog() {
  mkdir -p "$OUT_DIR" "$PROJECT_ROOT/scripts/tmp"
  exec 9>"$LOCK_FILE"
  flock -n 9 || fail "watchdog lock is already held: $LOCK_FILE"
  printf '%s\n' "$$" >"$PID_FILE"

  child_pid=""
  cleanup() { rm -f "$PID_FILE"; }
  stop_child() {
    if [[ "$child_pid" =~ ^[0-9]+$ ]]; then
      kill -TERM "$child_pid" 2>/dev/null || true
      wait "$child_pid" 2>/dev/null || true
    fi
    exit 130
  }
  trap cleanup EXIT
  trap stop_child INT TERM

  local restart=0
  while (( restart <= MAX_RESTARTS )); do
    printf '\n[%s] watchdog_attempt=%d/%d\n' \
      "$(date '+%Y-%m-%d %H:%M:%S')" "$((restart + 1))" "$((MAX_RESTARTS + 1))"
    set +e
    env \
      TCSIM_GSS_BACKEND=native \
      ROOT="$PROJECT_ROOT" PY="$PY" CKPT="$CKPT" MANIFEST="$MANIFEST" \
      GPUS="$GPUS" SPLITS="$SPLITS" CORE_COUNTS="$CORE_COUNTS" \
      MODES="$MODES" WINDOW_SHIFT="$WINDOW_SHIFT" \
      TARGET_STRIDE="$TARGET_STRIDE" WINDOW_CONTEXT_BACKEND=process \
      OUT="$OUT_DIR" RESUME=1 FAIL_FAST=1 MAX_TRACES=0 MAX_FREE_STEPS=0 \
      MAX_CORE_STALL_STEPS=256 PROGRESS_EVERY=100 \
      ORACLE_DRIFT_DIAGNOSTICS=0 ALLOW_READY_CLOCK_GSS_COMPAT=1 \
      bash scripts/run_v30_gss_window_parallel_4gpu.sh &
    child_pid=$!
    wait "$child_pid"
    status=$?
    set -e
    child_pid=""
    if (( status == 0 )); then
      for mode in ${MODES//,/ }; do
        [[ -s "$OUT_DIR/$mode/report.json" ]] \
          || fail "successful evaluator lacks $mode/report.json"
      done
      echo "[v30-ready-compat] PASS out=$OUT_DIR"
      return 0
    fi
    restart=$((restart + 1))
    echo "[v30-ready-compat][WARN] evaluator exit=$status restart=$restart/$MAX_RESTARTS"
    (( restart <= MAX_RESTARTS )) || fail "restart budget exhausted"
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
  "") ;;
  *)
    echo "usage: $0 [--check|--watchdog]" >&2
    exit 2
    ;;
esac

if [[ -f "$PID_FILE" ]]; then
  existing_pid=$(<"$PID_FILE")
  if [[ "$existing_pid" =~ ^[0-9]+$ ]] \
      && kill -0 "$existing_pid" 2>/dev/null \
      && [[ -r "/proc/$existing_pid/cmdline" ]] \
      && grep -aFq "launch_v30_gss_ready_compat_parallel_4gpu.sh" \
        "/proc/$existing_pid/cmdline"; then
    fail "evaluation watchdog already running: pid=$existing_pid"
  fi
fi

mkdir -p "$OUT_DIR" "$PROJECT_ROOT/scripts/tmp"
print_configuration
nohup env \
  ROOT="$PROJECT_ROOT" PY="$PY" CKPT="$CKPT" MANIFEST="$MANIFEST" \
  GPUS="$GPUS" SPLITS="$SPLITS" CORE_COUNTS="$CORE_COUNTS" MODES="$MODES" \
  WINDOW_SHIFT="$WINDOW_SHIFT" TARGET_STRIDE="$TARGET_STRIDE" \
  MAX_RESTARTS="$MAX_RESTARTS" RUN_TAG="$RUN_TAG" OUT="$OUT_DIR" \
  WATCHDOG_LOG="$WATCHDOG_LOG" PID_FILE="$PID_FILE" LOCK_FILE="$LOCK_FILE" \
  bash "$0" --watchdog >>"$WATCHDOG_LOG" 2>&1 &
watchdog_pid=$!
printf '%s\n' "$watchdog_pid" >"$PID_FILE"

echo "[v30-ready-compat] started watchdog_pid=$watchdog_pid"
echo "[v30-ready-compat] monitor: tail -f $WATCHDOG_LOG"
echo "[v30-ready-compat] final report: $OUT_DIR/speculative/report.txt"
