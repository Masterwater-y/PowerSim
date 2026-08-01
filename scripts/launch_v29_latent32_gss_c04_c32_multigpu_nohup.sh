#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT=${ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
cd "$PROJECT_ROOT"

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
CKPT=${CKPT:-$PROJECT_ROOT/ckpt/tcsim_v29_latent32_scratch_100m_8gpu_60k/best.pt}
MANIFEST=${MANIFEST:-$PROJECT_ROOT/data/v30_gss_commit_dataset/manifest.json}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}

# The commit-clock GSS sidecars currently cover the 16 seed0 base workloads.
# After the c1 slice is filtered out this is 16 workloads x 4 core counts = 64
# complete free-running traces, sharded across the selected GPUs.
SPLITS=${SPLITS:-train}
SEEDS=${SEEDS:-0}
CORE_COUNTS=${CORE_COUNTS:-4,8,16,32}
WORKLOADS=${WORKLOADS:-}

TARGET_STRIDE=${TARGET_STRIDE:-256}
MIN_STEP_CYCLES=${MIN_STEP_CYCLES:-4}
MAX_STEP_CYCLES=${MAX_STEP_CYCLES:-1024}
MAX_NO_PROGRESS_STEPS=${MAX_NO_PROGRESS_STEPS:-64}
MAX_CORE_STALL_STEPS=${MAX_CORE_STALL_STEPS:-256}
MAX_FREE_STEPS=${MAX_FREE_STEPS:-0}
MAX_TRACES=${MAX_TRACES:-0}
PROGRESS_EVERY=${PROGRESS_EVERY:-100}

TCSIM_CONTEXT_BACKEND=${TCSIM_CONTEXT_BACKEND:-native}
TCSIM_GSS_BACKEND=${TCSIM_GSS_BACKEND:-native}
CROSS_ATTENTION_BACKEND=${CROSS_ATTENTION_BACKEND:-hierarchical_latent}
QRKV_PROJECTION_BACKEND=${QRKV_PROJECTION_BACKEND:-fused}
AMP_DTYPE=${AMP_DTYPE:-bf16}
SDPA_BACKEND=${SDPA_BACKEND:-auto}
GSS_PMU_ONLY=${GSS_PMU_ONLY:-1}
RESUME=${RESUME:-1}

RUN_STAMP=${RUN_STAMP:-$(date +%Y%m%d_%H%M%S)}
RUN_TAG=${RUN_TAG:-v29_latent32_best55k_gss_pmu_seed0_base_c04_c08_c16_c32_$RUN_STAMP}
OUT_DIR=${OUT:-$PROJECT_ROOT/logs/$RUN_TAG}
LAUNCH_LOG=${LAUNCH_LOG:-$OUT_DIR/launcher.log}
PID_FILE=${PID_FILE:-$OUT_DIR/launcher.pid}

[[ -x "$PY" ]] || {
  echo "[v29-latent-infer][ERROR] missing Python: $PY" >&2
  exit 2
}
[[ -f "$CKPT" ]] || {
  echo "[v29-latent-infer][ERROR] missing checkpoint: $CKPT" >&2
  exit 2
}
[[ -f "$MANIFEST" ]] || {
  echo "[v29-latent-infer][ERROR] missing manifest: $MANIFEST" >&2
  exit 2
}
[[ "$CROSS_ATTENTION_BACKEND" == "hierarchical_latent" ]] || {
  echo "[v29-latent-infer][ERROR] latent32 checkpoint requires CROSS_ATTENTION_BACKEND=hierarchical_latent" >&2
  exit 2
}
[[ "$GSS_PMU_ONLY" == "1" ]] || {
  echo "[v29-latent-infer][ERROR] this launcher requires GSS_PMU_ONLY=1" >&2
  exit 2
}

