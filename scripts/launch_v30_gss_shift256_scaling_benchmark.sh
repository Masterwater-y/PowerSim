#!/usr/bin/env bash
set -euo pipefail

# Throughput scaling benchmark for the current deployed model contract:
# canonical v29 Full-QKVR backbone + online commit-clock GSS + Exposure-v1.
#
# The three modes consume exactly the same four seed1 BVC traces (one each for
# C4/C8/C16/C32).  Unconditional modes use non-overlapping 256-UOP lanes so
# depth=N has an N-times geometric lookahead ceiling.

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
cd "$ROOT"

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
CKPT=${CKPT:-$ROOT/ckpt/tcsim_v30_gss_exposure_v1_60k_seed1234/best.pt}
MANIFEST=${MANIFEST:-$ROOT/data/v30_exposure_v1_inference_dataset/manifest.json}
SPLITS=${SPLITS:-deployment_inference}
WORKLOADS=${WORKLOADS:-W_v28_bvc_encoder_base}
SEEDS=${SEEDS:-1}
CORE_COUNTS=${CORE_COUNTS:-4,8,16,32}
TARGET_STRIDE=${TARGET_STRIDE:-256}
WINDOW_SHIFT=${WINDOW_SHIFT:-256}
SERIAL_GPU=${SERIAL_GPU:-0}
GPUS4=${GPUS4:-0,1,2,3}
GPUS8=${GPUS8:-0,1,2,3,4,5,6,7}
MAX_RESTARTS=${MAX_RESTARTS:-2}
RUN_TAG=${RUN_TAG:-v30_gss_exposure_v1_bvc_seed1_shift256_scaling}
OUT_ROOT=${OUT:-$ROOT/logs/$RUN_TAG}
CONTROLLER_LOG=${CONTROLLER_LOG:-$OUT_ROOT/controller.log}
PID_FILE=${PID_FILE:-$ROOT/scripts/tmp/$RUN_TAG.controller.pid}
LOCK_FILE=${LOCK_FILE:-$ROOT/scripts/tmp/$RUN_TAG.controller.lock}
ACTIVE_PID=""

fail() {
  echo "[gss-scaling][ERROR] $*" >&2
  exit 2
}

validate_static_inputs() {
  [[ -x "$PY" ]] || fail "missing Python: $PY"
  [[ -f "$CKPT" ]] || fail "missing checkpoint: $CKPT"
  [[ -f "$MANIFEST" ]] || fail "missing manifest: $MANIFEST"
  [[ "$WINDOW_SHIFT" == "256" ]] || \
    fail "this benchmark requires WINDOW_SHIFT=256, got $WINDOW_SHIFT"

  "$PY" - "$CKPT" "$MANIFEST" "$SPLITS" "$WORKLOADS" \
    "$SEEDS" "$CORE_COUNTS" <<'PY'
import json
import os
import sys
import torch

checkpoint_path, manifest_path, split_csv, workload_csv, seed_csv, core_csv = sys.argv[1:]
try:
    checkpoint = torch.load(checkpoint_path, map_location="cpu", weights_only=False)
except TypeError:
    checkpoint = torch.load(checkpoint_path, map_location="cpu")

model = checkpoint.get("config", {}).get("model", {})
contract = checkpoint.get("contract", {})
gss = contract.get("gss")
exposure = contract.get("exposure")
if not isinstance(gss, dict):
    raise SystemExit("checkpoint has no GSS contract")
if gss.get("clock_source") != "commit":
    raise SystemExit(f"expected commit-clock GSS, got {gss.get('clock_source')}")
if model.get("gss_mode") != "causal_adapter":
    raise SystemExit(f"expected causal-adapter GSS, got {model.get('gss_mode')}")
if not model.get("gss_exposure_features") or not isinstance(exposure, dict):
    raise SystemExit("checkpoint does not consume Exposure-v1 features")

with open(manifest_path, "r", encoding="utf-8") as handle:
    manifest = json.load(handle)
splits = [value.strip() for value in split_csv.split(",") if value.strip()]
workloads = {value.strip() for value in workload_csv.split(",") if value.strip()}
seeds = {int(value) for value in seed_csv.split(",") if value.strip()}
cores = [int(value) for value in core_csv.split(",") if value.strip()]
rows = [
    row
    for split in splits
    for row in manifest.get("splits", {}).get(split, [])
    if str(row.get("workload", "")) in workloads
    and int(row.get("seed", -1)) in seeds
    and int(row.get("n_cores", -1)) in set(cores)
]
counts = {core: sum(int(row.get("n_cores", -1)) == core for row in rows) for core in cores}
if len(rows) != len(cores) or any(value != 1 for value in counts.values()):
    raise SystemExit(
        f"expected exactly one trace for every requested core count; rows={len(rows)} counts={counts}"
    )
for row in rows:
    sidecar = row.get("exposure_sidecar_dir")
    if not sidecar or not os.path.isdir(sidecar):
        raise SystemExit(f"missing Exposure-v1 sidecar for {row.get('trace_id')}: {sidecar}")

print(
    f"[gss-scaling] checkpoint_step={int(checkpoint.get('step') or 0)} "
    f"gss_clock={gss.get('clock_source')} exposure={exposure.get('feature_schema')}"
)
for row in sorted(rows, key=lambda item: int(item["n_cores"])):
    print(f"[gss-scaling] C{row['n_cores']} trace={row['trace_id']}")
PY
}

