#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
cd "$ROOT"

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
CKPT=${CKPT:-ckpt/tcsim_v30_gss_exposure_v1_60k_seed1234/best.pt}
BASE_MANIFEST=${BASE_MANIFEST:-data/v30_exposure_v1_dataset/manifest.json}
EXPOSURE_ROOT=${EXPOSURE_ROOT:-data/v30_exposure_v1_sidecars}
MANIFEST=${MANIFEST:-data/v30_exposure_v1_inference_dataset/manifest.json}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
SPLITS=${SPLITS:-development_heldout,deployment_inference}
CORE_COUNTS=${CORE_COUNTS:-32,16,8,4}
TARGET_STRIDE=${TARGET_STRIDE:-256}
MAX_STEP_CYCLES=${MAX_STEP_CYCLES:-1024}
MAX_CORE_STALL_STEPS=${MAX_CORE_STALL_STEPS:-256}
CACHE_WORKERS=${CACHE_WORKERS:-128}
MAX_RESTARTS=${MAX_RESTARTS:-3}
GSS_BACKEND=${GSS_BACKEND:-native}
RUN_TAG=${RUN_TAG:-v30_gss_exposure_v1_best60k_seed1_heldout_c32_c04_serial_s256_8gpu}
OUT_DIR=${OUT:-$ROOT/logs/$RUN_TAG}
CONTROLLER_LOG=${CONTROLLER_LOG:-$OUT_DIR/controller.log}
PID_FILE=${PID_FILE:-$ROOT/scripts/tmp/$RUN_TAG.controller.pid}
LOCK_FILE=${LOCK_FILE:-$ROOT/scripts/tmp/$RUN_TAG.controller.lock}
ACTIVE_PID=""

fail() {
  echo "[v30-exposure-infer][ERROR] $*" >&2
  exit 2
}

validate_static_inputs() {
  [[ -x "$PY" ]] || fail "missing Python: $PY"
  [[ -f "$CKPT" ]] || fail "missing checkpoint: $CKPT"
  [[ -f "$BASE_MANIFEST" ]] || fail "missing base manifest: $BASE_MANIFEST"
  [[ -f scripts/build_v30_exposure_sidecar.py ]] || \
    fail "missing Exposure-v1 cache builder"
  [[ -f scripts/run_v29_eval_8gpu.sh ]] || fail "missing 8-GPU evaluator"
  [[ -f scripts/build_v30_gss_native.py ]] || fail "missing native GSS builder"

  "$PY" - "$CKPT" "$BASE_MANIFEST" "$SPLITS" "$CORE_COUNTS" <<'PY'
import json
import sys
import torch

checkpoint_path, manifest_path, split_csv, core_csv = sys.argv[1:]
try:
    checkpoint = torch.load(checkpoint_path, map_location="cpu", weights_only=False)
except TypeError:
    checkpoint = torch.load(checkpoint_path, map_location="cpu")

step = int(checkpoint.get("step") or 0)
model = checkpoint.get("config", {}).get("model", {})
contract = checkpoint.get("contract", {})
gss = contract.get("gss")
exposure = contract.get("exposure")
if step != 60000:
    raise SystemExit(f"expected exact step-60000 checkpoint, found step={step}")
if model.get("gss_mode") != "causal_adapter":
    raise SystemExit(f"checkpoint is not causal-adapter GSS: {model.get('gss_mode')}")
if not model.get("gss_strength_router"):
    raise SystemExit("checkpoint has no GSS strength router")
if not model.get("gss_exposure_features"):
    raise SystemExit("checkpoint does not consume Exposure-v1 features")
if not isinstance(gss, dict):
    raise SystemExit("checkpoint has no GSS contract")
if gss.get("clock_source") != "commit":
    raise SystemExit(f"expected commit-clock GSS, found {gss.get('clock_source')}")
if gss.get("order_policy") != "commit_tick_then_core_then_uop_v1":
    raise SystemExit(f"unexpected GSS order policy: {gss.get('order_policy')}")
if not isinstance(exposure, dict):
    raise SystemExit("checkpoint has no Exposure-v1 contract")
if exposure.get("feature_schema") != "tcsim-v30-exposure-functional-1":
    raise SystemExit(f"unexpected exposure schema: {exposure.get('feature_schema')}")
if int(exposure.get("max_lookahead", 0)) != 256:
    raise SystemExit(f"unexpected exposure lookahead: {exposure.get('max_lookahead')}")
if exposure.get("uses_timing_or_microarchitecture_oracle") is not False:
    raise SystemExit("Exposure-v1 contract is not functional-only")

with open(manifest_path, "r", encoding="utf-8") as handle:
    manifest = json.load(handle)
splits = [value.strip() for value in split_csv.split(",") if value.strip()]
cores = {int(value) for value in core_csv.split(",") if value.strip()}
missing_splits = [name for name in splits if name not in manifest.get("splits", {})]
if missing_splits:
    raise SystemExit(f"base manifest is missing splits: {missing_splits}")
counts = {
    name: sum(
        int(item.get("n_cores", -1)) in cores
        for item in manifest["splits"][name]
    )
    for name in splits
}
selected = sum(counts.values())
if selected != 120:
    raise SystemExit(f"expected 120 seed1+heldout C32--C4 traces, found {selected}: {counts}")
print(
    f"[v30-exposure-infer] checkpoint_step={step} "
    f"best_validation={checkpoint.get('best_validation')}"
)
print(f"[v30-exposure-infer] selected_traces={selected} by_split={counts}")
print("[v30-exposure-infer] GSS clock=commit order=commit/core/uop exact=true")
print("[v30-exposure-infer] Exposure-v1=functional-only lookahead=256")
PY
}

