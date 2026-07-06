#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
cd "$ROOT"

TORCHRUN=${TORCHRUN:-/data00/yinhaolang/infer/.venv/bin/torchrun}
DATA=${DATA:-data/windows_v16_v9core_tail_local_all/windows.jsonl}
CACHE_PATH=${CACHE_PATH:-data/windows_v16_v9core_tail_local_all/windows.maxlen32768.tensor_cache}
OUT=${OUT:-ckpt/v22_v16_bind_split_direct_no_tstart_8gpu_12000}
BASE_MODEL=${BASE_MODEL:-Qwen/Qwen3-0.6B-Base}

STEPS=${STEPS:-12000}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
NPROC=${NPROC:-8}
BS=${BS:-1}
GRAD_ACCUM=${GRAD_ACCUM:-1}
MAX_LEN=${MAX_LEN:-32768}
LR_LORA=${LR_LORA:-2e-4}
LR_HEAD=${LR_HEAD:-1e-3}
LR_EMB=${LR_EMB:-1e-3}
VAL_FRAC=${VAL_FRAC:-0.15}
LOG_EVERY=${LOG_EVERY:-20}
EVAL_EVERY=${EVAL_EVERY:-500}
SAVE_EVERY=${SAVE_EVERY:-500}
EVAL_BATCHES=${EVAL_BATCHES:-0}
NUM_WORKERS=${NUM_WORKERS:-2}

LOCAL_FUSE_MODE=${LOCAL_FUSE_MODE:-bind_concat}

LOSS_WEIGHT_MODE=${LOSS_WEIGHT_MODE:-fixed}
LAMBDA_CPI_ABS=${LAMBDA_CPI_ABS:-1.0}
LAMBDA_CYCLES=${LAMBDA_CYCLES:-${LAMBDA_CYCLES_WINDOW:-1.0}}
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

echo "[v22-bind-notstart] DATA=$DATA"
echo "[v22-bind-notstart] CACHE_PATH=$CACHE_PATH"
echo "[v22-bind-notstart] OUT=$OUT"
echo "[v22-bind-notstart] BASE_MODEL=$BASE_MODEL"
echo "[v22-bind-notstart] STEPS=$STEPS GPUS=$GPUS NPROC=$NPROC"
echo "[v22-bind-notstart] LR_LORA=$LR_LORA LR_HEAD=$LR_HEAD LR_EMB=$LR_EMB"
echo "[v22-bind-notstart] cpi_head_mode=direct local_fuse_mode=$LOCAL_FUSE_MODE"
echo "[v22-bind-notstart] heads=cpi/branch/cache no_dtlb=1 rank_spread=disabled"
echo "[v22-bind-notstart] loss_weight_mode=$LOSS_WEIGHT_MODE cpi_abs=$LAMBDA_CPI_ABS cycles=$LAMBDA_CYCLES aux_pmu=$LAMBDA_AUX_PMU centered_cpi=$LAMBDA_CENTERED_CPI"
echo "[v22-bind-notstart] loss_disabled inv=$LAMBDA_INV phys=$LAMBDA_PHYS centered_min_std=$CENTERED_MIN_STD centered_ref_std=$CENTERED_REF_STD centered_weight_min=$CENTERED_WEIGHT_MIN centered_weight_max=$CENTERED_WEIGHT_MAX centered_delta=$CENTERED_DELTA"
echo "[v22-bind-notstart] EVAL_EVERY=$EVAL_EVERY SAVE_EVERY=$SAVE_EVERY EVAL_BATCHES=$EVAL_BATCHES"
echo "[v22-bind-notstart] INIT_CKPT=${INIT_CKPT:-<none>} SKIP_TRAIN_BATCHES=$SKIP_TRAIN_BATCHES STEP_OFFSET=$STEP_OFFSET"
echo "[v22-bind-notstart] MAX_LEN=$MAX_LEN use_tstart=0 query_placement=tail_local"

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
  --bs "$BS" \
  --grad-accum "$GRAD_ACCUM" \
  --lr-lora "$LR_LORA" \
  --lr-head "$LR_HEAD" \
  --lr-emb "$LR_EMB" \
  --max-len "$MAX_LEN" \
  --val-frac "$VAL_FRAC" \
  --log-every "$LOG_EVERY" \
  --eval-every "$EVAL_EVERY" \
  --eval-batches "$EVAL_BATCHES" \
  --num-workers "$NUM_WORKERS" \
  "${extra_args[@]}"
