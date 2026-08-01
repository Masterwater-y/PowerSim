#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
cd "$ROOT"

MANIFEST=${MANIFEST:-data/v29_global_time_dataset/manifest.json}
CONFIG=${CONFIG:-configs/v29_latent32_scratch_60k.yaml}
OUT=${OUT:-ckpt/tcsim_v29_latent32_scratch_100m_8gpu_60k}
STEPS=${STEPS:-60000}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
NPROC=${NPROC:-8}

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

if [[ -z "${RESUME_CKPT:-}" && -e "$OUT/last.pt" ]]; then
  echo "[v29-latent32-60k][ERROR] refusing to overwrite existing run: $OUT" >&2
  echo "[v29-latent32-60k][ERROR] set RESUME_CKPT=$OUT/last.pt or choose another OUT" >&2
  exit 2
fi

echo "[v29-latent32-60k] run_target=$STEPS schedule_end=60000 milestone=30000"
CONFIG="$CONFIG" OUT="$OUT" STEPS="$STEPS" \
MANIFEST="$MANIFEST" GPUS="$GPUS" NPROC="$NPROC" \
RESUME_CKPT="${RESUME_CKPT:-}" \
  bash scripts/run_v29_latent32_ddp8.sh
