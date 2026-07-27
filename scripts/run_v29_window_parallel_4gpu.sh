#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT=${ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
cd "$PROJECT_ROOT"

PROJECT_TMP=${PROJECT_TMP:-$PROJECT_ROOT/tmp}
mkdir -p "$PROJECT_TMP"
export TMPDIR="$PROJECT_TMP"

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
CKPT=${CKPT:-$PROJECT_ROOT/ckpt/tcsim_v29_packed3_100m_8gpu_60k/best.pt}
MANIFEST=${MANIFEST:-$PROJECT_ROOT/data/v29_global_time_dataset/manifest.json}

# Physical GPU IDs.  They are exposed to the process as local cuda:0..cuda:3.
GPUS=${GPUS:-0,1,2,3}
LOCAL_DEVICES=cuda:0,cuda:1,cuda:2,cuda:3

SPLITS=${SPLITS:-seed0_inference,development_heldout}
CORE_COUNTS=${CORE_COUNTS:-4,8,16,32}
WORKLOADS=${WORKLOADS:-}
SEEDS=${SEEDS:-}
MODES=${MODES:-unconditional,speculative}
WINDOW_SHIFT=${WINDOW_SHIFT:-64}
WINDOW_CONTEXT_BACKEND=${WINDOW_CONTEXT_BACKEND:-process}
TARGET_STRIDE=${TARGET_STRIDE:-256}
MIN_STEP_CYCLES=${MIN_STEP_CYCLES:-4}
MAX_STEP_CYCLES=${MAX_STEP_CYCLES:-1024}
MAX_NO_PROGRESS_STEPS=${MAX_NO_PROGRESS_STEPS:-64}
MAX_FREE_STEPS=${MAX_FREE_STEPS:-0}
MAX_TRACES=${MAX_TRACES:-0}
PROGRESS_EVERY=${PROGRESS_EVERY:-200}
AMP_DTYPE=${AMP_DTYPE:-bf16}
SDPA_BACKEND=${SDPA_BACKEND:-auto}
RESUME=${RESUME:-0}
ORACLE_DRIFT_DIAGNOSTICS=${ORACLE_DRIFT_DIAGNOSTICS:-0}
FAIL_FAST=${FAIL_FAST:-1}

RUN_STAMP=$(date +%Y%m%d_%H%M%S)
OUT_ROOT=${OUT:-$PROJECT_ROOT/logs/v29_window_parallel_4gpu_$RUN_STAMP}

[[ -x "$PY" ]] || {
  echo "[v29-window-4gpu][ERROR] missing Python: $PY" >&2
  exit 2
}
[[ -f "$CKPT" ]] || {
  echo "[v29-window-4gpu][ERROR] missing checkpoint: $CKPT" >&2
  exit 2
}
[[ -f "$MANIFEST" ]] || {
  echo "[v29-window-4gpu][ERROR] missing manifest: $MANIFEST" >&2
  exit 2
}
[[ "$WINDOW_SHIFT" =~ ^[0-9]+$ ]] \
  && (( WINDOW_SHIFT >= 1 && WINDOW_SHIFT <= 256 )) || {
  echo "[v29-window-4gpu][ERROR] WINDOW_SHIFT must satisfy 1 <= shift <= 256" >&2
  exit 2
}
[[ "$WINDOW_CONTEXT_BACKEND" =~ ^(serial|thread|process)$ ]] || {
  echo "[v29-window-4gpu][ERROR] invalid WINDOW_CONTEXT_BACKEND: $WINDOW_CONTEXT_BACKEND" >&2
  exit 2
}

