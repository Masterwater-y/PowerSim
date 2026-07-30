#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
cd "$ROOT"

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
BASE_MANIFEST=${BASE_MANIFEST:-data/v29_global_time_dataset/manifest.json}
V30_CACHE_ROOT=${V30_CACHE_ROOT:-data/v30_branch_replay_dataset}
MANIFEST=${MANIFEST:-$V30_CACHE_ROOT/manifest.json}
CKPT_60K=${CKPT_60K:-ckpt/tcsim_v30_branch_b3_replay_history_100m_8gpu_60000/step_60000.pt}
CKPT_90K_BEST=${CKPT_90K_BEST:-ckpt/tcsim_v30_branch_b3_replay_history_100m_8gpu_60000/best.pt}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
SPLITS=${SPLITS:-development_heldout,deployment_inference}
CORE_COUNTS=${CORE_COUNTS:-4,8,16,32}
TARGET_STRIDE=${TARGET_STRIDE:-256}
MAX_STEP_CYCLES=${MAX_STEP_CYCLES:-1024}
CACHE_WORKERS=${CACHE_WORKERS:-128}
MAX_RESTARTS=${MAX_RESTARTS:-3}
RUN_TAG=${RUN_TAG:-v30_b3_exact60k_vs_best90k_seed1_heldout_c04_c32_serial_s256_8gpu}
OUT_ROOT=${OUT:-$ROOT/logs/$RUN_TAG}
OUT_60K=${OUT_60K:-$OUT_ROOT/exact_60k}
OUT_90K_BEST=${OUT_90K_BEST:-$OUT_ROOT/best_through_90k}
CONTROLLER_LOG=${CONTROLLER_LOG:-$OUT_ROOT/controller.log}
PID_FILE=${PID_FILE:-$ROOT/scripts/tmp/$RUN_TAG.controller.pid}
LOCK_FILE=${LOCK_FILE:-$ROOT/scripts/tmp/$RUN_TAG.controller.lock}
ACTIVE_PID=""

fail() {
  echo "[v30-b3-dual-infer][ERROR] $*" >&2
  exit 2
}

checkpoint_info() {
  "$PY" - "$1" <<'PY'
import sys
import torch

path = sys.argv[1]
try:
    payload = torch.load(path, map_location="cpu", weights_only=False)
except TypeError:
    payload = torch.load(path, map_location="cpu")
mode = payload.get("config", {}).get("model", {}).get("branch_mode")
print(int(payload.get("step") or 0), mode or "", payload.get("best_validation"))
PY
}

validate_static_inputs() {
  [[ -x "$PY" ]] || fail "missing Python: $PY"
  [[ -f "$BASE_MANIFEST" ]] || fail "missing base manifest: $BASE_MANIFEST"
  [[ -f "$CKPT_60K" ]] || fail "missing exact-60k checkpoint: $CKPT_60K"
  [[ -f "$CKPT_90K_BEST" ]] || \
    fail "missing best-through-90k checkpoint: $CKPT_90K_BEST"
  [[ -f scripts/run_v29_eval_8gpu.sh ]] || fail "missing 8-GPU evaluator"
  [[ -f scripts/build_v30_branch_replay_cache.py ]] || \
    fail "missing v30 replay-cache builder"
  [[ -f scripts/compare_v30_b3_dual_eval.py ]] || \
    fail "missing dual-report comparator"

  local step mode best
  read -r step mode best <<<"$(checkpoint_info "$CKPT_60K")"
  [[ "$step" == "60000" ]] || \
    fail "CKPT_60K must be exact step 60000, found $step"
  [[ "$mode" == "replay_event_history" ]] || \
    fail "CKPT_60K is not B3 replay_event_history: $mode"
  read -r step mode best <<<"$(checkpoint_info "$CKPT_90K_BEST")"
  [[ "$mode" == "replay_event_history" ]] || \
    fail "CKPT_90K_BEST is not B3 replay_event_history: $mode"
  echo "[v30-b3-dual-infer] exact60k=$CKPT_60K step=60000"
  echo "[v30-b3-dual-infer] best-through-90k=$CKPT_90K_BEST step=$step val=$best"
}

