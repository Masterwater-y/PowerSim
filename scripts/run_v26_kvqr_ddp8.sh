#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TSim}
cd "$ROOT"

TORCHRUN=${TORCHRUN:-/data00/yinhaolang/infer/.venv/bin/torchrun}
DATA=${DATA:-data/windows_v26_clean14_tail_local_all/windows.jsonl}
CACHE_PATH=${CACHE_PATH:-data/windows_v26_clean14_tail_local_all/windows.maxlen32768.tensor_cache}
OUT=${OUT:-ckpt/v26_kvqr_clean14_8gpu_20000}

STEPS=${STEPS:-20000}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
NPROC=${NPROC:-8}
BS=${BS:-1}
LR=${LR:-3e-4}
WEIGHT_DECAY=${WEIGHT_DECAY:-0.05}
MAX_LEN=${MAX_LEN:-32768}
MAX_UOPS_PER_CORE=${MAX_UOPS_PER_CORE:-32768}
TRAIN_MAX_UOPS_PER_CORE=${TRAIN_MAX_UOPS_PER_CORE:-0}
TRAIN_MAX_TOTAL_UOPS=${TRAIN_MAX_TOTAL_UOPS:-32768}
VAL_FRAC=${VAL_FRAC:-0.05}
EVAL_EVERY=${EVAL_EVERY:-1000}
EVAL_BATCHES=${EVAL_BATCHES:-0}
SAVE_EVERY=${SAVE_EVERY:-500}
NUM_WORKERS=${NUM_WORKERS:-2}
PREFETCH_FACTOR=${PREFETCH_FACTOR:-2}
LENGTH_BUCKET_SIZE=${LENGTH_BUCKET_SIZE:-2048}
AMP_DTYPE=${AMP_DTYPE:-bf16}
REQUIRE_FLASH_ATTN=${REQUIRE_FLASH_ATTN:-0}
SDPA_BACKEND=${SDPA_BACKEND:-no_flash}

D_MODEL=${D_MODEL:-320}
N_HEADS=${N_HEADS:-8}
N_LAYERS=${N_LAYERS:-8}
FFN_DIM=${FFN_DIM:-1280}
FIELD_DIM=${FIELD_DIM:-96}
HEAD_HIDDEN=${HEAD_HIDDEN:-256}
DROPOUT=${DROPOUT:-0.1}

mkdir -p logs ckpt tmp "$OUT"

echo "[v26-kvqr-ddp] ROOT=$ROOT"
echo "[v26-kvqr-ddp] DATA=$DATA"
echo "[v26-kvqr-ddp] CACHE_PATH=$CACHE_PATH"
echo "[v26-kvqr-ddp] OUT=$OUT"
echo "[v26-kvqr-ddp] GPUS=$GPUS NPROC=$NPROC STEPS=$STEPS BS=$BS"
echo "[v26-kvqr-ddp] schema/PMU inferred from tensor cache manifest"
echo "[v26-kvqr-ddp] attention=doc_qkvr attention_impl=ragged_sdpa layers=$N_LAYERS heads=$N_HEADS d_model=$D_MODEL ffn=$FFN_DIM"
echo "[v26-kvqr-ddp] amp_dtype=$AMP_DTYPE require_flash_attn=$REQUIRE_FLASH_ATTN sdpa_backend=$SDPA_BACKEND"
echo "[v26-kvqr-ddp] max_uops_per_core=$MAX_UOPS_PER_CORE train_max_uops_per_core=$TRAIN_MAX_UOPS_PER_CORE train_max_total_uops=$TRAIN_MAX_TOTAL_UOPS filter_long_uops=on"
echo "[v26-kvqr-ddp] bucket_by_shape=on length_bucket_size=$LENGTH_BUCKET_SIZE"
echo "[v26-kvqr-ddp] eval_every=$EVAL_EVERY eval_batches=$EVAL_BATCHES save_every=$SAVE_EVERY"
echo "[v26-kvqr-ddp] structured tensor cache is required"

export CUDA_VISIBLE_DEVICES="$GPUS"
export OMP_NUM_THREADS=${OMP_NUM_THREADS:-4}
export NCCL_IB_DISABLE=${NCCL_IB_DISABLE:-1}
export PYTORCH_CUDA_ALLOC_CONF=${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}
export TMPDIR=${TMPDIR:-"$ROOT/tmp"}

flash_args=()
if [[ "$REQUIRE_FLASH_ATTN" == "1" || "$REQUIRE_FLASH_ATTN" == "true" || "$REQUIRE_FLASH_ATTN" == "TRUE" ]]; then
  flash_args+=(--require-flash-attn)
else
  flash_args+=(--no-require-flash-attn)
fi

exec "$TORCHRUN" \
  --standalone \
  --nproc_per_node="$NPROC" \
  train/train_v26_kvqr.py \
  --data "$DATA" \
  --cache-path "$CACHE_PATH" \
  --out "$OUT" \
  --steps "$STEPS" \
  --bs "$BS" \
  --lr "$LR" \
  --weight-decay "$WEIGHT_DECAY" \
  --max-len "$MAX_LEN" \
  --d-model "$D_MODEL" \
  --n-heads "$N_HEADS" \
  --n-layers "$N_LAYERS" \
  --ffn-dim "$FFN_DIM" \
  --field-dim "$FIELD_DIM" \
  --head-hidden "$HEAD_HIDDEN" \
  --amp-dtype "$AMP_DTYPE" \
  --sdpa-backend "$SDPA_BACKEND" \
  --max-uops-per-core "$MAX_UOPS_PER_CORE" \
  --train-max-uops-per-core "$TRAIN_MAX_UOPS_PER_CORE" \
  --train-max-total-uops "$TRAIN_MAX_TOTAL_UOPS" \
  --dropout "$DROPOUT" \
  --val-frac "$VAL_FRAC" \
  --eval-every "$EVAL_EVERY" \
  --eval-batches "$EVAL_BATCHES" \
  --save-every "$SAVE_EVERY" \
  --num-workers "$NUM_WORKERS" \
  --prefetch-factor "$PREFETCH_FACTOR" \
  --length-bucket-size "$LENGTH_BUCKET_SIZE" \
  "${flash_args[@]}" \
  2>&1 | tee logs/train_v26_kvqr_clean14_ddp8.log
