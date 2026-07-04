#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
cd "$ROOT"

TORCHRUN=${TORCHRUN:-/data00/yinhaolang/infer/.venv/bin/torchrun}
DATA=${DATA:-data/windows_v17_bc_split_heads_nophase_all/windows.jsonl}
CACHE_PATH=${CACHE_PATH:-data/windows_v17_bc_split_heads_nophase_all/windows.maxlen32768.tensor_cache}
OUT=${OUT:-ckpt/v18_fastslow_adapter_nophase_8gpu_8000}
BASE_MODEL=${BASE_MODEL:-Qwen/Qwen3-0.6B-Base}

DEFAULT_INIT=${DEFAULT_INIT:-ckpt/v17_bc_split_heads_nophase_8gpu_8000_resume500/step_008000}
WARM_START_V17=${WARM_START_V17:-0}
INIT_FROM_DEFAULT=0
if [[ -z "${INIT_CKPT:-}" && "$WARM_START_V17" == "1" && -d "$DEFAULT_INIT" ]]; then
  INIT_CKPT="$DEFAULT_INIT"
  INIT_FROM_DEFAULT=1
else
  INIT_CKPT=${INIT_CKPT:-}
fi
RESET_LOSS_STATE=${RESET_LOSS_STATE:-$INIT_FROM_DEFAULT}

STEPS=${STEPS:-8000}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
NPROC=${NPROC:-8}
BS=${BS:-1}
GRAD_ACCUM=${GRAD_ACCUM:-1}
MAX_LEN=${MAX_LEN:-32768}
LR_LORA=${LR_LORA:-1e-4}
LR_HEAD=${LR_HEAD:-5e-4}
LR_EMB=${LR_EMB:-3e-4}
VAL_FRAC=${VAL_FRAC:-0.15}
LOG_EVERY=${LOG_EVERY:-20}
EVAL_EVERY=${EVAL_EVERY:-500}
SAVE_EVERY=${SAVE_EVERY:-500}
EVAL_BATCHES=${EVAL_BATCHES:-0}
NUM_WORKERS=${NUM_WORKERS:-2}

LAMBDA_DELTA=${LAMBDA_DELTA:-2.0}
LAMBDA_CYCLES_WINDOW=${LAMBDA_CYCLES_WINDOW:-0.5}
LAMBDA_RANK=${LAMBDA_RANK:-0.15}
LAMBDA_SPREAD=${LAMBDA_SPREAD:-0.10}
LAMBDA_SLOWEST=${LAMBDA_SLOWEST:-0.05}
LAMBDA_FASTEST=${LAMBDA_FASTEST:-0.05}
RANK_GAP=${RANK_GAP:-0.08}
RANK_TAU=${RANK_TAU:-0.10}
SPREAD_MIN_STD=${SPREAD_MIN_STD:-0.03}
SPREAD_REF=${SPREAD_REF:-0.10}
SPREAD_WEIGHT_MAX=${SPREAD_WEIGHT_MAX:-5.0}

CORE_ADAPTER_LAYERS=${CORE_ADAPTER_LAYERS:-2}
CORE_ADAPTER_HEADS=${CORE_ADAPTER_HEADS:-8}
CORE_ADAPTER_FF_MULT=${CORE_ADAPTER_FF_MULT:-2}
CORE_ADAPTER_DROPOUT=${CORE_ADAPTER_DROPOUT:-0.05}

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
if [[ "$RESET_LOSS_STATE" == "1" ]]; then
  extra_args+=(--reset-loss-state)
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

echo "[v18-fastslow-train] DATA=$DATA"
echo "[v18-fastslow-train] CACHE_PATH=$CACHE_PATH"
echo "[v18-fastslow-train] OUT=$OUT"
echo "[v18-fastslow-train] BASE_MODEL=$BASE_MODEL"
echo "[v18-fastslow-train] INIT_CKPT=${INIT_CKPT:-<none>} RESET_LOSS_STATE=$RESET_LOSS_STATE"
echo "[v18-fastslow-train] STEPS=$STEPS GPUS=$GPUS NPROC=$NPROC"
echo "[v18-fastslow-train] LR_LORA=$LR_LORA LR_HEAD=$LR_HEAD LR_EMB=$LR_EMB"
echo "[v18-fastslow-train] cpi_head_mode=delta label_version=v17_split_no_dtlb"
echo "[v18-fastslow-train] lambda_delta=$LAMBDA_DELTA lambda_cycles_window=$LAMBDA_CYCLES_WINDOW"
echo "[v18-fastslow-train] lambda_rank=$LAMBDA_RANK lambda_spread=$LAMBDA_SPREAD"
echo "[v18-fastslow-train] lambda_slowest=$LAMBDA_SLOWEST lambda_fastest=$LAMBDA_FASTEST"
echo "[v18-fastslow-train] rank_gap=$RANK_GAP rank_tau=$RANK_TAU spread_min_std=$SPREAD_MIN_STD"
echo "[v18-fastslow-train] spread_ref=$SPREAD_REF spread_weight_max=$SPREAD_WEIGHT_MAX"
echo "[v18-fastslow-train] core_adapter_layers=$CORE_ADAPTER_LAYERS heads=$CORE_ADAPTER_HEADS ff_mult=$CORE_ADAPTER_FF_MULT dropout=$CORE_ADAPTER_DROPOUT"
echo "[v18-fastslow-train] EVAL_EVERY=$EVAL_EVERY SAVE_EVERY=$SAVE_EVERY EVAL_BATCHES=$EVAL_BATCHES"
echo "[v18-fastslow-train] MAX_LEN=$MAX_LEN use_tstart=1 query_placement=tail_local"

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
  --core-adapter-layers "$CORE_ADAPTER_LAYERS" \
  --core-adapter-heads "$CORE_ADAPTER_HEADS" \
  --core-adapter-ff-mult "$CORE_ADAPTER_FF_MULT" \
  --core-adapter-dropout "$CORE_ADAPTER_DROPOUT" \
  --lambda-delta "$LAMBDA_DELTA" \
  --lambda-cycles-window "$LAMBDA_CYCLES_WINDOW" \
  --lambda-rank "$LAMBDA_RANK" \
  --lambda-spread "$LAMBDA_SPREAD" \
  --lambda-slowest "$LAMBDA_SLOWEST" \
  --lambda-fastest "$LAMBDA_FASTEST" \
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
  "${extra_args[@]}" \
  "$@"
