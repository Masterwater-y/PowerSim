#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
cd "$ROOT"

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
CKPT=${CKPT:-ckpt/tcsim_v30_branch_b3_replay_history_100m_8gpu_60000/best.pt}
MANIFEST=${MANIFEST:-data/v30_branch_replay_dataset/manifest.json}
OUT_ROOT=${OUT_ROOT:-logs/v30_b3_branch_scale_no_train_20260728}
SPLITS=${SPLITS:-development_heldout,deployment_inference}
CORE_COUNTS=${CORE_COUNTS:-4,8,16,32}
WORKLOADS=${WORKLOADS:-W_v28_bvc_encoder_heldout,W_v28_pytorch_heldout,W_v28_coh_readmostly_sparse,W_v28_gofeed_heldout,W_v28_redis_heldout}

mkdir -p "$OUT_ROOT"

run_variant() {
  local label=$1
  local gpus=$2
  local event_scale=$3
  local history_scale=$4
  local output="$OUT_ROOT/$label"
  echo "[v30-scale] start label=$label gpus=$gpus event=$event_scale history=$history_scale"
  env \
    PY="$PY" \
    CKPT="$CKPT" \
    MANIFEST="$MANIFEST" \
    OUT="$output" \
    GPUS="$gpus" \
    SPLITS="$SPLITS" \
    MODE=free \
    WINDOW_PARALLEL_MODE=serial \
    CORE_COUNTS="$CORE_COUNTS" \
    WORKLOADS="$WORKLOADS" \
    TARGET_STRIDE=256 \
    MAX_FREE_STEPS=0 \
    MAX_ORACLE_SAMPLES=0 \
    AMP_DTYPE=bf16 \
    SDPA_BACKEND=auto \
    PROGRESS_EVERY=500 \
    ORACLE_DRIFT_DIAGNOSTICS=0 \
    BRANCH_EVENT_SCALE="$event_scale" \
    BRANCH_HISTORY_SCALE="$history_scale" \
    RESUME=1 \
    bash scripts/run_v29_eval_8gpu.sh \
    >"$OUT_ROOT/$label.log" 2>&1
  echo "[v30-scale] done label=$label report=$output/report.json"
}

pids=()
labels=()
run_variant event_off 0,1 0.0 1.0 &
pids+=("$!")
labels+=(event_off)
run_variant history_off 2,3 1.0 0.0 &
pids+=("$!")
labels+=(history_off)
run_variant both_off 4,5 0.0 0.0 &
pids+=("$!")
labels+=(both_off)
run_variant both_half 6,7 0.5 0.5 &
pids+=("$!")
labels+=(both_half)

on_signal() {
  for pid in "${pids[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  exit 130
}
trap on_signal INT TERM

failed=0
for index in "${!pids[@]}"; do
  if wait "${pids[$index]}"; then
    continue
  fi
  echo "[v30-scale][ERROR] label=${labels[$index]} failed; see $OUT_ROOT/${labels[$index]}.log" >&2
  failed=1
done
(( failed == 0 )) || exit 2

echo "[v30-scale] all variants complete: $OUT_ROOT"
