#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
cd "$ROOT"
STEPS=${STEPS:-100}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
NPROC=${NPROC:-8}
STAMP=${RUN_STAMP:-$(date +%Y%m%d_%H%M%S)}
V29_OUT=${V29_OUT:-/tmp/tcsim_v29_speed_${STAMP}}
V30_OUT=${V30_OUT:-/tmp/tcsim_v30_exposure_speed_${STAMP}}

echo "[speed] v29 baseline steps=$STEPS"
MANIFEST=data/v29_global_time_dataset/manifest.json \
CONFIG=configs/v29_speed_smoke.yaml \
OUT="$V29_OUT" STEPS="$STEPS" GPUS="$GPUS" NPROC="$NPROC" \
PROFILE_ATTENTION=0 bash scripts/run_v29_ddp8.sh

echo "[speed] v30 Exposure-v1 full-backbone steps=$STEPS"
MANIFEST=data/v30_exposure_v1_dataset/manifest.json \
CONFIG=configs/v30_gss_exposure_v1_speed_smoke.yaml \
OUT="$V30_OUT" STEPS="$STEPS" GPUS="$GPUS" NPROC="$NPROC" \
PROFILE_ATTENTION=0 \
INIT_CHECKPOINT=ckpt/tcsim_v29_packed3_100m_8gpu_60k/best.pt \
bash scripts/run_v29_ddp8.sh

echo "[speed] outputs v29=$V29_OUT v30=$V30_OUT"
