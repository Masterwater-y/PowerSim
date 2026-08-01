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
LOG_DIR=${LOG_DIR:-logs/watchdog}
LOG_FILE=${LOG_FILE:-$LOG_DIR/v30_gss_forward_ab_$RUN_STAMP.nohup.log}
PID_FILE=${PID_FILE:-$LOG_DIR/v30_gss_forward_ab_$RUN_STAMP.pid}

[[ -x "$PY" ]] || { echo "[v30-gss-forward-ab-nohup][ERROR] missing Python: $PY" >&2; exit 2; }
[[ -f "$CKPT" ]] || { echo "[v30-gss-forward-ab-nohup][ERROR] missing checkpoint: $CKPT" >&2; exit 2; }
[[ -f "$MANIFEST" ]] || { echo "[v30-gss-forward-ab-nohup][ERROR] missing manifest: $MANIFEST" >&2; exit 2; }
mkdir -p "$LOG_DIR" "$OUT_ROOT"

nohup env \
  ROOT="$ROOT" \
  PY="$PY" \
  GPU="$GPU" \
  CKPT="$CKPT" \
  MANIFEST="$MANIFEST" \
  SPLITS="$SPLITS" \
  CORE_COUNTS="$CORE_COUNTS" \
  WORKLOADS="$WORKLOADS" \
  MAX_FREE_STEPS="$MAX_FREE_STEPS" \
  TARGET_STRIDE="$TARGET_STRIDE" \
  TCSIM_GSS_BACKEND="$TCSIM_GSS_BACKEND" \
  RUN_STAMP="$RUN_STAMP" \
  OUT_ROOT="$OUT_ROOT" \
  bash scripts/benchmark_v30_gss_forward_rollout.sh \
  >"$LOG_FILE" 2>&1 </dev/null &
pid=$!
printf '%s\n' "$pid" >"$PID_FILE"

echo "[v30-gss-forward-ab-nohup] started pid=$pid gpu=$GPU steps=$MAX_FREE_STEPS"
echo "[v30-gss-forward-ab-nohup] workload=$WORKLOADS cores=$CORE_COUNTS stride=$TARGET_STRIDE"
echo "[v30-gss-forward-ab-nohup] checkpoint=$CKPT"
echo "[v30-gss-forward-ab-nohup] GSS=online-timing+canonical-PMU backend=$TCSIM_GSS_BACKEND"
echo "[v30-gss-forward-ab-nohup] log=$LOG_FILE"
echo "[v30-gss-forward-ab-nohup] output=$OUT_ROOT"
echo "[v30-gss-forward-ab-nohup] comparison=$OUT_ROOT/comparison.json"
echo "[v30-gss-forward-ab-nohup] follow: tail -f $LOG_FILE"
echo "[v30-gss-forward-ab-nohup] status: ps -p $pid -o pid,stat,etime,cmd"