prepare_replay_cache() {
  echo "[v30-b3-dual-infer] preparing/reusing branch replay sidecars"
  "$PY" scripts/build_v30_branch_replay_cache.py \
    --manifest "$BASE_MANIFEST" \
    --out "$V30_CACHE_ROOT" \
    --splits train,validation,development_heldout,deployment_inference \
    --workers "$CACHE_WORKERS" &
  ACTIVE_PID=$!
  wait "$ACTIVE_PID"
  ACTIVE_PID=""

  "$PY" - "$MANIFEST" "$SPLITS" "$CORE_COUNTS" <<'PY'
import json
import os
import sys

manifest_path, split_csv, core_csv = sys.argv[1:]
manifest = json.load(open(manifest_path, "r", encoding="utf-8"))
splits = [value.strip() for value in split_csv.split(",") if value.strip()]
cores = {int(value) for value in core_csv.split(",") if value.strip()}
missing_splits = [name for name in splits if name not in manifest.get("splits", {})]
if missing_splits:
    raise SystemExit(f"manifest is missing splits: {missing_splits}")
selected = [
    item for name in splits for item in manifest["splits"][name]
    if int(item.get("n_cores", -1)) in cores
]
missing = [
    item.get("trace_id", "<unknown>") for item in selected
    if not item.get("branch_replay_dir")
    or not os.path.isfile(os.path.join(item["branch_replay_dir"], "meta.json"))
]
if missing:
    raise SystemExit(
        f"{len(missing)} selected traces lack replay sidecars; first={missing[0]}"
    )
by_split = {
    name: sum(
        int(item.get("n_cores", -1)) in cores
        for item in manifest["splits"][name]
    )
    for name in splits
}
if len(selected) != 120:
    raise SystemExit(f"expected 120 seed1+heldout traces, found {len(selected)}")
print(f"[v30-b3-dual-infer] selected_traces={len(selected)} by_split={by_split}")
PY
}

run_one() {
  local label=$1
  local checkpoint=$2
  local output=$3
  local attempt=0
  mkdir -p "$output"
  while (( attempt <= MAX_RESTARTS )); do
    attempt=$((attempt + 1))
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] label=$label attempt=$attempt/$((MAX_RESTARTS + 1)) checkpoint=$checkpoint"
    set +e
    env \
      ROOT="$ROOT" \
      PY="$PY" \
      CKPT="$checkpoint" \
      MANIFEST="$MANIFEST" \
      OUT="$output" \
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
    ACTIVE_PID=$!
    wait "$ACTIVE_PID"
    status=$?
    ACTIVE_PID=""
    set -e
    if (( status == 0 )); then
      [[ -s "$output/report.json" ]] || \
        fail "$label evaluator exited successfully without report.json"
      [[ -s "$output/report.txt" ]] || \
        fail "$label evaluator exited successfully without report.txt"
      echo "[v30-b3-dual-infer] PASS label=$label report=$output/report.txt"
      return 0
    fi
    echo "[v30-b3-dual-infer][WARN] label=$label exit_status=$status"
    if (( attempt > MAX_RESTARTS )); then
      fail "$label restart budget exhausted"
    fi
    echo "[v30-b3-dual-infer] restarting in 10s; completed traces will resume"
    sleep 10
  done
}

run_controller() {
  mkdir -p "$OUT_ROOT" "$ROOT/scripts/tmp"
  exec 9>"$LOCK_FILE"
  flock -n 9 || fail "another controller holds $LOCK_FILE"
  printf '%s\n' "$$" >"$PID_FILE"
  cleanup_controller() {
    rm -f "$PID_FILE"
  }
  stop_controller() {
    if [[ "$ACTIVE_PID" =~ ^[0-9]+$ ]]; then
      kill -TERM "$ACTIVE_PID" 2>/dev/null || true
      wait "$ACTIVE_PID" 2>/dev/null || true
    fi
    exit 130
  }
  trap cleanup_controller EXIT
  trap stop_controller INT TERM

  validate_static_inputs
  prepare_replay_cache
  run_one exact_60k "$CKPT_60K" "$OUT_60K"
  run_one best_through_90k "$CKPT_90K_BEST" "$OUT_90K_BEST"
  "$PY" scripts/compare_v30_b3_dual_eval.py \
    --exact-60k "$OUT_60K/report.json" \
    --best-through-90k "$OUT_90K_BEST/report.json" \
    --out "$OUT_ROOT"
  echo "[v30-b3-dual-infer] PASS comparison=$OUT_ROOT/comparison.txt"
}

