#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
cd "$ROOT"

PROFILE=${1:-full}
case "$PROFILE" in
  targeted)
    DEFAULT_OUT=logs/v29_frozen_probe_best9500_targeted_c04_c32_s256_8gpu
    export CORE_COUNTS=${CORE_COUNTS:-4,32}
    export WORKLOADS=${WORKLOADS:-W_v28_redis_base,W_v28_redis_heldout,W_v28_simd_sse_dense,W_v28_marine_heldout,W_v28_coh_readmostly_sparse,W_v28_flink_heldout,W_v28_memory_random_mlp,W_v28_memory_seq_moderate,W_v28_gofeed_base}
    ;;
  full)
    DEFAULT_OUT=logs/v29_frozen_probe_best9500_full_s256_8gpu
    export CORE_COUNTS=${CORE_COUNTS:-4,8,16,32}
    # A full run must not inherit an accidental targeted WORKLOADS filter.
    export WORKLOADS=${FULL_WORKLOADS:-}
    ;;
  *)
    echo "usage: $0 [targeted|full]" >&2
    exit 2
    ;;
esac

export CKPT=${CKPT:-ckpt/tcsim_v29_frozen_memory_probe_e2_10k_seed1234/best.pt}
export MANIFEST=${MANIFEST:-data/v29_long_history_dataset/manifest.json}
export OUT=${OUT:-$DEFAULT_OUT}
export GPUS=${GPUS:-0,1,2,3,4,5,6,7}
# Both selected splits have materialized long-history sidecars.  The standalone
# development_heldout split intentionally does not and is therefore invalid for
# a memory-gated long-history checkpoint.
export SPLITS=${SPLITS:-seed0_inference,deployment_inference}
export MODE=${MODE:-free}
export TARGET_STRIDE=${TARGET_STRIDE:-256}
export MIN_STEP_CYCLES=${MIN_STEP_CYCLES:-4}
export MAX_STEP_CYCLES=${MAX_STEP_CYCLES:-1024}
export MAX_NO_PROGRESS_STEPS=${MAX_NO_PROGRESS_STEPS:-64}
export WINDOW_PARALLEL_MODE=${WINDOW_PARALLEL_MODE:-serial}
export AMP_DTYPE=${AMP_DTYPE:-bf16}
export SDPA_BACKEND=${SDPA_BACKEND:-auto}
export PROGRESS_EVERY=${PROGRESS_EVERY:-100}
export RESUME=${RESUME:-1}
export LAUNCH_LOG=${LAUNCH_LOG:-$OUT/launch.nohup.log}
export PID_FILE=${PID_FILE:-$OUT/launch.pid}

for required in "$CKPT" "$MANIFEST"; do
  [[ -f "$required" ]] || {
    echo "[v29-frozen-infer][ERROR] missing required file: $required" >&2
    exit 2
  }
done
if ! command -v nvidia-smi >/dev/null 2>&1 || ! nvidia-smi -L >/dev/null 2>&1; then
  echo "[v29-frozen-infer][ERROR] NVIDIA driver/GPU is not visible" >&2
  exit 3
fi

echo "[v29-frozen-infer] profile=$PROFILE"
echo "[v29-frozen-infer] checkpoint=$CKPT"
echo "[v29-frozen-infer] manifest=$MANIFEST"
echo "[v29-frozen-infer] out=$OUT"
echo "[v29-frozen-infer] gpus=$GPUS cores=$CORE_COUNTS stride=$TARGET_STRIDE"
echo "[v29-frozen-infer] splits=$SPLITS"
echo "[v29-frozen-infer] workloads=${WORKLOADS:-<all>} resume=$RESUME"

mkdir -p "$OUT"
if [[ -f "$PID_FILE" ]]; then
  previous_pid=$(<"$PID_FILE")
  if [[ "$previous_pid" =~ ^[0-9]+$ ]] && kill -0 "$previous_pid" 2>/dev/null; then
    echo "[v29-frozen-infer][ERROR] an evaluation is already running: pid=$previous_pid" >&2
    echo "[v29-frozen-infer][ERROR] log=$LAUNCH_LOG" >&2
    exit 4
  fi
fi

nohup bash scripts/run_v29_eval_8gpu.sh >"$LAUNCH_LOG" 2>&1 &
launch_pid=$!
printf '%s\n' "$launch_pid" >"$PID_FILE"

echo "[v29-frozen-infer] started pid=$launch_pid"
echo "[v29-frozen-infer] log=$LAUNCH_LOG"
echo "[v29-frozen-infer] progress: tail -f $LAUNCH_LOG"
