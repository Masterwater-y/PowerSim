#!/usr/bin/env bash
# 多卡 eval_cycles 启动脚本（手动 spawn，规避容器内 fork+CUDA Error 304）。
# 每个 rank 绑定单卡、处理 rows[rank::world] 的窗口分片，最后 all-reduce 聚合。
#
# 用法：
#   NPROC=8 BS=8 bash scripts/launch_eval_ddp.sh
# 可调环境变量：
#   NPROC(默认8) BS(默认8) MAXLEN(默认32768) MAXWIN(默认0=全量)
#   DATA CKPT  WORKLOADS(以空格分隔的 NAME:stats 列表)
set -uo pipefail

PY=/data00/yinhaolang/infer/.venv/bin/python
ROOT=/data00/yinhaolang/LLMSim
cd "$ROOT"

export HF_HUB_OFFLINE=1
export TOKENIZERS_PARALLELISM=false
export OMP_NUM_THREADS=4
export NCCL_P2P_DISABLE=0
export NCCL_IB_DISABLE=1
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True

NPROC=${NPROC:-8}
BS=${BS:-8}
MAXLEN=${MAXLEN:-32768}
MAXWIN=${MAXWIN:-0}
DATA=${DATA:-data/windows_train8_w512/windows.jsonl}
CKPT=${CKPT:-ckpt/train8_w512_embfix_v2}
MASTER_ADDR=${MASTER_ADDR:-127.0.0.1}
MASTER_PORT=${MASTER_PORT:-29588}

# 默认 4 个代表性负载（可用 WORKLOADS 环境变量覆盖）
WORKLOADS=${WORKLOADS:-"\
W_compute_int:data/raw_8w_8c_500k/W_compute_int/stats.txt \
W_chase_dram:data/raw_8w_8c_500k/W_chase_dram/stats.txt \
W_branch_storm:data/raw_fix3_8c_500k/W_branch_storm/stats.txt \
W_stream:data/raw_8w_8c_500k/W_stream/stats.txt"}

WL_ARGS=()
for w in $WORKLOADS; do
  WL_ARGS+=(--workload "$w")
done

mkdir -p logs
echo "[launch] NPROC=$NPROC BS=$BS MAXLEN=$MAXLEN MAXWIN=$MAXWIN CKPT=$CKPT"
echo "[launch] workloads: $WORKLOADS"

pids=()
for ((r=0; r<NPROC; r++)); do
  CUDA_VISIBLE_DEVICES="$r" \
  RANK="$r" LOCAL_RANK=0 WORLD_SIZE="$NPROC" \
  MASTER_ADDR="$MASTER_ADDR" MASTER_PORT="$MASTER_PORT" \
    "$PY" eval/eval_cycles.py \
      --data "$DATA" --ckpt "$CKPT" \
      --max-len "$MAXLEN" --bs "$BS" --max-windows "$MAXWIN" \
      "${WL_ARGS[@]}" \
      > "logs/eval_rank_${r}.log" 2>&1 &
  pids+=($!)
done

echo "[launch] spawned ${#pids[@]} ranks, pids=${pids[*]}"
echo "[launch] rank0 实时输出："
echo "------------------------------------------------------------"
tail -f logs/eval_rank_0.log &
TAIL_PID=$!

fail=0
for p in "${pids[@]}"; do
  wait "$p" || fail=1
done
kill "$TAIL_PID" 2>/dev/null || true

echo "------------------------------------------------------------"
if [ "$fail" = "0" ]; then
  echo "[launch] EVAL DONE OK"
else
  echo "[launch] SOME RANK FAILED — 检查 logs/eval_rank_*.log"
fi