IFS=',' read -r -a mode_array <<< "$MODES"
(( ${#mode_array[@]} > 0 )) || {
  echo "[v29-window-4gpu][ERROR] MODES must not be empty" >&2
  exit 2
}
declare -A seen_modes=()
for index in "${!mode_array[@]}"; do
  mode=${mode_array[$index]//[[:space:]]/}
  [[ "$mode" =~ ^(unconditional|speculative)$ ]] || {
    echo "[v29-window-4gpu][ERROR] invalid parallel mode: ${mode_array[$index]}" >&2
    exit 2
  }
  [[ -z "${seen_modes[$mode]:-}" ]] || {
    echo "[v29-window-4gpu][ERROR] duplicate parallel mode: $mode" >&2
    exit 2
  }
  seen_modes[$mode]=1
  mode_array[$index]=$mode
done

IFS=',' read -r -a gpu_array <<< "$GPUS"
(( ${#gpu_array[@]} == 4 )) || {
  echo "[v29-window-4gpu][ERROR] GPUS must contain exactly four physical GPU IDs" >&2
  exit 2
}
declare -A seen_gpus=()
for index in "${!gpu_array[@]}"; do
  gpu=${gpu_array[$index]//[[:space:]]/}
  [[ "$gpu" =~ ^[0-9]+$ ]] || {
    echo "[v29-window-4gpu][ERROR] invalid GPU ID: ${gpu_array[$index]}" >&2
    exit 2
  }
  [[ -z "${seen_gpus[$gpu]:-}" ]] || {
    echo "[v29-window-4gpu][ERROR] duplicate GPU ID: $gpu" >&2
    exit 2
  }
  seen_gpus[$gpu]=1
  gpu_array[$index]=$gpu
done
PHYSICAL_GPUS=$(IFS=,; echo "${gpu_array[*]}")

if ! visible_gpu_count=$(
  CUDA_VISIBLE_DEVICES="$PHYSICAL_GPUS" \
    "$PY" -c 'import torch; print(torch.cuda.device_count())'
); then
  echo "[v29-window-4gpu][ERROR] failed to query CUDA devices" >&2
  exit 2
fi
[[ "$visible_gpu_count" == "4" ]] || {
  echo "[v29-window-4gpu][ERROR] requested four GPUs but PyTorch sees $visible_gpu_count" >&2
  exit 2
}

mkdir -p "$OUT_ROOT"

common_args=(
  --ckpt "$CKPT"
  --manifest "$MANIFEST"
  --splits "$SPLITS"
  --mode free
  --window-parallel-devices "$LOCAL_DEVICES"
  --window-parallel-shift "$WINDOW_SHIFT"
  --window-context-backend "$WINDOW_CONTEXT_BACKEND"
  --amp-dtype "$AMP_DTYPE"
  --sdpa-backend "$SDPA_BACKEND"
  --core-counts "$CORE_COUNTS"
  --max-free-steps "$MAX_FREE_STEPS"
  --max-traces "$MAX_TRACES"
  --target-stride "$TARGET_STRIDE"
  --min-step-cycles "$MIN_STEP_CYCLES"
  --max-step-cycles "$MAX_STEP_CYCLES"
  --max-no-progress-steps "$MAX_NO_PROGRESS_STEPS"
  --progress-every "$PROGRESS_EVERY"
)

if [[ -n "$WORKLOADS" ]]; then
  common_args+=(--workloads "$WORKLOADS")
fi
if [[ -n "$SEEDS" ]]; then
  common_args+=(--seeds "$SEEDS")
fi
if [[ "$RESUME" == "1" ]]; then
  common_args+=(--resume)
fi
if [[ "$ORACLE_DRIFT_DIAGNOSTICS" == "1" ]]; then
  common_args+=(--oracle-drift-diagnostics)
fi
if [[ "$FAIL_FAST" == "1" ]]; then
  common_args+=(--fail-fast)
fi

echo "[v29-window-4gpu] checkpoint=$CKPT"
echo "[v29-window-4gpu] manifest=$MANIFEST"
echo "[v29-window-4gpu] physical_gpus=$PHYSICAL_GPUS local_devices=$LOCAL_DEVICES"
echo "[v29-window-4gpu] modes=$MODES splits=$SPLITS cores=$CORE_COUNTS shift=$WINDOW_SHIFT stride=$TARGET_STRIDE context_backend=$WINDOW_CONTEXT_BACKEND"
echo "[v29-window-4gpu] output_root=$OUT_ROOT"

for mode in "${mode_array[@]}"; do
  mode_out="$OUT_ROOT/$mode"
  mode_log="$OUT_ROOT/$mode.log"
  mkdir -p "$mode_out"
  echo "[v29-window-4gpu] starting mode=$mode out=$mode_out"
  CUDA_VISIBLE_DEVICES="$PHYSICAL_GPUS" \
    "$PY" scripts/infer_v29.py \
      "${common_args[@]}" \
      --out "$mode_out" \
      --window-parallel-mode "$mode" \
      2>&1 | tee "$mode_log"
  echo "[v29-window-4gpu] completed mode=$mode"
  echo "[v29-window-4gpu] report_json=$mode_out/report.json"
  echo "[v29-window-4gpu] report_text=$mode_out/report.txt"
done

echo "[v29-window-4gpu] requested modes completed: $MODES"
for mode in "${mode_array[@]}"; do
  echo "[v29-window-4gpu] $mode=$OUT_ROOT/$mode/report.txt"
done
