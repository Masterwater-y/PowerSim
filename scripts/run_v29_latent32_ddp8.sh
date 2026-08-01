#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
cd "$ROOT"

MANIFEST=${MANIFEST:-data/v29_global_time_dataset/manifest.json}
CONFIG=${CONFIG:-configs/v29_latent32_scratch_100m.yaml}
OUT=${OUT:-ckpt/tcsim_v29_latent32_scratch_100m_8gpu_90k}
STEPS=${STEPS:-90000}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
NPROC=${NPROC:-8}

[[ -f "$MANIFEST" ]] || {
  echo "[v29-latent32][ERROR] missing manifest: $MANIFEST" >&2
  exit 2
}
[[ -f "$CONFIG" ]] || {
  echo "[v29-latent32][ERROR] missing config: $CONFIG" >&2
  exit 2
}

if [[ -n "${RESUME_CKPT:-}" ]]; then
  [[ -f "$RESUME_CKPT" ]] || {
    echo "[v29-latent32][ERROR] missing resume checkpoint: $RESUME_CKPT" >&2
    exit 2
  }
  echo "[v29-latent32] resume=$RESUME_CKPT"
  RESUME_CKPT="$RESUME_CKPT" \
  INIT_CHECKPOINT= \
  MANIFEST="$MANIFEST" CONFIG="$CONFIG" OUT="$OUT" STEPS="$STEPS" \
  GPUS="$GPUS" NPROC="$NPROC" PROFILE_ATTENTION=0 \
    bash scripts/run_v29_ddp8.sh
else
  echo "[v29-latent32] initialization=random all_parameters=active"
  RESUME_CKPT= \
  INIT_CHECKPOINT= \
  MANIFEST="$MANIFEST" CONFIG="$CONFIG" OUT="$OUT" STEPS="$STEPS" \
  GPUS="$GPUS" NPROC="$NPROC" PROFILE_ATTENTION=0 \
    bash scripts/run_v29_ddp8.sh
fi