prepare_native_gss() {
  if ! "$PY" -c \
    'import os; import tcsim.v30._gss_native as m; raise SystemExit(os.path.getmtime(m.__file__) < os.path.getmtime("tcsim/v30/native_gss.cpp"))' \
    >/dev/null 2>&1; then
    echo "[v30-exposure-infer] building native GSS hot path"
    "$PY" scripts/build_v30_gss_native.py
  fi
}

prepare_exposure_cache() {
  echo "[v30-exposure-infer] preparing/reusing Exposure-v1 sidecars workers=$CACHE_WORKERS"
  mkdir -p "$(dirname "$MANIFEST")" "$EXPOSURE_ROOT"
  "$PY" scripts/build_v30_exposure_sidecar.py \
    --manifest "$BASE_MANIFEST" \
    --splits "$SPLITS" \
    --core-counts "$CORE_COUNTS" \
    --workers "$CACHE_WORKERS" \
    --out-root "$EXPOSURE_ROOT" \
    --write-manifest "$MANIFEST" &
  ACTIVE_PID=$!
  wait "$ACTIVE_PID"
  ACTIVE_PID=""

  "$PY" - "$CKPT" "$MANIFEST" "$SPLITS" "$CORE_COUNTS" <<'PY'
import json
import os
import sys
import torch

checkpoint_path, manifest_path, split_csv, core_csv = sys.argv[1:]
try:
    checkpoint = torch.load(checkpoint_path, map_location="cpu", weights_only=False)
except TypeError:
    checkpoint = torch.load(checkpoint_path, map_location="cpu")
expected = checkpoint["contract"]["exposure"]
with open(manifest_path, "r", encoding="utf-8") as handle:
    manifest = json.load(handle)
splits = [value.strip() for value in split_csv.split(",") if value.strip()]
cores = {int(value) for value in core_csv.split(",") if value.strip()}
selected = [
    item
    for name in splits
    for item in manifest["splits"][name]
    if int(item.get("n_cores", -1)) in cores
]
if len(selected) != 120:
    raise SystemExit(f"expected 120 C32--C4 evaluation traces, found {len(selected)}")
required = {
    "schema_version": expected["schema_version"],
    "feature_schema": expected["feature_schema"],
    "max_lookahead": expected["max_lookahead"],
    "uses_timing_or_microarchitecture_oracle": False,
}
missing = []
mismatch = []
for item in selected:
    sidecar = item.get("exposure_sidecar_dir")
    if not sidecar:
        missing.append(str(item.get("trace_id")))
        continue
    path = sidecar if os.path.isabs(sidecar) else os.path.join(
        os.path.dirname(os.path.abspath(manifest_path)), sidecar,
    )
    meta_path = os.path.join(path, "meta.json")
    if not os.path.isfile(meta_path):
        missing.append(str(item.get("trace_id")))
        continue
    with open(meta_path, "r", encoding="utf-8") as handle:
        meta = json.load(handle)
    if any(meta.get(key) != value for key, value in required.items()):
        mismatch.append(str(item.get("trace_id")))
if missing:
    raise SystemExit(
        f"{len(missing)} evaluation traces lack Exposure-v1 sidecars; first={missing[0]}"
    )
if mismatch:
    raise SystemExit(
        f"{len(mismatch)} evaluation sidecars violate the checkpoint contract; "
        f"first={mismatch[0]}"
    )
print(f"[v30-exposure-infer] exposure_sidecars=120/120 manifest={manifest_path}")
PY
}

