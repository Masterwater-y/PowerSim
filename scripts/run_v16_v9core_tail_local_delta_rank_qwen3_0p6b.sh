#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
cd "$ROOT"

TORCHRUN=${TORCHRUN:-/data00/yinhaolang/infer/.venv/bin/torchrun}
DATA=${DATA:-data/windows_v16_v9core_tail_local_all/windows.jsonl}
CACHE_PATH=${CACHE_PATH:-data/windows_v16_v9core_tail_local_all/windows.maxlen32768.tensor_cache}
OUT=${OUT:-ckpt/v16_v9core_tail_local_delta_rank_8gpu_8000}
BASE_MODEL=${BASE_MODEL:-Qwen/Qwen3-0.6B-Base}

STEPS=${STEPS:-8000}
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

LAMBDA_RANK=${LAMBDA_RANK:-0.02}
LAMBDA_SPREAD=${LAMBDA_SPREAD:-0.02}
RANK_GAP=${RANK_GAP:-0.10}
RANK_TAU=${RANK_TAU:-0.10}
SPREAD_MIN_STD=${SPREAD_MIN_STD:-0.03}

INIT_CKPT=${INIT_CKPT:-}
SKIP_TRAIN_BATCHES=${SKIP_TRAIN_BATCHES:-0}
STEP_OFFSET=${STEP_OFFSET:-0}

mkdir -p logs ckpt "$OUT"

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

echo "[v16-train] DATA=$DATA"
echo "[v16-train] CACHE_PATH=$CACHE_PATH"
echo "[v16-train] OUT=$OUT"
echo "[v16-train] BASE_MODEL=$BASE_MODEL"
echo "[v16-train] STEPS=$STEPS GPUS=$GPUS NPROC=$NPROC"
echo "[v16-train] LR_LORA=$LR_LORA LR_HEAD=$LR_HEAD LR_EMB=$LR_EMB"
echo "[v16-train] cpi_head_mode=delta lambda_rank=$LAMBDA_RANK lambda_spread=$LAMBDA_SPREAD"
echo "[v16-train] rank_gap=$RANK_GAP rank_tau=$RANK_TAU spread_min_std=$SPREAD_MIN_STD"
echo "[v16-train] EVAL_EVERY=$EVAL_EVERY SAVE_EVERY=$SAVE_EVERY EVAL_BATCHES=$EVAL_BATCHES"
echo "[v16-train] INIT_CKPT=${INIT_CKPT:-<none>} SKIP_TRAIN_BATCHES=$SKIP_TRAIN_BATCHES STEP_OFFSET=$STEP_OFFSET"
echo "[v16-train] MAX_LEN=$MAX_LEN use_tstart=1 query_placement=tail_local"

export CUDA_VISIBLE_DEVICES="$GPUS"
export HF_HUB_OFFLINE=${HF_HUB_OFFLINE:-1}
export TOKENIZERS_PARALLELISM=${TOKENIZERS_PARALLELISM:-false}
export OMP_NUM_THREADS=${OMP_NUM_THREADS:-4}
export NCCL_IB_DISABLE=${NCCL_IB_DISABLE:-1}
export PYTORCH_CUDA_ALLOC_CONF=${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}

exec "$TORCHRUN" \
  --standalone \
  --nproc_per_node="$NPROC" \
  train/train_lora.py \
  --data "$DATA" \
  --out "$OUT" \
  --base-model "$BASE_MODEL" \
  --cpi-head-mode delta \
  --lambda-rank "$LAMBDA_RANK" \
  --lambda-spread "$LAMBDA_SPREAD" \
  --rank-gap "$RANK_GAP" \
  --rank-tau "$RANK_TAU" \
  --spread-min-std "$SPREAD_MIN_STD" \
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
  --use-tstart \
  "${extra_args[@]}"
