#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
cd "$ROOT"
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
GPU=${GPU:-0}
CKPT=${CKPT:-ckpt/tcsim_v30_gss_exposure_v1_60k_seed1234/best.pt}
MANIFEST=${MANIFEST:-data/v30_exposure_v1_inference_dataset/manifest.json}
SPLITS=${SPLITS:-seed0_inference}
CORE_COUNTS=${CORE_COUNTS:-32}
WORKLOADS=${WORKLOADS:-W_v28_int_alu_dense}
MAX_FREE_STEPS=${MAX_FREE_STEPS:-300}
TARGET_STRIDE=${TARGET_STRIDE:-256}
TCSIM_GSS_BACKEND=${TCSIM_GSS_BACKEND:-native}
RUN_STAMP=${RUN_STAMP:-$(date +%Y%m%d_%H%M%S)}
OUT_ROOT=${OUT_ROOT:-logs/v30_gss_forward_ab_$RUN_STAMP}

[[ -x "$PY" ]] || { echo "[v30-gss-forward-ab][ERROR] missing Python: $PY" >&2; exit 2; }
[[ -f "$CKPT" ]] || { echo "[v30-gss-forward-ab][ERROR] missing checkpoint: $CKPT" >&2; exit 2; }
[[ -f "$MANIFEST" ]] || { echo "[v30-gss-forward-ab][ERROR] missing manifest: $MANIFEST" >&2; exit 2; }
if [[ "$TCSIM_GSS_BACKEND" == "native" ]]; then
  if ! "$PY" -c 'import os; import tcsim.v30._gss_native as m; raise SystemExit(os.path.getmtime(m.__file__) < os.path.getmtime("tcsim/v30/native_gss.cpp"))' >/dev/null 2>&1; then
    echo "[v30-gss-forward-ab] building native GSS hot path"
    "$PY" scripts/build_v30_gss_native.py
  fi
fi

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
  PROGRESS_EVERY=50 \
  ORACLE_DRIFT_DIAGNOSTICS=0 \
  ALLOW_READY_CLOCK_GSS_COMPAT=0 \
  TCSIM_GSS_BACKEND="$TCSIM_GSS_BACKEND" \
  GSS_PMU_ONLY=0 \
  CROSS_ATTENTION_BACKEND="$cross_backend" \
  QRKV_PROJECTION_BACKEND="$projection_backend" \
  bash scripts/run_v29_eval_8gpu.sh
}

echo "[v30-gss-forward-ab] model=Exposure-v1-60k GSS=online-timing+canonical-PMU"
echo "[v30-gss-forward-ab] baseline: legacy cross-attention + separate Q/R/K/V"
run_case baseline legacy separate
echo "[v30-gss-forward-ab] optimized: shared-K/V + fused Q/R/K/V"
run_case optimized flex_shared_kv fused

"$PY" scripts/compare_v29_forward_rollouts.py \
  "$OUT_ROOT/baseline/report.json" \
  "$OUT_ROOT/optimized/report.json" \
  | tee "$OUT_ROOT/comparison.json"
echo "[v30-gss-forward-ab] comparison=$OUT_ROOT/comparison.json"
