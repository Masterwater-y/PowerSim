#!/usr/bin/env bash
# c8 mainline: pretrained online Qwen + fresh task modules + real labels only.
# No native timing checkpoint and no teacher/distillation data are accepted.
set -euo pipefail

REPO=${REPO:-/data00/yinhaolang/LLMSim}
TORCHRUN=${TORCHRUN:-/data00/yinhaolang/infer/.venv/bin/torchrun}
BASE_MODEL=${BASE_MODEL:-Qwen/Qwen2.5-Coder-1.5B-Instruct}
MANIFEST=${MANIFEST:-/data00/yinhaolang/TCSim/data/v29_global_time_dataset/manifest.json}
STATIC_MANIFEST=${STATIC_MANIFEST:-$REPO/data/v28_1/static_dict/manifest.jsonl}
SEMANTIC_CACHE=${SEMANTIC_CACHE:-$REPO/data/v29_macro_semantic_cache_c8}
RUN_NAME=${RUN_NAME:-macro_v29_semantic_c8_8k}
OUTPUT=${OUTPUT:-$REPO/ckpt/$RUN_NAME}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
NPROC=${NPROC:-8}
MASTER_PORT=${MASTER_PORT:-29639}
STEPS=${STEPS:-8000}
BATCH_SIZE=${BATCH_SIZE:-1}
NUM_WORKERS=${NUM_WORKERS:-4}
LR_HEAD=${LR_HEAD:-3e-4}
LR_LORA=${LR_LORA:-1e-4}
DTYPE=${DTYPE:-bf16}
EVAL_EVERY=${EVAL_EVERY:-500}
EVAL_BATCHES=${EVAL_BATCHES:-32}
SAVE_EVERY=${SAVE_EVERY:-500}
TARGET_STRIDE_MACRO=${TARGET_STRIDE_MACRO:-256}

cd "$REPO"
if [[ ! -f "$SEMANTIC_CACHE/manifest.json" ]]; then
  echo "[train][ERROR] missing semantic cache: $SEMANTIC_CACHE/manifest.json" >&2
  echo "Run: bash scripts/build_macro_v29_semantic_cache.sh" >&2
  exit 2
fi
if [[ -e "$OUTPUT/run.json" ]]; then
  echo "[train][ERROR] output already contains run.json: $OUTPUT" >&2
  echo "Set a new RUN_NAME/OUTPUT; mainline initialization must be unambiguous." >&2
  exit 3
fi
mkdir -p "$OUTPUT"

export CUDA_VISIBLE_DEVICES="$GPUS"
export PYTHONPATH="$REPO:/data00/yinhaolang/TCSim:${PYTHONPATH:-}"
export TOKENIZERS_PARALLELISM=false
export HF_HUB_OFFLINE=${HF_HUB_OFFLINE:-1}
export TRANSFORMERS_OFFLINE=${TRANSFORMERS_OFFLINE:-1}
export OMP_NUM_THREADS=${OMP_NUM_THREADS:-4}
export NCCL_IB_DISABLE=${NCCL_IB_DISABLE:-1}
export PYTORCH_CUDA_ALLOC_CONF=${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}

cmd=(
  "$TORCHRUN" --standalone --nproc-per-node="$NPROC" --master_port="$MASTER_PORT"
  train/train_macro_v29.py
  --manifest "$MANIFEST"
  --static-manifest "$STATIC_MANIFEST"
  --train-split train
  --validation-split train
  --cores 8
  --base-model "$BASE_MODEL"
  --semantic-input-mode cached_macro_soft_token
  --semantic-cache-root "$SEMANTIC_CACHE"
  --semantic-variant real
  --sequence-length 1
  --sequence-stride 1
  --batch-size "$BATCH_SIZE"
  --num-workers "$NUM_WORKERS"
  --max-steps "$STEPS"
  --lr-schedule-steps "$STEPS"
  --lr-head "$LR_HEAD"
  --lr-lora "$LR_LORA"
  --dtype "$DTYPE"
  --log-every 10
  --eval-every "$EVAL_EVERY"
  --eval-batches "$EVAL_BATCHES"
  --save-every "$SAVE_EVERY"
  --target-stride-macro "$TARGET_STRIDE_MACRO"
  --output "$OUTPUT"
)
if [[ "${GRADIENT_CHECKPOINTING:-0}" == "1" ]]; then
  cmd+=(--gradient-checkpointing)
fi

echo "[semantic train launch] output=$OUTPUT steps=$STEPS cores=8 nproc=$NPROC"
echo "[semantic train contract] fresh task init; real labels only; distillation=false"
exec "${cmd[@]}"
