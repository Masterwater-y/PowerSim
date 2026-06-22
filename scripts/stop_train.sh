#!/usr/bin/env bash
# stop_train.sh — 停止由 start_quota_balanced.sh 拉起的训练
set -uo pipefail

ROOT=/data00/yinhaolang/LLMSim
cd "$ROOT"

PID_FILE=${PID_FILE:-logs/train_quota_balanced.pid}

if [[ -f "$PID_FILE" ]]; then
  PID=$(cat "$PID_FILE")
  echo "[stop] killing launcher pid=$PID and its tree"
  if kill -0 "$PID" 2>/dev/null; then
    pkill -P "$PID" 2>/dev/null || true
    kill "$PID" 2>/dev/null || true
  fi
fi

# 兜底：杀掉所有 train_lora.py 进程
echo "[stop] killing all train_lora.py processes"
pkill -f "train/train_lora.py" 2>/dev/null || true
sleep 2
# 强制
pkill -9 -f "train/train_lora.py" 2>/dev/null || true

# 清 pid 文件
[[ -f "$PID_FILE" ]] && rm -f "$PID_FILE"
echo "[stop] done. 残留进程检查："
pgrep -af "train_lora.py" || echo "  (none)"