ensure_native_gss() {
  if ! "$PY" -c \
    'import os; import tcsim.v30._gss_native as m; raise SystemExit(os.path.getmtime(m.__file__) < os.path.getmtime("tcsim/v30/native_gss.cpp"))' \
    >/dev/null 2>&1; then
    echo "[gss-scaling] rebuilding native GSS hot path"
    "$PY" scripts/build_v30_gss_native.py
  fi
}

validate_visible_gpus() {
  local physical=$1
  local expected=$2
  local actual
  actual=$(CUDA_VISIBLE_DEVICES="$physical" "$PY" -c 'import torch; print(torch.cuda.device_count())')
  [[ "$actual" == "$expected" ]] || \
    fail "requested $expected GPUs ($physical), but PyTorch sees $actual"
}

run_mode_once() {
  local label=$1
  local physical_gpus=$2
  local parallel_mode=$3
  local local_devices=$4
  local mode_out="$OUT_ROOT/$label"
  local mode_log="$OUT_ROOT/$label.log"
  local args=(
    --ckpt "$CKPT"
    --manifest "$MANIFEST"
    --splits "$SPLITS"
    --out "$mode_out"
    --mode free
    --device cuda:0
    --amp-dtype bf16
    --sdpa-backend auto
    --core-counts "$CORE_COUNTS"
    --workloads "$WORKLOADS"
    --seeds "$SEEDS"
    --max-traces 4
    --max-oracle-samples 0
    --max-free-steps 0
    --target-stride "$TARGET_STRIDE"
    --min-step-cycles 4
    --max-step-cycles 1024
    --max-no-progress-steps 64
    --max-core-stall-steps 256
    --progress-every 100
    --window-parallel-mode "$parallel_mode"
    --gss-ablation-mode predicted-order
    --resume
    --fail-fast
  )
  if [[ "$parallel_mode" != "serial" ]]; then
    args+=(
      --window-parallel-devices "$local_devices"
      --window-parallel-shift "$WINDOW_SHIFT"
      --window-context-backend process
    )
  fi

  mkdir -p "$mode_out"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] start label=$label mode=$parallel_mode physical_gpus=$physical_gpus devices=${local_devices:-cuda:0}" | tee -a "$mode_log"
  CUDA_VISIBLE_DEVICES="$physical_gpus" TCSIM_GSS_BACKEND=native \
    "$PY" scripts/infer_v29.py "${args[@]}" >>"$mode_log" 2>&1 &
  ACTIVE_PID=$!
  wait "$ACTIVE_PID"
  local status=$?
  ACTIVE_PID=""
  if (( status == 0 )); then
    [[ -s "$mode_out/report.json" ]] || fail "$label completed without report.json"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] PASS label=$label report=$mode_out/report.json" | tee -a "$mode_log"
  fi
  return "$status"
}

