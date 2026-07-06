#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TSim}
cd "$ROOT"

TORCHRUN=${TORCHRUN:-/data00/yinhaolang/infer/.venv/bin/torchrun}
DATA=${DATA:-data/windows_v16_v9core_tail_local_all/windows.jsonl}
CACHE_PATH=${CACHE_PATH:-data/windows_v16_v9core_tail_local_all/windows.maxlen32768.tensor_cache}
OUT=${OUT:-ckpt/v25a_tiny_transformer_8l320_8gpu_8000}
BASE_MODEL=${BASE_MODEL:-Qwen/Qwen3-0.6B-Base}

STEPS=${STEPS:-8000}
WARMUP=${WARMUP:-1000}
LR=${LR:-3e-4}
LR_MIN_RATIO=${LR_MIN_RATIO:-0.1}
WEIGHT_DECAY=${WEIGHT_DECAY:-0.05}
ADAM_BETA1=${ADAM_BETA1:-0.9}
ADAM_BETA2=${ADAM_BETA2:-0.95}
GRAD_CLIP=${GRAD_CLIP:-1.0}

GPUS=${GPUS:-0,1,2,3,4,5,6,7}
NPROC=${NPROC:-8}
BS=${BS:-8}
GRAD_ACCUM=${GRAD_ACCUM:-1}
MAX_LEN=${MAX_LEN:-32768}
VAL_FRAC=${VAL_FRAC:-0.15}
LOG_EVERY=${LOG_EVERY:-100}
EVAL_EVERY=${EVAL_EVERY:-500}
SAVE_EVERY=${SAVE_EVERY:-500}
EVAL_BATCHES=${EVAL_BATCHES:-0}
NUM_WORKERS=${NUM_WORKERS:-2}

LOCAL_FUSE_MODE=${LOCAL_FUSE_MODE:-add}

LOSS_WEIGHT_MODE=${LOSS_WEIGHT_MODE:-fixed}
LAMBDA_CPI_ABS=${LAMBDA_CPI_ABS:-1.0}
LAMBDA_CYCLES=${LAMBDA_CYCLES:-1.0}
LAMBDA_AUX_PMU=${LAMBDA_AUX_PMU:-0.05}
LAMBDA_CENTERED_CPI=${LAMBDA_CENTERED_CPI:-0.3}
LAMBDA_INV=${LAMBDA_INV:-0.0}
LAMBDA_PHYS=${LAMBDA_PHYS:-0.0}
CENTERED_MIN_STD=${CENTERED_MIN_STD:-0.30}
CENTERED_REF_STD=${CENTERED_REF_STD:-0.30}
CENTERED_WEIGHT_MIN=${CENTERED_WEIGHT_MIN:-0.10}
CENTERED_WEIGHT_MAX=${CENTERED_WEIGHT_MAX:-3.0}
CENTERED_DELTA=${CENTERED_DELTA:-0.1}

INIT_CKPT=${INIT_CKPT:-}
SKIP_TRAIN_BATCHES=${SKIP_TRAIN_BATCHES:-0}
STEP_OFFSET=${STEP_OFFSET:-0}

mkdir -p logs ckpt tmp "$OUT"

extra_args=()
if [[ -n "$CACHE_PATH" ]]; then
  extra_args+=(--cache-path "$CACHE_PATH")
fi
if [[ -n "$INIT_CKPT" ]]; then
  extra_args+=(--init-ckpt "$INIT_CKPT")
fi
if [[ "$SKIP_TRAIN_BATCHES" != "0" ]]; then
  extra_args+=(--skip-train-batches "$SKIP_TRAIN_BATCHES")
fi
if [[ "$STEP_OFFSET" != "0" ]]; then
  extra_args+=(--step-offset "$STEP_OFFSET")
fi
if [[ "$SAVE_EVERY" != "0" ]]; then
  extra_args+=(--save-every "$SAVE_EVERY")
fi

