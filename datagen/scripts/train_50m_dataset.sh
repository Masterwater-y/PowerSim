#!/usr/bin/env bash
# 训练 50M W11-W15 balanced parquet 的推荐脚本。
#
# 用法：
#   bash scripts/train_50m_dataset.sh
#   DATA=/path/to/final_balanced_50000000_pq bash scripts/train_50m_dataset.sh
#   STEPS=50000 BS=256 LR=2e-4 bash scripts/train_50m_dataset.sh
#   RESUME=/path/to/ckpt.pt bash scripts/train_50m_dataset.sh
#
# 说明：
# - 默认使用 ${TAO_DATAGEN_ROOT:-${TAO_ROOT}/datagen}/tmp/06031920/final_balanced_50000000_pq
# - 训练入口是 ml/train.py，已启用：
#   1) is_fetch_group_head 作为分类目标
#   2) head-gated fetch latency 训练
#   3) is_macro_head / uop_pos_in_macro 结构特征（由 dataset.py 在线派生）
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="${DATA:-${TAO_DATAGEN_ROOT:-${TAO_ROOT}/datagen}/tmp/06031920/final_balanced_50000000_pq}"
PYBIN="${PYBIN:-${PYTHON:-python3}}"

BS="${BS:-256}"
CTX="${CTX:-128}"
STEPS="${STEPS:-30000}"
LR="${LR:-3e-4}"
WD="${WD:-0.01}"
WARMUP="${WARMUP:-1500}"
WORKERS="${WORKERS:-8}"
NUM_THREADS="${NUM_THREADS:-32}"
LOG_EVERY="${LOG_EVERY:-50}"
SAVE_EVERY="${SAVE_EVERY:-1000}"
KEEP_LAST="${KEEP_LAST:-5}"
RESUME="${RESUME:-}"

cd "$REPO"

if [[ ! -d "$DATA" ]]; then
  echo "[train50m] ERROR: 数据目录不存在: $DATA" >&2
  exit 2
fi

TS="$(date +%Y%m%d_%H%M%S)"
CKPT_DIR="${CKPT_DIR:-$REPO/tmp/ckpt_50m}"
mkdir -p "$CKPT_DIR"
CKPT="${CKPT_DIR}/tao50m_${TS}.pt"

RESUME_DESC="(从头训)"
RESUME_ARGS=()
if [[ -n "$RESUME" ]]; then
  if [[ ! -f "$RESUME" ]]; then
    echo "[train50m] ERROR: RESUME ckpt 不存在: $RESUME" >&2
    exit 2
  fi
  RESUME_DESC="$RESUME"
  RESUME_ARGS=(--resume "$RESUME")
fi

echo "============================================================"
echo "[train50m] data       = $DATA"
echo "[train50m] steps      = $STEPS"
echo "[train50m] bs/ctx     = $BS / $CTX"
echo "[train50m] lr/wd      = $LR / $WD"
echo "[train50m] warmup     = $WARMUP"
echo "[train50m] workers    = $WORKERS"
echo "[train50m] threads    = $NUM_THREADS"
echo "[train50m] log_every  = $LOG_EVERY"
echo "[train50m] save_every = $SAVE_EVERY"
echo "[train50m] keep_last  = $KEEP_LAST"
echo "[train50m] resume     = $RESUME_DESC"
echo "[train50m] ckpt       = $CKPT"
echo "[train50m] log        = ${CKPT%.pt}.log"
echo "[train50m] status     = ${CKPT%.pt}.status.json"
echo "============================================================"

exec numactl --cpunodebind=0 --membind=0 \
  stdbuf -oL -eL \
  "$PYBIN" -u -m ml.train \
    --data "$DATA" \
    --bs "$BS" \
    --ctx "$CTX" \
    --steps "$STEPS" \
    --lr "$LR" \
    --wd "$WD" \
    --warmup "$WARMUP" \
    --workers "$WORKERS" \
    --log-every "$LOG_EVERY" \
    --num-threads "$NUM_THREADS" \
    --save "$CKPT" \
    --save-every "$SAVE_EVERY" \
    --keep-last "$KEEP_LAST" \
    ${RESUME_ARGS[@]+"${RESUME_ARGS[@]}"}
