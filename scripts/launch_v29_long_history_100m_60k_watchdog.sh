#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
cd "$ROOT"
mkdir -p logs/watchdog

RUN_NAME=${RUN_NAME:-tcsim_v29_long_history_100m_8gpu_60000_watch}
OUT=${OUT:-ckpt/tcsim_v29_long_history_100m_8gpu_60000}
MANIFEST=${MANIFEST:-data/v29_long_history_dataset/manifest.json}
CONFIG=${CONFIG:-configs/v29_long_history_100m.yaml}
GPU_LIST=${GPUS:-0,1,2,3,4,5,6,7}
NPROC_VALUE=${NPROC:-8}

[[ -f "$MANIFEST" ]] || {
  echo "[v29-long-watch][ERROR] missing cache manifest: $MANIFEST" >&2
  echo "build it first: bash scripts/build_v29_long_history_cache.sh" >&2
  exit 2
}
[[ -f "$CONFIG" ]] || {
  echo "[v29-long-watch][ERROR] missing config: $CONFIG" >&2
  exit 2
}
if ! command -v nvidia-smi >/dev/null 2>&1 || ! nvidia-smi -L >/dev/null 2>&1; then
  echo "[v29-long-watch][ERROR] NVIDIA driver/GPU is not visible; training was not started" >&2
  exit 3
fi
IFS=',' read -r -a GPU_ITEMS <<< "$GPU_LIST"
if (( ${#GPU_ITEMS[@]} != NPROC_VALUE )); then
  echo "[v29-long-watch][ERROR] GPUS count != NPROC: $GPU_LIST vs $NPROC_VALUE" >&2
  exit 3
fi

nohup env \
  ROOT="$ROOT" \
  RUN_NAME="$RUN_NAME" \
  TRAIN_SCRIPT=scripts/run_v29_long_history_ddp8.sh \
  MANIFEST="$MANIFEST" \
  CONFIG="$CONFIG" \
  OUT="$OUT" \
  TARGET_STEPS=${TARGET_STEPS:-60000} \
  GPUS="$GPU_LIST" \
  NPROC="$NPROC_VALUE" \
  SDPA_BACKEND=${SDPA_BACKEND:-auto} \
  AMP_DTYPE=${AMP_DTYPE:-bf16} \
  PROFILE_ATTENTION=${PROFILE_ATTENTION:-1} \
  MONITOR_INTERVAL=${MONITOR_INTERVAL:-60} \
  STALL_SECONDS=${STALL_SECONDS:-3600} \
  MAX_RESTARTS=${MAX_RESTARTS:-20} \
  NO_PROGRESS_LIMIT=${NO_PROGRESS_LIMIT:-3} \
  OMP_NUM_THREADS=${OMP_NUM_THREADS:-4} \
  NCCL_P2P_DISABLE=${NCCL_P2P_DISABLE:-0} \
  NCCL_IB_DISABLE=${NCCL_IB_DISABLE:-1} \
  PYTORCH_CUDA_ALLOC_CONF=${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True} \
  bash scripts/watch_mvp_train.sh \
  > "logs/watchdog/${RUN_NAME}.nohup.log" 2>&1 &

echo "started: $RUN_NAME watchdog_pid=$!"
echo "train log: logs/${RUN_NAME}.current.log"
echo "watchdog log: logs/watchdog/${RUN_NAME}.nohup.log"
echo "checkpoint: $OUT/{last.pt,best.pt}"