echo "[v25a] ROOT=$ROOT"
echo "[v25a] DATA=$DATA"
echo "[v25a] CACHE_PATH=$CACHE_PATH"
echo "[v25a] OUT=$OUT"
echo "[v25a] BASE_MODEL(tokenizer only)=$BASE_MODEL"
echo "[v25a] tiny=d_model320 layers8 heads8 ffn1280 position=RoPE theta=10000"
echo "[v25a] STEPS=$STEPS WARMUP=$WARMUP LR=$LR LR_MIN_RATIO=$LR_MIN_RATIO"
echo "[v25a] WEIGHT_DECAY=$WEIGHT_DECAY ADAM_BETAS=($ADAM_BETA1,$ADAM_BETA2) GRAD_CLIP=$GRAD_CLIP"
echo "[v25a] GPUS=$GPUS NPROC=$NPROC BS=$BS GRAD_ACCUM=$GRAD_ACCUM MAX_LEN=$MAX_LEN"
echo "[v25a] cpi_head_mode=direct local_fuse_mode=$LOCAL_FUSE_MODE"
echo "[v25a] loss fixed cpi_abs=$LAMBDA_CPI_ABS cycles=$LAMBDA_CYCLES aux_pmu=$LAMBDA_AUX_PMU centered_cpi=$LAMBDA_CENTERED_CPI"

export CUDA_VISIBLE_DEVICES="$GPUS"
export HF_HUB_OFFLINE=${HF_HUB_OFFLINE:-1}
export TOKENIZERS_PARALLELISM=${TOKENIZERS_PARALLELISM:-false}
export OMP_NUM_THREADS=${OMP_NUM_THREADS:-4}
export NCCL_IB_DISABLE=${NCCL_IB_DISABLE:-1}
export PYTORCH_CUDA_ALLOC_CONF=${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}
export TMPDIR=${TMPDIR:-"$ROOT/tmp"}

exec "$TORCHRUN" \
  --standalone \
  --nproc_per_node="$NPROC" \
  train/train_lora.py \
  --data "$DATA" \
  --out "$OUT" \
  --base-model "$BASE_MODEL" \
  --tiny-transformer \
  --tiny-d-model 320 \
  --tiny-n-layers 8 \
  --tiny-n-heads 8 \
  --tiny-ffn-dim 1280 \
  --tiny-rope-theta 10000 \
  --dropout 0.1 \
  --attn-dropout 0.1 \
  --cpi-head-mode direct \
  --local-fuse-mode "$LOCAL_FUSE_MODE" \
  --loss-weight-mode "$LOSS_WEIGHT_MODE" \
  --lambda-cpi-abs "$LAMBDA_CPI_ABS" \
  --lambda-cycles "$LAMBDA_CYCLES" \
  --lambda-aux-pmu "$LAMBDA_AUX_PMU" \
  --lambda-centered-cpi "$LAMBDA_CENTERED_CPI" \
  --lambda-inv "$LAMBDA_INV" \
  --lambda-phys "$LAMBDA_PHYS" \
  --centered-min-std "$CENTERED_MIN_STD" \
  --centered-ref-std "$CENTERED_REF_STD" \
  --centered-weight-min "$CENTERED_WEIGHT_MIN" \
  --centered-weight-max "$CENTERED_WEIGHT_MAX" \
  --centered-delta "$CENTERED_DELTA" \
  --steps "$STEPS" \
  --warmup-steps "$WARMUP" \
  --lr-min-ratio "$LR_MIN_RATIO" \
  --bs "$BS" \
  --grad-accum "$GRAD_ACCUM" \
  --lr-lora "$LR" \
  --lr-head "$LR" \
  --lr-emb "$LR" \
  --weight-decay "$WEIGHT_DECAY" \
  --adam-beta1 "$ADAM_BETA1" \
  --adam-beta2 "$ADAM_BETA2" \
  --grad-clip "$GRAD_CLIP" \
  --max-len "$MAX_LEN" \
  --val-frac "$VAL_FRAC" \
  --log-every "$LOG_EVERY" \
  --eval-every "$EVAL_EVERY" \
  --eval-batches "$EVAL_BATCHES" \
  --num-workers "$NUM_WORKERS" \
  "${extra_args[@]}" \
  2>&1 | tee logs/train_v25a.log
