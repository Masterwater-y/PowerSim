#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
cd "$ROOT"
mkdir -p logs

MANIFEST=${MANIFEST:-data/v29_global_time_dataset/manifest.json}
CONFIG=${CONFIG:-configs/v29_latent32_scratch_60k.yaml}
OUT=${OUT:-ckpt/tcsim_v29_latent32_scratch_100m_8gpu_60k}
STEPS=${STEPS:-60000}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
NPROC=${NPROC:-8}
RESUME_CKPT=${RESUME_CKPT:-}

[[ -f "$MANIFEST" ]] || {
  echo "[v29-latent32-60k][ERROR] missing manifest: $MANIFEST" >&2
  exit 2
}
[[ -f "$CONFIG" ]] || {
  echo "[v29-latent32-60k][ERROR] missing config: $CONFIG" >&2
  exit 2
}
if [[ ! "$STEPS" =~ ^[0-9]+$ ]] || (( STEPS < 1 || STEPS > 60000 )); then
  echo "[v29-latent32-60k][ERROR] STEPS must be in [1,60000], got $STEPS" >&2
  exit 2
fi
if [[ -z "$RESUME_CKPT" && -e "$OUT/last.pt" ]]; then
  echo "[v29-latent32-60k][ERROR] refusing to overwrite existing run: $OUT" >&2
  echo "[v29-latent32-60k][ERROR] set RESUME_CKPT=$OUT/last.pt or choose another OUT" >&2
  exit 2
fi
if [[ -n "$RESUME_CKPT" && ! -f "$RESUME_CKPT" ]]; then
  echo "[v29-latent32-60k][ERROR] missing resume checkpoint: $RESUME_CKPT" >&2
  exit 2
fi

STAMP=$(date +%Y%m%d_%H%M%S)
LOG=${LOG:-logs/v29_latent32_scratch_60k_${STAMP}.nohup.log}
PID_FILE=${PID_FILE:-${LOG}.pid}

nohup env \
  ROOT="$ROOT" \
  MANIFEST="$MANIFEST" \
  CONFIG="$CONFIG" \
  OUT="$OUT" \
  STEPS="$STEPS" \
  GPUS="$GPUS" \
  NPROC="$NPROC" \
  RESUME_CKPT="$RESUME_CKPT" \
  bash scripts/run_v29_latent32_scratch_60k_ddp8.sh >"$LOG" 2>&1 &

PID=$!
printf '%s\n' "$PID" >"$PID_FILE"
echo "[v29-latent32-60k] pid=$PID"
echo "[v29-latent32-60k] log=$LOG"
echo "[v29-latent32-60k] pid_file=$PID_FILE"
echo "[v29-latent32-60k] output=$OUT"
echo "[v29-latent32-60k] milestone=step_30000.pt final=last.pt"
echo "[v29-latent32-60k] follow: tail -f $LOG"
