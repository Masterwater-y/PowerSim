#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TSim}
cd "$ROOT"

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
DATA=${DATA:-data/windows_v16_v9core_tail_local_all/windows.jsonl}
CACHE_PATH=${CACHE_PATH:-data/windows_v16_v9core_tail_local_all/windows.maxlen32768.tensor_cache}
OUT=${OUT:-ckpt/v26_kvqr_compat6_smoke}
DEVICE=${DEVICE:-cuda}

STEPS=${STEPS:-20}
BS=${BS:-1}
MAX_UOPS_PER_CORE=${MAX_UOPS_PER_CORE:-32768}
TRAIN_MAX_UOPS_PER_CORE=${TRAIN_MAX_UOPS_PER_CORE:-0}
TRAIN_MAX_TOTAL_UOPS=${TRAIN_MAX_TOTAL_UOPS:-8192}
N_LAYERS=${N_LAYERS:-1}
FFN_DIM=${FFN_DIM:-1280}
LENGTH_BUCKET_SIZE=${LENGTH_BUCKET_SIZE:-512}

echo "[v26-kvqr] ROOT=$ROOT"
echo "[v26-kvqr] DATA=$DATA"
echo "[v26-kvqr] CACHE_PATH=$CACHE_PATH"
echo "[v26-kvqr] OUT=$OUT"
echo "[v26-kvqr] DEVICE=$DEVICE"
echo "[v26-kvqr] schema=compat6 UOP fields, PMU=v26a_8key"
echo "[v26-kvqr] attention=doc_qkvr attention_impl=ragged_sdpa layers=$N_LAYERS ffn=$FFN_DIM"
echo "[v26-kvqr] max_uops_per_core=$MAX_UOPS_PER_CORE train_max_uops_per_core=$TRAIN_MAX_UOPS_PER_CORE train_max_total_uops=$TRAIN_MAX_TOTAL_UOPS filter_long_uops=on"
echo "[v26-kvqr] bucket_by_shape=on length_bucket_size=$LENGTH_BUCKET_SIZE"
echo "[v26-kvqr] clean 10-field schema requires rebuilding windows/cache"

"$PY" train/train_v26_kvqr.py \
  --data "$DATA" \
  --cache-path "$CACHE_PATH" \
  --out "$OUT" \
  --steps "$STEPS" \
  --bs "$BS" \
  --max-uops-per-core "$MAX_UOPS_PER_CORE" \
  --train-max-uops-per-core "$TRAIN_MAX_UOPS_PER_CORE" \
  --train-max-total-uops "$TRAIN_MAX_TOTAL_UOPS" \
  --n-layers "$N_LAYERS" \
  --ffn-dim "$FFN_DIM" \
  --length-bucket-size "$LENGTH_BUCKET_SIZE" \
  --device "$DEVICE"
