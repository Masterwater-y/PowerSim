#!/usr/bin/env bash
set -euo pipefail

cd ${TAO_TRAIN_ROOT:-${TAO_ROOT}/train}

export CUDA_VISIBLE_DEVICES=0
export OMP_NUM_THREADS=8
export MKL_NUM_THREADS=8
export PYTHONUNBUFFERED=1

PY=/root/miniconda3/envs/yinhaolang/bin/python
CKPT=${TAO_CKPT_ROOT:-${TAO_ROOT}/ckpt}/tao_v10_3_ma16.best.pt
VAL=${TAO_DATAGEN_ROOT:-${TAO_ROOT}/datagen}/tmp/06031920/final_balanced_50000000_pq_split_95_5/val
OUT=${TAO_CKPT_ROOT:-${TAO_ROOT}/ckpt}/tao_v10_3_ma16.best.full_val.eval.json

$PY -m ml.eval \
  --ckpt "$CKPT" \
  --data "$VAL" \
  --sample-size 2747520 \
  --batch-size 4096 \
  --workers 8 \
  --device cuda:0 | tee "$OUT"
