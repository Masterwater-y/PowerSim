#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT=${ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
cd "$PROJECT_ROOT"

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
CKPT=${CKPT:-$PROJECT_ROOT/ckpt/tcsim_v30_gss_joint_v2_60k_seed1234/last.pt}
MANIFEST=${MANIFEST:-$PROJECT_ROOT/data/v30_gss_ready_dataset/manifest.json}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
SPLITS=${SPLITS:-development_heldout,deployment_inference}
CORE_COUNTS=${CORE_COUNTS:-4,8,16,32}
TARGET_STRIDE=${TARGET_STRIDE:-256}
MAX_STEP_CYCLES=${MAX_STEP_CYCLES:-1024}
MAX_RESTARTS=${MAX_RESTARTS:-3}
GSS_BACKEND=${GSS_BACKEND:-native}
RUN_TAG=${RUN_TAG:-v30_gss_step38000_readycompat_commitdeploy_seed1_heldout_c04_c32_serial_s256_8gpu}
OUT_DIR=${OUT:-$PROJECT_ROOT/logs/$RUN_TAG}
WATCHDOG_LOG=${WATCHDOG_LOG:-$OUT_DIR/watchdog.log}
PID_FILE=${PID_FILE:-$PROJECT_ROOT/scripts/tmp/$RUN_TAG.watchdog.pid}
LOCK_FILE=${LOCK_FILE:-$PROJECT_ROOT/scripts/tmp/$RUN_TAG.watchdog.lock}

fail() {
  echo "[v30-gss-infer][ERROR] $*" >&2
  exit 2
}

[[ -x "$PY" ]] || fail "missing Python: $PY"
[[ -f "$CKPT" ]] || fail "missing checkpoint: $CKPT"
[[ -f "$MANIFEST" ]] || fail "missing manifest: $MANIFEST"
if ! "$PY" -c 'import os; import tcsim.v30._gss_native as m; raise SystemExit(os.path.getmtime(m.__file__) < os.path.getmtime("tcsim/v30/native_gss.cpp"))' >/dev/null 2>&1; then
  echo "[v30-gss-infer] building native GSS hot path"
  "$PY" scripts/build_v30_gss_native.py
fi