run_mode_with_watchdog() {
  local label=$1
  local physical_gpus=$2
  local parallel_mode=$3
  local local_devices=$4
  local attempt=0
  while (( attempt <= MAX_RESTARTS )); do
    attempt=$((attempt + 1))
    echo "[gss-scaling] label=$label attempt=$attempt/$((MAX_RESTARTS + 1))"
    set +e
    run_mode_once "$label" "$physical_gpus" "$parallel_mode" "$local_devices"
    local status=$?
    set -e
    if (( status == 0 )); then
      return 0
    fi
    echo "[gss-scaling][WARN] label=$label exit=$status"
    (( attempt <= MAX_RESTARTS )) || fail "$label exhausted restart budget"
    sleep 10
  done
}

write_summary() {
  "$PY" - "$OUT_ROOT" <<'PY'
import json
import os
import sys

root = os.path.abspath(sys.argv[1])
labels = ["serial", "unconditional4", "unconditional8"]
depths = {"serial": 1, "unconditional4": 4, "unconditional8": 8}
summaries = {}
per_core = {}

for label in labels:
    path = os.path.join(root, label, "report.json")
    with open(path, "r", encoding="utf-8") as handle:
        report = json.load(handle)
    rows = sorted(report["traces"], key=lambda row: int(row["n_cores"]))
    total_uops = sum(int(row["free_running"]["retired_uops"]) for row in rows)
    total_elapsed = sum(float(row["free_running"]["elapsed_s"]) for row in rows)
    total_gss_preview = sum(float(row["free_running"].get("gss_preview_seconds", 0.0)) for row in rows)
    total_gss_commit = sum(float(row["free_running"].get("gss_commit_seconds", 0.0)) for row in rows)
    total_context = sum(float(row["free_running"]["timing_breakdown"]["context_build_seconds"]) for row in rows)
    total_predict = sum(float(row["free_running"]["timing_breakdown"]["predict_seconds"]) for row in rows)
    total_model = sum(float(row["free_running"].get("model_forward_seconds", 0.0)) for row in rows)
    total_scheduler = sum(float(row["free_running"]["timing_breakdown"]["scheduler_seconds"]) for row in rows)
    summaries[label] = {
        "depth": depths[label],
        "trace_count": len(rows),
        "retired_uops": total_uops,
        "elapsed_s_sum": total_elapsed,
        "pooled_uops_per_s": total_uops / total_elapsed,
        "gss_preview_s_sum": total_gss_preview,
        "gss_commit_s_sum": total_gss_commit,
        "gss_timer_fraction": (total_gss_preview + total_gss_commit) / total_elapsed,
        "context_build_s_sum": total_context,
        "predict_s_sum": total_predict,
        "model_forward_s_sum": total_model,
        "scheduler_s_sum": total_scheduler,
        "core_equal_cpi_mape": sum(
            float(row["free_running"]["roi_cpi_error"]) for row in rows
        ) / len(rows),
        "model_forwards": sum(int(row["free_running"].get("model_forwards", 0)) for row in rows),
        "parallel_waves": sum(int(row["free_running"].get("parallel_waves", 0)) for row in rows),
    }
    per_core[label] = {
        str(row["n_cores"]): {
            "uops_per_s": float(row["free_running"]["uops_per_s"]),
            "elapsed_s": float(row["free_running"]["elapsed_s"]),
            "roi_cpi_error": float(row["free_running"]["roi_cpi_error"]),
            "gss_preview_s": float(row["free_running"].get("gss_preview_seconds", 0.0)),
            "gss_commit_s": float(row["free_running"].get("gss_commit_seconds", 0.0)),
            "gss_accuracy_mode": row["free_running"].get("gss_accuracy_mode"),
        }
        for row in rows
    }

serial_rate = summaries["serial"]["pooled_uops_per_s"]
for label in labels:
    summaries[label]["speedup_vs_serial"] = summaries[label]["pooled_uops_per_s"] / serial_rate
    summaries[label]["gpu_scaling_efficiency"] = summaries[label]["speedup_vs_serial"] / depths[label]
    for core, values in per_core[label].items():
        values["speedup_vs_serial"] = values["uops_per_s"] / per_core["serial"][core]["uops_per_s"]

payload = {
    "contract": {
        "checkpoint_family": "v29 Full-QKVR + online commit-clock GSS + Exposure-v1",
        "workload": "W_v28_bvc_encoder_base",
        "seed": 1,
        "core_counts": [4, 8, 16, 32],
        "window_size_uops": 256,
        "window_shift_uops": 256,
        "parallel_accuracy_contract": "parallel-relaxed-continuous-shadow-deadline-v2",
    },
    "summary": summaries,
    "per_core": per_core,
}
with open(os.path.join(root, "throughput_summary.json"), "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, ensure_ascii=False)

lines = [
    "# GSS shift=256 throughput scaling",
    "",
    "| Mode | GPUs/depth | Pooled UOP/s | Speedup | Efficiency | Core-equal CPI MAPE | Elapsed sum | GSS share |",
    "|---|---:|---:|---:|---:|---:|---:|---:|",
]
for label in labels:
    row = summaries[label]
    lines.append(
        f"| {label} | {row['depth']} | {row['pooled_uops_per_s']:.1f} | "
        f"{row['speedup_vs_serial']:.3f}x | {100.0 * row['gpu_scaling_efficiency']:.1f}% | "
        f"{100.0 * row['core_equal_cpi_mape']:.3f}% | {row['elapsed_s_sum']:.1f}s | "
        f"{100.0 * row['gss_timer_fraction']:.2f}% |"
    )
lines.extend([
    "",
    "## Per-core speedup",
    "",
    "| Core count | Serial UOP/s | Unconditional-4 | Speedup-4 | Unconditional-8 | Speedup-8 |",
    "|---:|---:|---:|---:|---:|---:|",
])
for core in ("4", "8", "16", "32"):
    s = per_core["serial"][core]
    p4 = per_core["unconditional4"][core]
    p8 = per_core["unconditional8"][core]
    lines.append(
        f"| C{core} | {s['uops_per_s']:.1f} | {p4['uops_per_s']:.1f} | "
        f"{p4['speedup_vs_serial']:.3f}x | {p8['uops_per_s']:.1f} | {p8['speedup_vs_serial']:.3f}x |"
    )
lines.extend([
    "",
    "## Runtime breakdown",
    "",
    "| Mode | Context | Predict | Model forward | Scheduler | GSS preview | GSS commit | Waves | Lane forwards |",
    "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
])
for label in labels:
    row = summaries[label]
    lines.append(
        f"| {label} | {row['context_build_s_sum']:.1f}s | {row['predict_s_sum']:.1f}s | "
        f"{row['model_forward_s_sum']:.1f}s | {row['scheduler_s_sum']:.1f}s | "
        f"{row['gss_preview_s_sum']:.1f}s | {row['gss_commit_s_sum']:.1f}s | "
        f"{row['parallel_waves']} | {row['model_forwards']} |"
    )
lines.extend([
    "",
    "GSS timers are nested in total elapsed time and must not be added to it.",
    "Parallel CPI uses the relaxed GSS ordering contract and is reported for accuracy-cost auditing, not serial-exact equivalence.",
    "",
])
with open(os.path.join(root, "throughput_summary.md"), "w", encoding="utf-8") as handle:
    handle.write("\n".join(lines))
print(f"[gss-scaling] summary={os.path.join(root, 'throughput_summary.md')}")
PY
}

run_controller() {
  mkdir -p "$OUT_ROOT" "$ROOT/scripts/tmp"
  exec 9>"$LOCK_FILE"
  flock -n 9 || fail "another controller holds $LOCK_FILE"
  printf '%s\n' "$$" >"$PID_FILE"

  cleanup() { rm -f "$PID_FILE"; }
  stop_active() {
    if [[ "$ACTIVE_PID" =~ ^[0-9]+$ ]]; then
      kill -TERM "$ACTIVE_PID" 2>/dev/null || true
      wait "$ACTIVE_PID" 2>/dev/null || true
    fi
    exit 130
  }
  trap cleanup EXIT
  trap stop_active INT TERM

  validate_static_inputs
  ensure_native_gss
  validate_visible_gpus "$GPUS8" 8
  run_mode_with_watchdog serial "$SERIAL_GPU" serial ""
  run_mode_with_watchdog unconditional4 "$GPUS4" unconditional cuda:0,cuda:1,cuda:2,cuda:3
  run_mode_with_watchdog unconditional8 "$GPUS8" unconditional cuda:0,cuda:1,cuda:2,cuda:3,cuda:4,cuda:5,cuda:6,cuda:7
  write_summary
  echo "[gss-scaling] PASS all modes complete"
}

print_configuration() {
  validate_static_inputs
  echo "[gss-scaling] checkpoint=$CKPT"
  echo "[gss-scaling] manifest=$MANIFEST"
  echo "[gss-scaling] selection=split:$SPLITS workload:$WORKLOADS seed:$SEEDS cores:$CORE_COUNTS"
  echo "[gss-scaling] modes=serial,unconditional4,unconditional8 shift=$WINDOW_SHIFT stride=$TARGET_STRIDE"
  echo "[gss-scaling] output=$OUT_ROOT"
  echo "[gss-scaling] controller_log=$CONTROLLER_LOG"
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
  --summary)
    write_summary
    exit $?
    ;;
  "") ;;
  *)
    echo "usage: $0 [--check|--controller|--summary]" >&2
    exit 2
    ;;
