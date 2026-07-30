#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
cd "$ROOT"
mkdir -p logs/watchdog

RUN_NAME=${RUN_NAME:-tcsim_v30_gss_exposure_v1_60k_seed1234_watch}
OUT=${OUT:-ckpt/tcsim_v30_gss_exposure_v1_60k_seed1234}
MANIFEST=${MANIFEST:-data/v30_exposure_v1_dataset/manifest.json}
CONFIG=${CONFIG:-configs/v30_gss_exposure_v1_60k.yaml}
V29_CHECKPOINT=${V29_CHECKPOINT:-ckpt/tcsim_v29_packed3_100m_8gpu_60k/best.pt}
GPU_LIST=${GPUS:-0,1,2,3,4,5,6,7}
NPROC_VALUE=${NPROC:-8}

for required in "$MANIFEST" "$CONFIG" "$V29_CHECKPOINT"; do
  [[ -f "$required" ]] || {
    echo "[v30-exposure][ERROR] missing required file: $required" >&2
    exit 2
  }
done
if ! command -v nvidia-smi >/dev/null 2>&1 || ! nvidia-smi -L >/dev/null 2>&1; then
  echo "[v30-exposure][ERROR] NVIDIA driver/GPU is not visible" >&2
  exit 3
fi
IFS=',' read -r -a GPU_ITEMS <<< "$GPU_LIST"
if (( ${#GPU_ITEMS[@]} != NPROC_VALUE )); then
  echo "[v30-exposure][ERROR] GPUS count != NPROC: $GPU_LIST vs $NPROC_VALUE" >&2
  exit 3
fi

nohup setsid env \
  ROOT="$ROOT" \
  RUN_NAME="$RUN_NAME" \
  TRAIN_SCRIPT=scripts/run_v30_gss_exposure_v1_ddp8.sh \
  MANIFEST="$MANIFEST" \
  CONFIG="$CONFIG" \
  V29_CHECKPOINT="$V29_CHECKPOINT" \
  OUT="$OUT" \
  TARGET_STEPS=${TARGET_STEPS:-60000} \
  GPUS="$GPU_LIST" \
  NPROC="$NPROC_VALUE" \
  SDPA_BACKEND=${SDPA_BACKEND:-auto} \
  AMP_DTYPE=${AMP_DTYPE:-bf16} \
  PROFILE_ATTENTION=${PROFILE_ATTENTION:-0} \
  MONITOR_INTERVAL=${MONITOR_INTERVAL:-60} \
  STALL_SECONDS=${STALL_SECONDS:-3600} \
  MAX_RESTARTS=${MAX_RESTARTS:-20} \
  NO_PROGRESS_LIMIT=${NO_PROGRESS_LIMIT:-3} \
  OMP_NUM_THREADS=${OMP_NUM_THREADS:-4} \
  NCCL_P2P_DISABLE=${NCCL_P2P_DISABLE:-0} \
  NCCL_IB_DISABLE=${NCCL_IB_DISABLE:-1} \
  PYTORCH_CUDA_ALLOC_CONF=${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True} \
  bash scripts/watch_mvp_train.sh \
  < /dev/null \
  > "logs/watchdog/${RUN_NAME}.nohup.log" 2>&1 &

echo "started: $RUN_NAME watchdog_pid=$!"
echo "train log: logs/${RUN_NAME}.current.log"
echo "watchdog log: logs/watchdog/${RUN_NAME}.nohup.log"
echo "checkpoint: $OUT/{best.pt,best_post_coverage.pt,last.pt,step_52920.pt,step_60000.pt}"