validate_static_inputs

case "${1:-}" in
  --controller)
    run_controller
    ;;
  --check)
    "$PY" - "$BASE_MANIFEST" "$SPLITS" "$CORE_COUNTS" <<'PY'
import json
import sys
manifest_path, split_csv, core_csv = sys.argv[1:]
manifest = json.load(open(manifest_path, "r", encoding="utf-8"))
cores = {int(value) for value in core_csv.split(",") if value.strip()}
counts = {
    name: sum(int(item.get("n_cores", -1)) in cores for item in manifest["splits"][name])
    for name in split_csv.split(",") if name.strip()
}
print(f"[v30-b3-dual-infer] selected_traces={sum(counts.values())} by_split={counts}")
PY
    echo "[v30-b3-dual-infer] mode=free serial stride=$TARGET_STRIDE cores=$CORE_COUNTS gpus=$GPUS"
    echo "[v30-b3-dual-infer] cache_workers=$CACHE_WORKERS"
    echo "[v30-b3-dual-infer] output=$OUT_ROOT"
    echo "[v30-b3-dual-infer] controller_log=$CONTROLLER_LOG"
    ;;
  "")
    mkdir -p "$OUT_ROOT" "$ROOT/scripts/tmp"
    if [[ -f "$PID_FILE" ]]; then
      existing_pid=$(<"$PID_FILE")
      if [[ "$existing_pid" =~ ^[0-9]+$ ]] && kill -0 "$existing_pid" 2>/dev/null; then
        fail "controller is already running: pid=$existing_pid log=$CONTROLLER_LOG"
      fi
    fi
    nohup env \
      ROOT="$ROOT" PY="$PY" BASE_MANIFEST="$BASE_MANIFEST" \
      V30_CACHE_ROOT="$V30_CACHE_ROOT" MANIFEST="$MANIFEST" \
      CKPT_60K="$CKPT_60K" CKPT_90K_BEST="$CKPT_90K_BEST" \
      GPUS="$GPUS" SPLITS="$SPLITS" CORE_COUNTS="$CORE_COUNTS" \
      TARGET_STRIDE="$TARGET_STRIDE" MAX_STEP_CYCLES="$MAX_STEP_CYCLES" \
      CACHE_WORKERS="$CACHE_WORKERS" MAX_RESTARTS="$MAX_RESTARTS" \
      RUN_TAG="$RUN_TAG" OUT="$OUT_ROOT" OUT_60K="$OUT_60K" \
      OUT_90K_BEST="$OUT_90K_BEST" CONTROLLER_LOG="$CONTROLLER_LOG" \
      PID_FILE="$PID_FILE" LOCK_FILE="$LOCK_FILE" \
      bash "$0" --controller >"$CONTROLLER_LOG" 2>&1 &
    controller_pid=$!
    printf '%s\n' "$controller_pid" >"$PID_FILE"
    echo "[v30-b3-dual-infer] started controller_pid=$controller_pid"
    echo "[v30-b3-dual-infer] monitor: tail -f $CONTROLLER_LOG"
    echo "[v30-b3-dual-infer] exact60k report: $OUT_60K/report.txt"
    echo "[v30-b3-dual-infer] best-through-90k report: $OUT_90K_BEST/report.txt"
    echo "[v30-b3-dual-infer] comparison: $OUT_ROOT/comparison.txt"
    ;;
  *)
    echo "usage: $0 [--check|--controller]" >&2
    exit 2
    ;;
esac
