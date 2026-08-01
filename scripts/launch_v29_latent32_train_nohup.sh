#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
cd "$ROOT"
mkdir -p logs

STAMP=$(date +%Y%m%d_%H%M%S)
LOG=${LOG:-logs/v29_latent32_train_${STAMP}.nohup.log}
PID_FILE=${PID_FILE:-${LOG}.pid}

nohup env \
  ROOT="$ROOT" \
  MANIFEST="${MANIFEST:-data/v29_global_time_dataset/manifest.json}" \
  CONFIG="${CONFIG:-configs/v29_latent32_scratch_100m.yaml}" \
  OUT="${OUT:-ckpt/tcsim_v29_latent32_scratch_100m_8gpu_90k}" \
  STEPS="${STEPS:-90000}" \
  GPUS="${GPUS:-0,1,2,3,4,5,6,7}" \
  NPROC="${NPROC:-8}" \
  RESUME_CKPT="${RESUME_CKPT:-}" \
  bash scripts/run_v29_latent32_ddp8.sh >"$LOG" 2>&1 &

PID=$!
printf '%s\n' "$PID" >"$PID_FILE"
echo "[v29-latent32] pid=$PID"
echo "[v29-latent32] log=$LOG"
echo "[v29-latent32] pid_file=$PID_FILE"
echo "[v29-latent32] follow: tail -f $LOG"
