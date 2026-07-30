#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT=${ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
cd "$PROJECT_ROOT"

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
CKPT=${CKPT:-$PROJECT_ROOT/ckpt/tcsim_v30_gss_joint_v2_60k_seed1234/last.pt}
MANIFEST=${MANIFEST:-$PROJECT_ROOT/data/v30_gss_ready_dataset/manifest.json}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
SPLITS=${SPLITS:-deployment_inference}
CORE_COUNTS=${CORE_COUNTS:-16,32}
SEEDS=${SEEDS:-1}
WORKLOADS=${WORKLOADS:-W_v28_redis_heldout,W_v28_bvc_encoder_heldout,W_v28_pytorch_heldout}
TARGET_STRIDE=${TARGET_STRIDE:-256}
MAX_RESTARTS=${MAX_RESTARTS:-3}
GSS_BACKEND=${GSS_BACKEND:-native}
RUN_TAG=${RUN_TAG:-v30_gss_step38000_sameckpt_targeted_seed1_c16_c32_ablation}
OUT_ROOT=${OUT_ROOT:-$PROJECT_ROOT/logs/$RUN_TAG}
WATCHDOG_LOG=${WATCHDOG_LOG:-$OUT_ROOT/watchdog.log}
PID_FILE=${PID_FILE:-$PROJECT_ROOT/scripts/tmp/$RUN_TAG.watchdog.pid}
LOCK_FILE=${LOCK_FILE:-$PROJECT_ROOT/scripts/tmp/$RUN_TAG.watchdog.lock}

MODES=(gap0 state-disabled predicted-order teacher-order)

fail() {
  echo "[v30-gss-ablation][ERROR] $*" >&2
  exit 2
}

[[ -x "$PY" ]] || fail "missing Python: $PY"
[[ -f "$CKPT" ]] || fail "missing checkpoint: $CKPT"
[[ -f "$MANIFEST" ]] || fail "missing manifest: $MANIFEST"

IFS=',' read -r -a GPU_ARRAY <<<"$GPUS"
(( ${#GPU_ARRAY[@]} == 8 )) || fail "GPUS must contain exactly 8 devices"
for gpu in "${GPU_ARRAY[@]}"; do
  [[ "$gpu" =~ ^[0-9]+$ ]] || fail "GPU entries must be integer IDs: $gpu"
done

if ! "$PY" -c 'import os; import tcsim.v30._gss_native as m; raise SystemExit(os.path.getmtime(m.__file__) < os.path.getmtime("tcsim/v30/native_gss.cpp"))' >/dev/null 2>&1; then
  echo "[v30-gss-ablation] building native GSS hot path"
  "$PY" scripts/build_v30_gss_native.py
fi

print_configuration() {
  "$PY" - "$CKPT" "$MANIFEST" "$SPLITS" "$CORE_COUNTS" "$SEEDS" "$WORKLOADS" <<'PY'
import json
import sys
import torch

checkpoint_path, manifest_path, split_csv, core_csv, seed_csv, workload_csv = sys.argv[1:]
try:
    checkpoint = torch.load(checkpoint_path, map_location="cpu", weights_only=False)
except TypeError:
    checkpoint = torch.load(checkpoint_path, map_location="cpu")
step = int(checkpoint.get("step") or 0)
model = checkpoint.get("config", {}).get("model", {})
gss = checkpoint.get("contract", {}).get("gss")
if model.get("gss_mode") != "causal_adapter":
    raise SystemExit(f"checkpoint is not causal GSS: {model.get('gss_mode')}")
if not model.get("gss_strength_router"):
    raise SystemExit("checkpoint lacks the three-anchor GSS router")
if not isinstance(gss, dict) or gss.get("clock_source") != "ready":
    raise SystemExit(f"expected ready-clock GSS checkpoint, got {gss}")

with open(manifest_path, "r", encoding="utf-8") as handle:
    manifest = json.load(handle)
splits = [value for value in split_csv.split(",") if value]
cores = {int(value) for value in core_csv.split(",") if value}
seeds = {int(value) for value in seed_csv.split(",") if value}
workloads = {value for value in workload_csv.split(",") if value}
selected = [
    row for split in splits for row in manifest.get("splits", {}).get(split, [])
    if int(row.get("n_cores", -1)) in cores
    and int(row.get("seed", -1)) in seeds
    and str(row.get("workload", "")) in workloads
]
if len(selected) != 6:
    raise SystemExit(f"expected exactly 6 targeted traces, found {len(selected)}")
missing = [row.get("trace_id") for row in selected if not row.get("gss_sidecar_dir")]
if missing:
    raise SystemExit(f"teacher-order sidecar missing for {missing}")
print(f"[v30-gss-ablation] checkpoint_step={step}")
print(f"[v30-gss-ablation] selected_traces={len(selected)}")
for row in sorted(selected, key=lambda value: (value["workload"], value["n_cores"])):
    print(f"  {row['workload']} C{row['n_cores']} seed={row['seed']}")
PY
  echo "[v30-gss-ablation] modes=${MODES[*]}"
  echo "[v30-gss-ablation] mode GPUs: gap0=${GPU_ARRAY[0]},${GPU_ARRAY[1]} state-disabled=${GPU_ARRAY[2]},${GPU_ARRAY[3]} predicted-order=${GPU_ARRAY[4]},${GPU_ARRAY[5]} teacher-order=${GPU_ARRAY[6]},${GPU_ARRAY[7]}"
  echo "[v30-gss-ablation] output=$OUT_ROOT"
}

mode_gpus() {
  case "$1" in
    gap0) echo "${GPU_ARRAY[0]},${GPU_ARRAY[1]}" ;;
    state-disabled) echo "${GPU_ARRAY[2]},${GPU_ARRAY[3]}" ;;
    predicted-order) echo "${GPU_ARRAY[4]},${GPU_ARRAY[5]}" ;;
    teacher-order) echo "${GPU_ARRAY[6]},${GPU_ARRAY[7]}" ;;
    *) fail "unknown mode: $1" ;;
  esac
}

