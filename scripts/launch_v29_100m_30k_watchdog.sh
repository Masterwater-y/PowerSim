#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
cd "$ROOT"
mkdir -p logs/watchdog

RUN_NAME=${RUN_NAME:-tcsim_v29_global_time_100m_8gpu_30000_watch}
OUT=${OUT:-ckpt/tcsim_v29_global_time_100m_8gpu_30000}
MANIFEST=${MANIFEST:-data/v29_global_time_dataset/manifest.json}
CONFIG=${CONFIG:-configs/v29_100m.yaml}

nohup env \
  ROOT="$ROOT" \
  RUN_NAME="$RUN_NAME" \
  TRAIN_SCRIPT=scripts/run_v29_ddp8.sh \
  MANIFEST="$MANIFEST" \
  CONFIG="$CONFIG" \
  OUT="$OUT" \
  TARGET_STEPS=${TARGET_STEPS:-30000} \
  GPUS=${GPUS:-0,1,2,3,4,5,6,7} \
  NPROC=${NPROC:-8} \
  SDPA_BACKEND=${SDPA_BACKEND:-auto} \
  AMP_DTYPE=${AMP_DTYPE:-bf16} \
  PROFILE_ATTENTION=${PROFILE_ATTENTION:-1} \
  MONITOR_INTERVAL=${MONITOR_INTERVAL:-60} \
  STALL_SECONDS=${STALL_SECONDS:-3600} \
  MAX_RESTARTS=${MAX_RESTARTS:-20} \
  OMP_NUM_THREADS=${OMP_NUM_THREADS:-4} \
  NCCL_P2P_DISABLE=${NCCL_P2P_DISABLE:-0} \
  NCCL_IB_DISABLE=${NCCL_IB_DISABLE:-1} \
  PYTORCH_CUDA_ALLOC_CONF=${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True} \
  bash scripts/watch_mvp_train.sh \
  > "logs/watchdog/${RUN_NAME}.nohup.log" 2>&1 &

echo "started: $RUN_NAME pid=$!"
echo "train log: logs/${RUN_NAME}.current.log"
echo "watchdog log: logs/watchdog/${RUN_NAME}.nohup.log"
