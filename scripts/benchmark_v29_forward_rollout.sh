#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
cd "$ROOT"
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
GPU=${GPU:-0}
CKPT=${CKPT:-ckpt/tcsim_v29_packed3_100m_8gpu_60k/best.pt}
MANIFEST=${MANIFEST:-data/v30_gss_commit_dataset/manifest.json}
SPLITS=${SPLITS:-seed0_inference}
CORE_COUNTS=${CORE_COUNTS:-32}
WORKLOADS=${WORKLOADS:-W_v28_int_alu_dense}
MAX_FREE_STEPS=${MAX_FREE_STEPS:-300}
TARGET_STRIDE=${TARGET_STRIDE:-256}
GSS_PMU_ONLY=${GSS_PMU_ONLY:-1}
RUN_STAMP=${RUN_STAMP:-$(date +%Y%m%d_%H%M%S)}
OUT_ROOT=${OUT_ROOT:-logs/v29_forward_ab_$RUN_STAMP}

run_case() {
  local name=$1
  local cross_backend=$2
  local projection_backend=$3
  OUT="$OUT_ROOT/$name" \
  GPUS="$GPU" \
  CKPT="$CKPT" \
  MANIFEST="$MANIFEST" \
  SPLITS="$SPLITS" \
  CORE_COUNTS="$CORE_COUNTS" \
  WORKLOADS="$WORKLOADS" \
  MODE=free \
  MAX_TRACES=1 \
  MAX_FREE_STEPS="$MAX_FREE_STEPS" \
  TARGET_STRIDE="$TARGET_STRIDE" \
  GSS_PMU_ONLY="$GSS_PMU_ONLY" \
  PROGRESS_EVERY=50 \
  ORACLE_DRIFT_DIAGNOSTICS=0 \
  CROSS_ATTENTION_BACKEND="$cross_backend" \
  QRKV_PROJECTION_BACKEND="$projection_backend" \
  bash scripts/run_v29_eval_8gpu.sh
}

echo "[v29-forward-ab] baseline: legacy cross-attention + separate Q/R/K/V"
run_case baseline legacy separate
echo "[v29-forward-ab] optimized: shared-K/V + fused Q/R/K/V"
run_case optimized flex_shared_kv fused

"$PY" scripts/compare_v29_forward_rollouts.py \
  "$OUT_ROOT/baseline/report.json" \
  "$OUT_ROOT/optimized/report.json" \
  | tee "$OUT_ROOT/comparison.json"
echo "[v29-forward-ab] comparison=$OUT_ROOT/comparison.json"
