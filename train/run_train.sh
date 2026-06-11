#!/usr/bin/env bash
# 全量训练入口（默认 8 卡 DDP，v10_3_fetchdecomp_soft15 训练口径）
# 使用：
#   bash run_train.sh
#   DATA=/path/to/split BS=32768 WORKERS=16 CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7 bash run_train.sh
#   bash run_train.sh [可选参数会继续透传给 ml.train]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_DATA="${TAO_DATAGEN_ROOT:-${TAO_ROOT}/datagen}/tmp/timing_functional_50m_fetchdecomp_20260606_191529/final_balanced_50000000_pq_split_95_5"
if [[ ! -d "$DEFAULT_DATA/train" || ! -d "$DEFAULT_DATA/val" ]]; then
  DEFAULT_DATA="$HERE/data/final_balanced_50000000_pq_dedup_v10_3_ma16"
fi
DATA="${DATA:-$DEFAULT_DATA}"
SAVE_DIR="${SAVE_DIR:-$HERE/ckpt/exp_tf50m_v10_3_fetchdecomp_soft15_bs32768_w16_8gpu}"
TORCHRUN_BIN="${TORCHRUN_BIN:-/root/miniconda3/envs/yinhaolang/bin/torchrun}"
PYTHON_BIN="${PYTHON_BIN:-/root/miniconda3/envs/yinhaolang/bin/python}"
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0,1,2,3,4,5,6,7}"
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
BS="${BS:-32768}"
STEPS="${STEPS:-50000}"
LR="${LR:-3e-4}"
WARMUP="${WARMUP:-2000}"
WORKERS="${WORKERS:-16}"
VAL_WORKERS="${VAL_WORKERS:-16}"
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
  --save       "$SAVE_DIR/tao_v10_3_ma16.pt" \
  --save-every "$SAVE_EVERY" \
  --keep-last  "$KEEP_LAST" \
  --log-every  "$LOG_EVERY" \
  "$@"