run_inference_with_watchdog() {
  local attempt=0
  while (( attempt <= MAX_RESTARTS )); do
    attempt=$((attempt + 1))
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] inference_attempt=$attempt/$((MAX_RESTARTS + 1))"
    set +e
    env \
      TCSIM_GSS_BACKEND="$GSS_BACKEND" \
      ROOT="$ROOT" PY="$PY" CKPT="$CKPT" MANIFEST="$MANIFEST" \
      OUT="$OUT_DIR" GPUS="$GPUS" SPLITS="$SPLITS" MODE=free \
      WINDOW_PARALLEL_MODE=serial WINDOW_PARALLEL_DEVICES= \
      WINDOW_CONTEXT_BACKEND=process CORE_COUNTS="$CORE_COUNTS" WORKLOADS= \
      MAX_ORACLE_SAMPLES=0 MAX_FREE_STEPS=0 TARGET_STRIDE="$TARGET_STRIDE" \
      MIN_STEP_CYCLES=4 MAX_STEP_CYCLES="$MAX_STEP_CYCLES" \
      MAX_NO_PROGRESS_STEPS=64 MAX_CORE_STALL_STEPS="$MAX_CORE_STALL_STEPS" \
      AMP_DTYPE=bf16 SDPA_BACKEND=auto PROGRESS_EVERY=100 \
      ORACLE_DRIFT_DIAGNOSTICS=0 ALLOW_READY_CLOCK_GSS_COMPAT=0 RESUME=1 \
      bash scripts/run_v29_eval_8gpu.sh &
    ACTIVE_PID=$!
    wait "$ACTIVE_PID"
    status=$?
    ACTIVE_PID=""
    set -e
    if (( status == 0 )); then
      [[ -s "$OUT_DIR/report.json" ]] || \
        fail "evaluator exited successfully without report.json"
      [[ -s "$OUT_DIR/report.txt" ]] || \
        fail "evaluator exited successfully without report.txt"
      echo "[v30-exposure-infer] PASS report=$OUT_DIR/report.txt"
      echo "[v30-exposure-infer] cache-miss PMU audit is included in the report"
      return 0
    fi
    echo "[v30-exposure-infer][WARN] evaluator exit_status=$status"
    if (( attempt > MAX_RESTARTS )); then
      fail "inference restart budget exhausted"
    fi
    echo "[v30-exposure-infer] restarting in 10s; completed traces will resume"
    sleep 10
  done
}

run_controller() {
  mkdir -p "$OUT_DIR" "$ROOT/scripts/tmp"
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
  prepare_native_gss
  prepare_exposure_cache
  run_inference_with_watchdog
}

print_configuration() {
  validate_static_inputs
  echo "[v30-exposure-infer] checkpoint=$CKPT"
  echo "[v30-exposure-infer] base_manifest=$BASE_MANIFEST"
  echo "[v30-exposure-infer] inference_manifest=$MANIFEST"
  echo "[v30-exposure-infer] cache_workers=$CACHE_WORKERS"
  echo "[v30-exposure-infer] gpus=$GPUS mode=free window_parallel=serial"
  echo "[v30-exposure-infer] splits=$SPLITS cores=$CORE_COUNTS stride=$TARGET_STRIDE"
  echo "[v30-exposure-infer] GSS backend=$GSS_BACKEND ready-clock-compat=off"
  echo "[v30-exposure-infer] output=$OUT_DIR"
  echo "[v30-exposure-infer] controller_log=$CONTROLLER_LOG"
  echo "[v30-exposure-infer] final_report=$OUT_DIR/report.txt"
}

case "${1:-}" in
  --check)
    print_configuration
    exit 0
    ;;
  --controller)
    run_controller
    exit $?
    ;;
  "") ;;
  *)
    echo "usage: $0 [--check|--controller]" >&2
    exit 2
    ;;
esac

validate_static_inputs
if [[ -f "$PID_FILE" ]]; then
  existing_pid=$(<"$PID_FILE")
  if [[ "$existing_pid" =~ ^[0-9]+$ ]] \
      && kill -0 "$existing_pid" 2>/dev/null \
      && [[ -r "/proc/$existing_pid/cmdline" ]] \
      && grep -aFq "launch_v30_gss_exposure_v1_60k_full_inference.sh" \
        "/proc/$existing_pid/cmdline"; then
    fail "controller is already running: pid=$existing_pid log=$CONTROLLER_LOG"
  fi
fi

mkdir -p "$OUT_DIR" "$ROOT/scripts/tmp"
nohup env \
  ROOT="$ROOT" PY="$PY" CKPT="$CKPT" BASE_MANIFEST="$BASE_MANIFEST" \
  EXPOSURE_ROOT="$EXPOSURE_ROOT" MANIFEST="$MANIFEST" GPUS="$GPUS" \
  SPLITS="$SPLITS" CORE_COUNTS="$CORE_COUNTS" \
  TARGET_STRIDE="$TARGET_STRIDE" MAX_STEP_CYCLES="$MAX_STEP_CYCLES" \
  MAX_CORE_STALL_STEPS="$MAX_CORE_STALL_STEPS" CACHE_WORKERS="$CACHE_WORKERS" \
  MAX_RESTARTS="$MAX_RESTARTS" GSS_BACKEND="$GSS_BACKEND" \
  RUN_TAG="$RUN_TAG" OUT="$OUT_DIR" CONTROLLER_LOG="$CONTROLLER_LOG" \
  PID_FILE="$PID_FILE" LOCK_FILE="$LOCK_FILE" \
  bash "$0" --controller >>"$CONTROLLER_LOG" 2>&1 &
controller_pid=$!
printf '%s\n' "$controller_pid" >"$PID_FILE"

echo "[v30-exposure-infer] started controller_pid=$controller_pid"
echo "[v30-exposure-infer] monitor: tail -f $CONTROLLER_LOG"
echo "[v30-exposure-infer] final report: $OUT_DIR/report.txt"