esac

validate_static_inputs
if [[ -f "$PID_FILE" ]]; then
  existing_pid=$(<"$PID_FILE")
  if [[ "$existing_pid" =~ ^[0-9]+$ ]] \
      && kill -0 "$existing_pid" 2>/dev/null \
      && [[ -r "/proc/$existing_pid/cmdline" ]] \
      && grep -aFq "launch_v30_gss_shift256_scaling_benchmark.sh" \
        "/proc/$existing_pid/cmdline"; then
    fail "controller already running: pid=$existing_pid log=$CONTROLLER_LOG"
  fi
fi

mkdir -p "$OUT_ROOT" "$ROOT/scripts/tmp"
nohup env \
  ROOT="$ROOT" PY="$PY" CKPT="$CKPT" MANIFEST="$MANIFEST" \
  SPLITS="$SPLITS" WORKLOADS="$WORKLOADS" SEEDS="$SEEDS" \
  CORE_COUNTS="$CORE_COUNTS" TARGET_STRIDE="$TARGET_STRIDE" \
  WINDOW_SHIFT="$WINDOW_SHIFT" SERIAL_GPU="$SERIAL_GPU" \
  GPUS4="$GPUS4" GPUS8="$GPUS8" MAX_RESTARTS="$MAX_RESTARTS" \
  RUN_TAG="$RUN_TAG" OUT="$OUT_ROOT" CONTROLLER_LOG="$CONTROLLER_LOG" \
  PID_FILE="$PID_FILE" LOCK_FILE="$LOCK_FILE" \
  bash "$0" --controller >>"$CONTROLLER_LOG" 2>&1 &
controller_pid=$!
printf '%s\n' "$controller_pid" >"$PID_FILE"

echo "[gss-scaling] started controller_pid=$controller_pid"
echo "[gss-scaling] monitor: tail -f $CONTROLLER_LOG"
echo "[gss-scaling] active mode logs: $OUT_ROOT/{serial,unconditional4,unconditional8}.log"
echo "[gss-scaling] final summary: $OUT_ROOT/throughput_summary.md"