merge_ablation() {
  "$PY" scripts/compare_v30_gss_same_ckpt_ablation.py \
    --gap0 "$OUT_ROOT/gap0/report.json" \
    --state-disabled "$OUT_ROOT/state-disabled/report.json" \
    --predicted-order "$OUT_ROOT/predicted-order/report.json" \
    --teacher-order "$OUT_ROOT/teacher-order/report.json" \
    --out-json "$OUT_ROOT/ablation_summary.json" \
    --out-md "$OUT_ROOT/ablation_summary.md"
}

run_attempt() {
  local pids=()
  local mode
  for mode in "${MODES[@]}"; do
    local mode_out="$OUT_ROOT/$mode"
    local assigned_gpus
    assigned_gpus=$(mode_gpus "$mode")
    mkdir -p "$mode_out"
    echo "[v30-gss-ablation] launch mode=$mode gpus=$assigned_gpus"
    env \
      TCSIM_GSS_BACKEND="$GSS_BACKEND" \
      ROOT="$PROJECT_ROOT" PY="$PY" CKPT="$CKPT" MANIFEST="$MANIFEST" \
      OUT="$mode_out" GPUS="$assigned_gpus" SPLITS="$SPLITS" MODE=free \
      WINDOW_PARALLEL_MODE=serial WINDOW_PARALLEL_DEVICES= \
      WINDOW_CONTEXT_BACKEND=process CORE_COUNTS="$CORE_COUNTS" \
      WORKLOADS="$WORKLOADS" SEEDS="$SEEDS" \
      MAX_ORACLE_SAMPLES=0 MAX_FREE_STEPS=0 TARGET_STRIDE="$TARGET_STRIDE" \
      MIN_STEP_CYCLES=4 MAX_STEP_CYCLES=1024 MAX_NO_PROGRESS_STEPS=64 \
      MAX_CORE_STALL_STEPS=256 AMP_DTYPE=bf16 SDPA_BACKEND=auto \
      ALLOW_READY_CLOCK_GSS_COMPAT=1 GSS_ABLATION_MODE="$mode" \
      PROGRESS_EVERY=500 ORACLE_DRIFT_DIAGNOSTICS=0 RESUME=1 \
      bash scripts/run_v29_eval_8gpu.sh >"$mode_out/launch.log" 2>&1 &
    pids+=("$!")
  done

  local failed=0
  local index
  for index in "${!pids[@]}"; do
    if wait "${pids[$index]}"; then
      echo "[v30-gss-ablation] complete mode=${MODES[$index]}"
    else
      echo "[v30-gss-ablation][WARN] failed mode=${MODES[$index]}" >&2
      failed=1
    fi
  done
  (( failed == 0 )) || return 2
  for mode in "${MODES[@]}"; do
    [[ -s "$OUT_ROOT/$mode/report.json" ]] \
      || fail "mode lacks report.json: $mode"
  done
  merge_ablation
}

run_watchdog() {
  mkdir -p "$OUT_ROOT" "$PROJECT_ROOT/scripts/tmp"
  exec 9>"$LOCK_FILE"
  flock -n 9 || fail "watchdog lock is already held: $LOCK_FILE"
  printf '%s\n' "$$" >"$PID_FILE"
  trap 'rm -f "$PID_FILE"' EXIT
  trap 'jobs -pr | xargs -r kill -TERM; exit 130' INT TERM

  local attempt=0
  while (( attempt <= MAX_RESTARTS )); do
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] watchdog attempt=$((attempt + 1))/$((MAX_RESTARTS + 1))"
    if run_attempt; then
      echo "[v30-gss-ablation] PASS summary=$OUT_ROOT/ablation_summary.md"
      return 0
    fi
    attempt=$((attempt + 1))
    (( attempt <= MAX_RESTARTS )) || fail "restart budget exhausted"
    echo "[v30-gss-ablation] retrying in 10 seconds; completed traces resume"
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
      && grep -aFq "launch_v30_gss_same_ckpt_targeted_ablation.sh" \
        "/proc/$existing_pid/cmdline"; then
    fail "watchdog already running: pid=$existing_pid"
  fi
fi

mkdir -p "$OUT_ROOT" "$PROJECT_ROOT/scripts/tmp"
print_configuration
nohup env \
  ROOT="$PROJECT_ROOT" PY="$PY" CKPT="$CKPT" MANIFEST="$MANIFEST" \
  GPUS="$GPUS" SPLITS="$SPLITS" CORE_COUNTS="$CORE_COUNTS" \
  SEEDS="$SEEDS" WORKLOADS="$WORKLOADS" TARGET_STRIDE="$TARGET_STRIDE" \
  MAX_RESTARTS="$MAX_RESTARTS" GSS_BACKEND="$GSS_BACKEND" \
  RUN_TAG="$RUN_TAG" OUT_ROOT="$OUT_ROOT" WATCHDOG_LOG="$WATCHDOG_LOG" \
  PID_FILE="$PID_FILE" LOCK_FILE="$LOCK_FILE" \
  bash "$0" --watchdog >>"$WATCHDOG_LOG" 2>&1 &
watchdog_pid=$!
printf '%s\n' "$watchdog_pid" >"$PID_FILE"

echo "[v30-gss-ablation] started watchdog_pid=$watchdog_pid"
echo "[v30-gss-ablation] monitor: tail -f $WATCHDOG_LOG"
echo "[v30-gss-ablation] summary: $OUT_ROOT/ablation_summary.md"
