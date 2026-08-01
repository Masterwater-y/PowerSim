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
LOG_DIR=${LOG_DIR:-logs/watchdog}
LOG_FILE=${LOG_FILE:-$LOG_DIR/v29_forward_ab_$RUN_STAMP.nohup.log}
PID_FILE=${PID_FILE:-$LOG_DIR/v29_forward_ab_$RUN_STAMP.pid}

[[ -x "$PY" ]] || {
  echo "[v29-forward-ab-nohup][ERROR] missing Python: $PY" >&2
  exit 2
}
[[ -f "$CKPT" ]] || {
  echo "[v29-forward-ab-nohup][ERROR] missing checkpoint: $CKPT" >&2
  exit 2
}
[[ -f "$MANIFEST" ]] || {
  echo "[v29-forward-ab-nohup][ERROR] missing manifest: $MANIFEST" >&2
  exit 2
}
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
  GSS_PMU_ONLY="$GSS_PMU_ONLY" \
  RUN_STAMP="$RUN_STAMP" \
  OUT_ROOT="$OUT_ROOT" \
  bash scripts/benchmark_v29_forward_rollout.sh \
  >"$LOG_FILE" 2>&1 </dev/null &
pid=$!
printf '%s\n' "$pid" >"$PID_FILE"

echo "[v29-forward-ab-nohup] started pid=$pid gpu=$GPU steps=$MAX_FREE_STEPS"
echo "[v29-forward-ab-nohup] workload=$WORKLOADS cores=$CORE_COUNTS stride=$TARGET_STRIDE"
echo "[v29-forward-ab-nohup] gss_pmu_only=$GSS_PMU_ONLY"
echo "[v29-forward-ab-nohup] log=$LOG_FILE"
echo "[v29-forward-ab-nohup] output=$OUT_ROOT"
echo "[v29-forward-ab-nohup] comparison=$OUT_ROOT/comparison.json"
echo "[v29-forward-ab-nohup] follow: tail -f $LOG_FILE"
echo "[v29-forward-ab-nohup] status: ps -p $pid -o pid,stat,etime,cmd"
