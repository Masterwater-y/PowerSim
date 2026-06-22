#!/usr/bin/env bash
# monitor_train.sh — 实时观察 DDP 训练进度
# 1) 滚动 rank0 日志的关键行（step / loss / eval / DONE）
# 2) 每 30s 打一条 GPU 利用率快照（nvidia-smi 的关键列）
set -uo pipefail

ROOT=/data00/yinhaolang/LLMSim
cd "$ROOT"

PID_FILE=${PID_FILE:-logs/train_quota_balanced.pid}
RANK0_LOG=${RANK0_LOG:-logs/rank_0.log}

if [[ -f "$PID_FILE" ]]; then
  PID=$(cat "$PID_FILE")
  if kill -0 "$PID" 2>/dev/null; then
    echo "[monitor] launcher pid=$PID alive"
  else
    echo "[monitor] launcher pid=$PID NOT running (training likely finished)"
  fi
fi

if [[ ! -f "$RANK0_LOG" ]]; then
  echo "[monitor] waiting for $RANK0_LOG ..."
  while [[ ! -f "$RANK0_LOG" ]]; do sleep 1; done
fi

# 周期性 nvidia-smi 摘要，写到一个滚动文件；后台运行
GPU_STAT=logs/_gpu_status.log
(
  while true; do
    if pgrep -f train/train_lora.py >/dev/null; then
      echo "===== $(date +%H:%M:%S) ====="
      nvidia-smi --query-gpu=index,utilization.gpu,utilization.memory,memory.used \
        --format=csv,noheader,nounits 2>/dev/null \
        | awk -F',' '{printf "gpu%s util=%s%% mem=%s/%sMiB\n",$1,$2,$4,$4}'
      sleep 30
    else
      echo "[monitor] training process not detected; gpu watcher exiting"
      break
    fi
  done
) > "$GPU_STAT" 2>&1 &
GPU_PID=$!

# Ctrl-C 退出时一并清理后台 GPU watcher
trap "kill $GPU_PID 2>/dev/null || true; echo; echo '[monitor] stopped'; exit 0" INT TERM

echo "[monitor] tailing $RANK0_LOG  (Ctrl-C 退出)"
echo "[monitor] gpu summary -> $GPU_STAT"
echo "------------------------------------------------------------"

# 只显示关键行，避免被 transformer 的 attention warning 刷屏
tail -n 50 -f "$RANK0_LOG" \
  | stdbuf -oL grep -E \
      "step=|eval_loss|best|\[ddp\]|\[data\]|\[model\]|\[DONE\]|\[WALL\]|\[THROUGHPUT|saved|ERROR|Error|RuntimeError|Traceback|loss=|L_cpi|L_cycles"
