#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

STEPS=8000
OUT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --steps)
      STEPS="${2:?missing value for --steps}"
      shift 2
      ;;
    --out)
      OUT="${2:?missing value for --out}"
      shift 2
      ;;
    -h|--help)
      echo "usage: $0 [--steps N] [--out CKPT_DIR]"
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      echo "usage: $0 [--steps N] [--out CKPT_DIR]" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$OUT" ]]; then
  OUT="ckpt/v14_headonly_qwen3_0p6b_c01_c04_c08_c16_${STEPS}"
fi

mkdir -p logs ckpt

export CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7

exec /data00/yinhaolang/infer/.venv/bin/torchrun \
  --standalone \
  --nproc_per_node=8 \
  train/train_lora.py \
  --data data/windows_v12_summary_tq_train600_seedA_c01_c04_c08_c16/windows.jsonl \
  --cache-path data/windows_v12_summary_tq_train600_seedA_c01_c04_c08_c16/windows.maxlen32768.tensor_cache \
  --out "$OUT" \
  --base-model Qwen/Qwen3-0.6B-Base \
  --steps "$STEPS" \
  --bs 1 \
  --grad-accum 1 \
  --max-len 32768 \
  --val-frac 0.15 \
  --log-every 20 \
  --eval-every 500 \
  --eval-batches 0 \
  --num-workers 2 \
  --loss-keys cpi_uop,branch_miss \
  --cycles-loss-mode window
