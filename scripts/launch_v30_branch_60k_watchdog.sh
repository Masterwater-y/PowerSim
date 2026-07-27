#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
cd "$ROOT"
mkdir -p logs/watchdog

variant=$(printf '%s' "${VARIANT:-b1}" | tr '[:upper:]' '[:lower:]')
case "$variant" in
  b1)
    default_config=configs/v30_branch_b1_headless_100m.yaml
    default_out=ckpt/tcsim_v30_branch_b1_headless_100m_8gpu_60000
    ;;
  b2)
    default_config=configs/v30_branch_b2_replay_event_100m.yaml
    default_out=ckpt/tcsim_v30_branch_b2_replay_event_100m_8gpu_60000
    ;;
  b3)
    default_config=configs/v30_branch_b3_replay_history_100m.yaml
    default_out=ckpt/tcsim_v30_branch_b3_replay_history_100m_8gpu_60000
    ;;
  *)
    echo "[v30-branch-watch][ERROR] VARIANT must be b1, b2, or b3: $variant" >&2
    exit 2
    ;;
esac

MANIFEST=${MANIFEST:-data/v30_branch_replay_dataset/manifest.json}
CONFIG=${CONFIG:-$default_config}
OUT=${OUT:-$default_out}
RUN_NAME=${RUN_NAME:-tcsim_v30_branch_${variant}_100m_8gpu_60000_watch}
GPU_LIST=${GPUS:-0,1,2,3,4,5,6,7}
NPROC_VALUE=${NPROC:-8}

for required in "$MANIFEST" "$CONFIG"; do
  [[ -f "$required" ]] || {
    echo "[v30-branch-watch][ERROR] missing required file: $required" >&2
    exit 2
  }
done
[[ -f data/v30_branch_replay_dataset/build_report.json ]] || {
  echo "[v30-branch-watch][ERROR] missing v30 branch-cache build report" >&2
  exit 2
}
if ! command -v nvidia-smi >/dev/null 2>&1 || ! nvidia-smi -L >/dev/null 2>&1; then
  echo "[v30-branch-watch][ERROR] NVIDIA driver/GPU is not visible" >&2
  exit 3
fi
IFS=',' read -r -a GPU_ITEMS <<< "$GPU_LIST"
if (( ${#GPU_ITEMS[@]} != NPROC_VALUE )); then
  echo "[v30-branch-watch][ERROR] GPUS count != NPROC: $GPU_LIST vs $NPROC_VALUE" >&2
  exit 3
fi

nohup env \
  ROOT="$ROOT" \
  VARIANT="$variant" \
  RUN_NAME="$RUN_NAME" \
  TRAIN_SCRIPT=scripts/run_v30_branch_ddp8.sh \
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

echo "started: variant=$variant watchdog_pid=$!"
echo "train log: logs/${RUN_NAME}.current.log"
echo "watchdog log: logs/watchdog/${RUN_NAME}.nohup.log"
echo "checkpoint: $OUT/{last.pt,best.pt,best_post_coverage.pt}"
