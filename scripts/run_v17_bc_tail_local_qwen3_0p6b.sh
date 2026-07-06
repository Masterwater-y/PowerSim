#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
cd "$ROOT"

TORCHRUN=${TORCHRUN:-/data00/yinhaolang/infer/.venv/bin/torchrun}
DATA=${DATA:-data/windows_v17_bc_split_heads_nophase_all/windows.jsonl}
CACHE_PATH=${CACHE_PATH:-data/windows_v17_bc_split_heads_nophase_all/windows.maxlen32768.tensor_cache}
OUT=${OUT:-ckpt/v17_bc_split_heads_nophase_8gpu_8000}
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

LAMBDA_DELTA=${LAMBDA_DELTA:-0.75}
LAMBDA_CYCLES_WINDOW=${LAMBDA_CYCLES_WINDOW:-1.0}
LAMBDA_RANK=${LAMBDA_RANK:-0.02}
LAMBDA_SPREAD=${LAMBDA_SPREAD:-0.02}
RANK_GAP=${RANK_GAP:-0.10}
RANK_TAU=${RANK_TAU:-0.10}
SPREAD_MIN_STD=${SPREAD_MIN_STD:-0.03}
SPREAD_REF=${SPREAD_REF:-0.10}
SPREAD_WEIGHT_MAX=${SPREAD_WEIGHT_MAX:-3.0}

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

echo "[v17BC-split-train] DATA=$DATA"
echo "[v17BC-split-train] CACHE_PATH=$CACHE_PATH"
echo "[v17BC-split-train] OUT=$OUT"
echo "[v17BC-split-train] BASE_MODEL=$BASE_MODEL"
echo "[v17BC-split-train] STEPS=$STEPS GPUS=$GPUS NPROC=$NPROC"
echo "[v17BC-split-train] LR_LORA=$LR_LORA LR_HEAD=$LR_HEAD LR_EMB=$LR_EMB"
echo "[v17BC-split-train] cpi_head_mode=delta label_version=v17_split_no_dtlb"
echo "[v17BC-split-train] lambda_delta=$LAMBDA_DELTA lambda_cycles_window=$LAMBDA_CYCLES_WINDOW"
echo "[v17BC-split-train] lambda_rank=$LAMBDA_RANK lambda_spread=$LAMBDA_SPREAD"
echo "[v17BC-split-train] rank_gap=$RANK_GAP rank_tau=$RANK_TAU spread_min_std=$SPREAD_MIN_STD"
echo "[v17BC-split-train] spread_ref=$SPREAD_REF spread_weight_max=$SPREAD_WEIGHT_MAX"
echo "[v17BC-split-train] EVAL_EVERY=$EVAL_EVERY SAVE_EVERY=$SAVE_EVERY EVAL_BATCHES=$EVAL_BATCHES"
echo "[v17BC-split-train] INIT_CKPT=${INIT_CKPT:-<none>} SKIP_TRAIN_BATCHES=$SKIP_TRAIN_BATCHES STEP_OFFSET=$STEP_OFFSET"
echo "[v17BC-split-train] MAX_LEN=$MAX_LEN use_tstart=1 query_placement=tail_local"

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
  --lambda-delta "$LAMBDA_DELTA" \
  --lambda-cycles-window "$LAMBDA_CYCLES_WINDOW" \
  --lambda-rank "$LAMBDA_RANK" \
  --lambda-spread "$LAMBDA_SPREAD" \
  --rank-gap "$RANK_GAP" \
  --rank-tau "$RANK_TAU" \
  --spread-min-std "$SPREAD_MIN_STD" \
  --spread-ref "$SPREAD_REF" \
  --spread-weight-max "$SPREAD_WEIGHT_MAX" \
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
