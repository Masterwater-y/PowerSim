#!/usr/bin/env bash
# LLMSim 8 卡 DDP 训练启动脚本（手动 spawn，规避容器内 torchrun fork+CUDA Error 304）。
#
# 关键点：
#   - 不用 torchrun 的 fork；用 shell 手动拉起 N 个独立进程。
#   - 每个进程在【import torch 之前】就通过 CUDA_VISIBLE_DEVICES 绑定单卡，
#     这样每个 rank 只初始化自己那张卡，避免多卡并发初始化触发 Error 304。
#   - 用环境变量 RANK/LOCAL_RANK/WORLD_SIZE/MASTER_ADDR/MASTER_PORT 手动构造，
#     train_lora.py 的 setup_ddp() 会识别它们走 NCCL DDP。
#   - 因为每进程 CUDA_VISIBLE_DEVICES 只有 1 张卡，进程内 local_rank 恒为 0。
#
# 用法：
#   bash scripts/launch_ddp8.sh
# 默认对齐方案A（5-head + RD/ST/SM + TQ 32768，functional-only）：
#   NPROC=8 STEPS=3000 BS=1 GRAD_ACCUM=2 MAXLEN=32768
#   LOG_EVERY=20 EVAL_EVERY=200 EVAL_BATCHES=20 VAL_FRAC=0.10
#   USE_TSTART=0 NUM_WORKERS=4
#   INIT_CKPT(默认空；非空时透传为 --init-ckpt，走续训；注意旧 ckpt 含 VL/VP
#   embedding 与新 RD/ST/SM 词表不兼容，必须留空从头训)
set -uo pipefail

PY=/data00/yinhaolang/infer/.venv/bin/python
ROOT=/data00/yinhaolang/LLMSim
cd "$ROOT"

export HF_HUB_OFFLINE=1
export TOKENIZERS_PARALLELISM=false
export OMP_NUM_THREADS=4
# NCCL：单机多卡走 P2P/共享内存，禁用网络相关探测，提升稳定性
export NCCL_P2P_DISABLE=0
export NCCL_IB_DISABLE=1
# 长序列易碎片化：开 expandable_segments 缓解
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True

NPROC=${NPROC:-8}
DATA=${DATA:-data/windows/windows.jsonl}
OUT=${OUT:-ckpt/phase0_ddp8}
STEPS=${STEPS:-3000}
BS=${BS:-1}
GRAD_ACCUM=${GRAD_ACCUM:-2}
MAXLEN=${MAXLEN:-32768}
LOG_EVERY=${LOG_EVERY:-20}
EVAL_EVERY=${EVAL_EVERY:-200}
EVAL_BATCHES=${EVAL_BATCHES:-20}
VAL_FRAC=${VAL_FRAC:-0.10}
USE_TSTART=${USE_TSTART:-0}
NUM_WORKERS=${NUM_WORKERS:-4}
MASTER_ADDR=${MASTER_ADDR:-127.0.0.1}
MASTER_PORT=${MASTER_PORT:-29577}
INIT_CKPT=${INIT_CKPT:-}

if [[ "$USE_TSTART" == "1" ]]; then
  TSTART_FLAG="--use-tstart"
else
  TSTART_FLAG=""
fi

INIT_CKPT_ARGS=()
if [[ -n "$INIT_CKPT" ]]; then
  INIT_CKPT_ARGS=(--init-ckpt "$INIT_CKPT")
fi

mkdir -p "$OUT" logs
echo "[launch] NPROC=$NPROC STEPS=$STEPS BS=$BS GRAD_ACCUM=$GRAD_ACCUM MAXLEN=$MAXLEN DATA=$DATA OUT=$OUT"
echo "[launch] LOG_EVERY=$LOG_EVERY EVAL_EVERY=$EVAL_EVERY EVAL_BATCHES=$EVAL_BATCHES VAL_FRAC=$VAL_FRAC USE_TSTART=$USE_TSTART NUM_WORKERS=$NUM_WORKERS MASTER_PORT=$MASTER_PORT"
echo "[launch] INIT_CKPT=${INIT_CKPT:-<none>}"

pids=()
for ((r=0; r<NPROC; r++)); do
  # 每个 rank 绑定第 r 张物理卡；进程内只见 1 张卡，故 LOCAL_RANK=0。
  CUDA_VISIBLE_DEVICES="$r" \
  RANK="$r" LOCAL_RANK=0 WORLD_SIZE="$NPROC" \
  MASTER_ADDR="$MASTER_ADDR" MASTER_PORT="$MASTER_PORT" \
    "$PY" train/train_lora.py \
      --data "$DATA" --out "$OUT" --steps "$STEPS" --bs "$BS" \
      --grad-accum "$GRAD_ACCUM" \
      --max-len "$MAXLEN" --log-every "$LOG_EVERY" \
      --eval-every "$EVAL_EVERY" --eval-batches "$EVAL_BATCHES" \
      --val-frac "$VAL_FRAC" --num-workers "$NUM_WORKERS" \
      $TSTART_FLAG "${INIT_CKPT_ARGS[@]}" \
      > "logs/rank_${r}.log" 2>&1 &
  pids+=($!)
done

echo "[launch] spawned ${#pids[@]} ranks, pids=${pids[*]}"
echo "[launch] rank0 日志实时输出在下方（其余见 logs/rank_*.log）："
echo "------------------------------------------------------------"

# 实时跟随 rank0 日志直到结束
tail -f logs/rank_0.log &
TAIL_PID=$!

# 等所有 rank 结束
fail=0
for p in "${pids[@]}"; do
  wait "$p" || fail=1
done
kill "$TAIL_PID" 2>/dev/null || true

echo "------------------------------------------------------------"
if [ "$fail" = "0" ]; then
  echo "[launch] ALL RANKS DONE OK"
else
  echo "[launch] SOME RANK FAILED — 检查 logs/rank_*.log"
fi

echo "=== rank0 关键结果 ==="
grep -E "\[ddp\]|\[data\]|\[model\]|\[DONE\]|\[WALL\]|\[THROUGHPUT|\[eval" logs/rank_0.log | tail -20