IFS=',' read -r -a gpu_array <<< "$GPUS"
(( ${#gpu_array[@]} > 0 )) || {
  echo "[v29-latent-infer][ERROR] GPUS must not be empty" >&2
  exit 2
}

selection_summary=$(
  "$PY" -c '
import os
import sys
from tcsim.v29.inference import load_manifest_sources

manifest, split_csv, core_csv, seed_csv, workload_csv = sys.argv[1:]
splits = [value.strip() for value in split_csv.split(",") if value.strip()]
cores = {int(value) for value in core_csv.split(",") if value.strip()}
seeds = {int(value) for value in seed_csv.split(",") if value.strip()}
workloads = {value.strip() for value in workload_csv.split(",") if value.strip()}
rows = load_manifest_sources(manifest, splits)
rows = [row for row in rows if int(row.get("n_cores", 0)) in cores]
if seeds:
    rows = [row for row in rows if int(row.get("seed", -1)) in seeds]
if workloads:
    rows = [row for row in rows if str(row.get("workload", "")) in workloads]
if not rows:
    raise SystemExit("no trace selected by SPLITS/CORE_COUNTS/SEEDS/WORKLOADS")
missing = [
    str(row.get("cache_dir", ""))
    for row in rows
    if not row.get("gss_sidecar_dir")
    or not os.path.isdir(str(row["gss_sidecar_dir"]))
]
if missing:
    preview = "\n  ".join(missing[:5])
    raise SystemExit(
        f"{len(missing)} selected traces lack a commit-clock GSS sidecar:\n  {preview}"
    )
selected_cores = sorted({int(row["n_cores"]) for row in rows})
print(
    len(rows),
    len({str(row.get("workload", "")) for row in rows}),
    ",".join(map(str, selected_cores)),
)
' "$MANIFEST" "$SPLITS" "$CORE_COUNTS" "$SEEDS" "$WORKLOADS"
)
read -r selected_trace_count selected_workload_count selected_core_counts \
  <<< "$selection_summary"

if [[ -f "$PID_FILE" ]]; then
  existing_pid=$(<"$PID_FILE")
  if [[ "$existing_pid" =~ ^[0-9]+$ ]] && kill -0 "$existing_pid" 2>/dev/null; then
    echo "[v29-latent-infer][ERROR] evaluation is already running: pid=$existing_pid" >&2
    echo "[v29-latent-infer] log=$LAUNCH_LOG" >&2
    exit 2
  fi
fi

mkdir -p "$OUT_DIR"
printf '[%s] launch checkpoint=%s gpus=%s splits=%s cores=%s stride=%s\n' \
  "$(date '+%Y-%m-%d %H:%M:%S')" "$CKPT" "$GPUS" "$SPLITS" \
  "$CORE_COUNTS" "$TARGET_STRIDE" >>"$LAUNCH_LOG"

nohup env \
  ROOT="$PROJECT_ROOT" \
  PY="$PY" \
  CKPT="$CKPT" \
  MANIFEST="$MANIFEST" \
  OUT="$OUT_DIR" \
  GPUS="$GPUS" \
  SPLITS="$SPLITS" \
  SEEDS="$SEEDS" \
  WORKLOADS="$WORKLOADS" \
  MODE=free \
  CORE_COUNTS="$CORE_COUNTS" \
  MAX_ORACLE_SAMPLES=0 \
  MAX_FREE_STEPS="$MAX_FREE_STEPS" \
  MAX_TRACES="$MAX_TRACES" \
  TARGET_STRIDE="$TARGET_STRIDE" \
  MIN_STEP_CYCLES="$MIN_STEP_CYCLES" \
  MAX_STEP_CYCLES="$MAX_STEP_CYCLES" \
  MAX_NO_PROGRESS_STEPS="$MAX_NO_PROGRESS_STEPS" \
  MAX_CORE_STALL_STEPS="$MAX_CORE_STALL_STEPS" \
  AMP_DTYPE="$AMP_DTYPE" \
  SDPA_BACKEND="$SDPA_BACKEND" \
  TCSIM_CONTEXT_BACKEND="$TCSIM_CONTEXT_BACKEND" \
  TCSIM_GSS_BACKEND="$TCSIM_GSS_BACKEND" \
  GSS_PMU_ONLY="$GSS_PMU_ONLY" \
  CROSS_ATTENTION_BACKEND="$CROSS_ATTENTION_BACKEND" \
  QRKV_PROJECTION_BACKEND="$QRKV_PROJECTION_BACKEND" \
  PROGRESS_EVERY="$PROGRESS_EVERY" \
  ORACLE_DRIFT_DIAGNOSTICS=0 \
  RESUME="$RESUME" \
  bash scripts/run_v29_eval_8gpu.sh \
  >>"$LAUNCH_LOG" 2>&1 &

launcher_pid=$!
printf '%s\n' "$launcher_pid" >"$PID_FILE"

printf '[v29-latent-infer] pid=%s\n' "$launcher_pid"
printf '[v29-latent-infer] checkpoint=%s (best step 55000)\n' "$CKPT"
printf '[v29-latent-infer] gpus=%s shards=%s cores=%s\n' \
  "$GPUS" "${#gpu_array[@]}" "$CORE_COUNTS"
printf '[v29-latent-infer] traces=%s workloads=%s selected_cores=%s\n' \
  "$selected_trace_count" "$selected_workload_count" "$selected_core_counts"
printf '[v29-latent-infer] timing=v29-latent32 pmu=online-gss backend=%s projection=%s\n' \
  "$CROSS_ATTENTION_BACKEND" "$QRKV_PROJECTION_BACKEND"
printf '[v29-latent-infer] out=%s\n' "$OUT_DIR"
printf '[v29-latent-infer] log=%s\n' "$LAUNCH_LOG"
printf '[v29-latent-infer] report=%s/report.txt\n' "$OUT_DIR"
printf '[v29-latent-infer] monitor: tail -f %q\n' "$LAUNCH_LOG"
