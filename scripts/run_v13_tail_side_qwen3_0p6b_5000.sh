#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p logs ckpt

export CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7

exec /data00/yinhaolang/infer/.venv/bin/torchrun \
  --standalone \
  --nproc_per_node=8 \
  train/train_lora.py \
  --data data/windows_v12_summary_tq_train600_seedA_c01_c04_c08_c16/windows.jsonl \
  --cache-path data/windows_v12_summary_tq_train600_seedA_c01_c04_c08_c16/windows.maxlen32768.tensor_cache \
  --out ckpt/v13_tail_side_qwen3_0p6b_c01_c04_c08_c16_5000 \
  --base-model Qwen/Qwen3-0.6B-Base \
  --steps 5000 \
  --bs 1 \
  --grad-accum 1 \
  --max-len 32768 \
  --val-frac 0.15 \
  --log-every 20 \
  --eval-every 500 \
  --eval-batches 0 \
  --num-workers 2 \
  --tail-cpi-loss \
  --tail-q 0.80 \
  --tail-tau 0.4 \
  --tail-under-lambda 0.25 \
  --tail-low-over-lambda 0.25 \
  --tail-low-over-margin-frac 0.05
