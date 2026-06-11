#!/usr/bin/env bash
# 全量训练入口：timing-functional 50M + fetch decomposition labels
# 默认配置面向 4 卡 H20：
#   GPU=0,1,2,3; global BS=16384; workers=12; ctx=128; bf16; steps=50000
#
# 常用启动：
#   bash run_train_fetchdecomp.sh
#   mkdir -p ckpt/exp_tf50m_fetchdecomp_bs16384_w12_4gpu
#   nohup bash run_train_fetchdecomp.sh \
#     > ckpt/exp_tf50m_fetchdecomp_bs16384_w12_4gpu/train.nohup.log 2>&1 &
#
# 覆盖参数：
#   STEPS=5000 VAL_MAX_BATCHES=200 bash run_train_fetchdecomp.sh
#   DATA=/path/to/split SAVE_DIR=/path/to/ckpt bash run_train_fetchdecomp.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

DEFAULT_DATA="${TAO_DATAGEN_ROOT:-${TAO_ROOT}/datagen}/tmp/timing_functional_50m_fetchdecomp_20260606_191529/final_balanced_50000000_pq_split_95_5"
if [[ ! -d "$DEFAULT_DATA/train" || ! -d "$DEFAULT_DATA/val" ]]; then
  echo "[FATAL] DEFAULT_DATA is not a valid train/val split: $DEFAULT_DATA" >&2
  exit 2
fi

DATA="${DATA:-$DEFAULT_DATA}"
SAVE_DIR="${SAVE_DIR:-$HERE/ckpt/exp_tf50m_fetchdecomp_bs16384_w12_4gpu}"
TORCHRUN_BIN="${TORCHRUN_BIN:-/root/miniconda3/envs/yinhaolang/bin/torchrun}"
PYTHON_BIN="${PYTHON_BIN:-/root/miniconda3/envs/yinhaolang/bin/python}"

export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0,1,2,3}"
if [[ -n "${CUDA_VISIBLE_DEVICES:-}" ]]; then
  IFS=',' read -r -a GPU_IDS <<< "$CUDA_VISIBLE_DEVICES"
  NPROC="${NPROC:-${#GPU_IDS[@]}}"
else
  NPROC="${NPROC:-$($PYTHON_BIN - <<'PY'
import torch
n = torch.cuda.device_count()
print(max(min(n, 8), 1))
PY
)}"
fi

THREADS_PER_RANK="${THREADS_PER_RANK:-$(( 32 / NPROC > 0 ? 32 / NPROC : 1 ))}"
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-$THREADS_PER_RANK}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-$THREADS_PER_RANK}"
export PYTHONUNBUFFERED=1

BS="${BS:-16384}"
STEPS="${STEPS:-50000}"
LR="${LR:-3e-4}"
WARMUP="${WARMUP:-2000}"
WORKERS="${WORKERS:-12}"
VAL_WORKERS="${VAL_WORKERS:-12}"
SAVE_EVERY="${SAVE_EVERY:-1000}"
KEEP_LAST="${KEEP_LAST:-10}"
LOG_EVERY="${LOG_EVERY:-100}"
VAL_EVERY="${VAL_EVERY:-2000}"
VAL_MAX_BATCHES="${VAL_MAX_BATCHES:-0}"

mkdir -p "$SAVE_DIR"

cd "$HERE"
exec "$TORCHRUN_BIN" --standalone --nnodes=1 --nproc_per_node="$NPROC" -m ml.train \
  --data       "$DATA" \
  --ctx        128 \
  --bs         "$BS" \
  --steps      "$STEPS" \
  --lr         "$LR" \
  --warmup     "$WARMUP" \
  --workers    "$WORKERS" \
  --val-workers "$VAL_WORKERS" \
  --val-every  "$VAL_EVERY" \
  --val-max-batches "$VAL_MAX_BATCHES" \
  --bf16 \
  --save       "$SAVE_DIR/tao_fetchdecomp_v10_3_ma16.pt" \
  --save-every "$SAVE_EVERY" \
  --keep-last  "$KEEP_LAST" \
  --log-every  "$LOG_EVERY" \
  "$@"
