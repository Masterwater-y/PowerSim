#!/usr/bin/env bash
# start_quota_balanced.sh — 用 nohup 后台拉起 8 卡训练（quota balanced 数据集）
# 配合 monitor_train.sh 实时查看进度
set -uo pipefail

ROOT=/data00/yinhaolang/LLMSim
cd "$ROOT"

DATA=${DATA:-data/windows_quota_maxlen32768_balanced/windows.jsonl}
OUT=${OUT:-ckpt/quota_32k_balanced_v1}
STEPS=${STEPS:-3000}
BS=${BS:-1}
GRAD_ACCUM=${GRAD_ACCUM:-2}
MAXLEN=${MAXLEN:-32768}
LOG_EVERY=${LOG_EVERY:-10}
EVAL_EVERY=${EVAL_EVERY:-100}
EVAL_BATCHES=${EVAL_BATCHES:-20}
VAL_FRAC=${VAL_FRAC:-0.10}
USE_TSTART=${USE_TSTART:-1}
NUM_WORKERS=${NUM_WORKERS:-4}

TS=$(date +%Y%m%d_%H%M%S)
RUN_LOG="logs/train_quota_balanced_${TS}.log"
PID_FILE="logs/train_quota_balanced.pid"

mkdir -p logs "$OUT"

echo "[start] launching 8-rank DDP training (nohup)..."
echo "[start] data    = $DATA"
echo "[start] out     = $OUT"
echo "[start] steps   = $STEPS  bs=$BS  grad-accum=$GRAD_ACCUM  max-len=$MAXLEN  workers=$NUM_WORKERS"
echo "[start] log     = $RUN_LOG"
echo "[start] pidfile = $PID_FILE"

# 把所有训练参数透传给 launch_ddp8.sh
nohup env \
  DATA="$DATA" OUT="$OUT" STEPS="$STEPS" BS="$BS" \
  GRAD_ACCUM="$GRAD_ACCUM" MAXLEN="$MAXLEN" \
  LOG_EVERY="$LOG_EVERY" EVAL_EVERY="$EVAL_EVERY" \
  EVAL_BATCHES="$EVAL_BATCHES" VAL_FRAC="$VAL_FRAC" \
  USE_TSTART="$USE_TSTART" NUM_WORKERS="$NUM_WORKERS" \
  bash scripts/launch_ddp8.sh \
  > "$RUN_LOG" 2>&1 &

LAUNCHER_PID=$!
echo "$LAUNCHER_PID" > "$PID_FILE"

echo "[start] launcher pid = $LAUNCHER_PID"
echo ""
echo "查看实时进度："
echo "  bash scripts/monitor_train.sh"
echo "  # 或直接："
echo "  tail -f logs/rank_0.log"
echo ""
echo "停止训练："
echo "  bash scripts/stop_train.sh"
