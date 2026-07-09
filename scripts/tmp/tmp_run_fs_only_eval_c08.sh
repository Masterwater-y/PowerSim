#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TSim}
cd "$ROOT"

mkdir -p logs/watchdog

CKPT=ckpt/v26_fs_only_l8_bs8_t32768_8k
RAW=data/raw_trace_pool/activecore_eval/c08_seedB_infer17
WORKLOADS=W_false_sharing

env CKPT="$CKPT" RAW="$RAW" WORKLOADS="$WORKLOADS" GPUS=0 TAG=fs_only_pred \
  DEVICE=cuda PLANNER_STATE_SOURCE=pred \
  nohup bash scripts/eval_parallel.sh \
  > logs/watchdog/fs_only_pred.log 2>&1 &

env CKPT="$CKPT" RAW="$RAW" WORKLOADS="$WORKLOADS" GPUS=1 TAG=fs_only_label \
  DEVICE=cuda PLANNER_STATE_SOURCE=label \
  nohup bash scripts/eval_parallel.sh \
  > logs/watchdog/fs_only_label.log 2>&1 &

echo "started pred+label eval"
echo "logs:"
echo "  logs/watchdog/fs_only_pred.log"
echo "  logs/watchdog/fs_only_label.log"