print_configuration() {
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
step = int(checkpoint.get("step") or 0)
model_config = checkpoint.get("config", {}).get("model", {})
gss_contract = checkpoint.get("contract", {}).get("gss")
if model_config.get("gss_mode") != "causal_adapter":
    raise SystemExit(f"checkpoint is not causal GSS: {model_config.get('gss_mode')}")
if not model_config.get("gss_strength_router"):
    raise SystemExit("checkpoint has no GSS strength router")
if not isinstance(gss_contract, dict):
    raise SystemExit("checkpoint has no GSS feature contract")
if gss_contract.get("clock_source") != "ready":
    raise SystemExit(
        f"compat launcher expects ready clock, got {gss_contract.get('clock_source')}"
    )
if gss_contract.get("order_policy") != "ready_tick_then_core_then_uop_v1":
    raise SystemExit(
        f"unexpected GSS order policy: {gss_contract.get('order_policy')}"
    )

with open(manifest_path, "r", encoding="utf-8") as handle:
    manifest = json.load(handle)
splits = [value.strip() for value in split_csv.split(",") if value.strip()]
cores = {int(value) for value in core_csv.split(",") if value.strip()}
missing_splits = [name for name in splits if name not in manifest.get("splits", {})]
if missing_splits:
    raise SystemExit(f"manifest is missing splits: {missing_splits}")
selected = [
    item for name in splits for item in manifest["splits"][name]
    if int(item.get("n_cores", -1)) in cores
]
manifest_base = os.path.dirname(os.path.abspath(manifest_path))
missing_cache = [
    item.get("trace_id", "<unknown>") for item in selected
    if not item.get("cache_dir")
    or not os.path.isfile(os.path.join(
        item["cache_dir"]
        if os.path.isabs(item["cache_dir"])
        else os.path.join(manifest_base, item["cache_dir"]),
        "meta.json",
    ))
]
if missing_cache:
    raise SystemExit(
        f"{len(missing_cache)} selected traces lack base cache; first={missing_cache[0]}"
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
print(f"[v30-gss-infer] checkpoint_step={step}")
print(f"[v30-gss-infer] selected_traces={len(selected)} by_split={by_split}")
print("[v30-gss-infer] GSS source=online canonical/shadow; teacher sidecars ignored")
print("[v30-gss-infer] clock compatibility=ready-trained/commit-deployment; accuracy=approximate")
PY
  echo "[v30-gss-infer] checkpoint=$CKPT"
  echo "[v30-gss-infer] manifest=$MANIFEST"
  echo "[v30-gss-infer] gpus=$GPUS mode=free serial stride=$TARGET_STRIDE cores=$CORE_COUNTS"
  echo "[v30-gss-infer] gss_backend=$GSS_BACKEND packed_attention=on batched_anchors=on"
  echo "[v30-gss-infer] out=$OUT_DIR"
  echo "[v30-gss-infer] log=$WATCHDOG_LOG"
}

run_watchdog() {
  mkdir -p "$OUT_DIR" "$PROJECT_ROOT/scripts/tmp"
  exec 9>"$LOCK_FILE"
  flock -n 9 || fail "watchdog lock is already held: $LOCK_FILE"
  printf '%s\n' "$$" >"$PID_FILE"

  eval_pid=""
  cleanup_pid() {
    rm -f "$PID_FILE"
  }
  stop_children() {
    if [[ "$eval_pid" =~ ^[0-9]+$ ]]; then
      kill -TERM "$eval_pid" 2>/dev/null || true
      wait "$eval_pid" 2>/dev/null || true
    fi
    exit 130
  }
  trap cleanup_pid EXIT
  trap stop_children INT TERM

  local restart=0
  while (( restart <= MAX_RESTARTS )); do
    printf '\n[%s] watchdog_attempt=%d/%d\n' \
      "$(date '+%Y-%m-%d %H:%M:%S')" "$((restart + 1))" "$((MAX_RESTARTS + 1))"
    set +e
    env \
      TCSIM_GSS_BACKEND="$GSS_BACKEND" \
      ROOT="$PROJECT_ROOT" PY="$PY" CKPT="$CKPT" MANIFEST="$MANIFEST" \
      OUT="$OUT_DIR" GPUS="$GPUS" SPLITS="$SPLITS" MODE=free \
      WINDOW_PARALLEL_MODE=serial WINDOW_PARALLEL_DEVICES= \
      WINDOW_CONTEXT_BACKEND=process CORE_COUNTS="$CORE_COUNTS" WORKLOADS= \
      MAX_ORACLE_SAMPLES=0 MAX_FREE_STEPS=0 TARGET_STRIDE="$TARGET_STRIDE" \
      MIN_STEP_CYCLES=4 MAX_STEP_CYCLES="$MAX_STEP_CYCLES" \
      MAX_NO_PROGRESS_STEPS=64 AMP_DTYPE=bf16 SDPA_BACKEND=auto \
      MAX_CORE_STALL_STEPS=256 ALLOW_READY_CLOCK_GSS_COMPAT=1 \
      PROGRESS_EVERY=100 ORACLE_DRIFT_DIAGNOSTICS=0 RESUME=1 \
      bash scripts/run_v29_eval_8gpu.sh &
    eval_pid=$!
    wait "$eval_pid"
    status=$?
    set -e
    eval_pid=""
    if (( status == 0 )); then
      [[ -s "$OUT_DIR/report.json" ]] || fail "successful evaluator lacks report.json"
      [[ -s "$OUT_DIR/report.txt" ]] || fail "successful evaluator lacks report.txt"
      echo "[v30-gss-infer] PASS report=$OUT_DIR/report.txt"
      return 0
    fi
    echo "[v30-gss-infer][WARN] evaluator exit_status=$status"
    restart=$((restart + 1))
    (( restart <= MAX_RESTARTS )) || fail "restart budget exhausted"
    echo "[v30-gss-infer] restarting in 10s; completed traces will resume"
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
      && grep -aFq "launch_v30_gss_joint_v2_step38000_full_inference.sh" \
        "/proc/$existing_pid/cmdline"; then
    fail "evaluation watchdog already running: pid=$existing_pid log=$WATCHDOG_LOG"
  fi
fi

mkdir -p "$OUT_DIR" "$PROJECT_ROOT/scripts/tmp"
print_configuration
nohup env \
  TCSIM_GSS_BACKEND="$GSS_BACKEND" \
  ROOT="$PROJECT_ROOT" PY="$PY" CKPT="$CKPT" MANIFEST="$MANIFEST" \
  GPUS="$GPUS" SPLITS="$SPLITS" CORE_COUNTS="$CORE_COUNTS" \
  TARGET_STRIDE="$TARGET_STRIDE" MAX_STEP_CYCLES="$MAX_STEP_CYCLES" \
  MAX_RESTARTS="$MAX_RESTARTS" RUN_TAG="$RUN_TAG" OUT="$OUT_DIR" \
  GSS_BACKEND="$GSS_BACKEND" \
  WATCHDOG_LOG="$WATCHDOG_LOG" PID_FILE="$PID_FILE" LOCK_FILE="$LOCK_FILE" \
  bash "$0" --watchdog >>"$WATCHDOG_LOG" 2>&1 &
watchdog_pid=$!
printf '%s\n' "$watchdog_pid" >"$PID_FILE"

echo "[v30-gss-infer] started watchdog_pid=$watchdog_pid"
echo "[v30-gss-infer] monitor: tail -f $WATCHDOG_LOG"
echo "[v30-gss-infer] final report: $OUT_DIR/report.txt"
