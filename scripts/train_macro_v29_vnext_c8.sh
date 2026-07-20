#!/usr/bin/env bash
# vNext c8 training: 384-d macro states plus full per-macro cross-core SDPA.
# Fresh task initialization, real labels only, no teacher or distillation.
set -euo pipefail

REPO=${REPO:-/data00/yinhaolang/LLMSim}
TORCHRUN=${TORCHRUN:-/data00/yinhaolang/infer/.venv/bin/torchrun}
BASE_MODEL=${BASE_MODEL:-Qwen/Qwen2.5-Coder-1.5B-Instruct}
MANIFEST=${MANIFEST:-/data00/yinhaolang/TCSim/data/v29_global_time_dataset/manifest.json}
STATIC_MANIFEST=${STATIC_MANIFEST:-$REPO/data/v28_1/static_dict/manifest.jsonl}
SEMANTIC_CACHE=${SEMANTIC_CACHE:-$REPO/data/v29_macro_semantic_cache_c8}
RUN_NAME=${RUN_NAME:-macro_v29_vnext_d384_crossmacro_c8_s1_30k}
OUTPUT=${OUTPUT:-$REPO/ckpt/$RUN_NAME}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
NPROC=${NPROC:-8}
MASTER_PORT=${MASTER_PORT:-29649}
STEPS=${STEPS:-30000}
BATCH_SIZE=${BATCH_SIZE:-1}
NUM_WORKERS=${NUM_WORKERS:-4}
LR_HEAD=${LR_HEAD:-3e-4}
LR_LORA=${LR_LORA:-1e-4}
DTYPE=${DTYPE:-bf16}
EVAL_EVERY=${EVAL_EVERY:-500}
EVAL_BATCHES=${EVAL_BATCHES:-32}
SAVE_EVERY=${SAVE_EVERY:-500}
TARGET_STRIDE_MACRO=${TARGET_STRIDE_MACRO:-256}
SEQUENCE_LENGTH=${SEQUENCE_LENGTH:-1}
D_MODEL=${D_MODEL:-384}
N_HEADS=${N_HEADS:-8}
CROSS_FFN_MULTIPLIER=${CROSS_FFN_MULTIPLIER:-4}
CROSS_TARGET_BLOCK=${CROSS_TARGET_BLOCK:-8}
BACKBONE_CORE_CHUNK_SIZE=${BACKBONE_CORE_CHUNK_SIZE:-8}
BACKBONE_CHUNK_CHECKPOINT=${BACKBONE_CHUNK_CHECKPOINT:-1}
GRADIENT_CHECKPOINTING=${GRADIENT_CHECKPOINTING:-0}

cd "$REPO"
if [[ ! -f "$SEMANTIC_CACHE/manifest.json" ]]; then
  echo "[vnext train][ERROR] missing semantic cache: $SEMANTIC_CACHE/manifest.json" >&2
  echo "Run scripts/build_macro_v29_semantic_cache.sh first." >&2
  exit 2
fi
if [[ -e "$OUTPUT/run.json" ]]; then
  echo "[vnext train][ERROR] output already contains run.json: $OUTPUT" >&2
  echo "Set a fresh RUN_NAME or OUTPUT; vNext does not resume an old timing model." >&2
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
  --validation-split validation
  --cores 8
  --base-model "$BASE_MODEL"
  --semantic-input-mode cached_macro_soft_token
  --semantic-cache-root "$SEMANTIC_CACHE"
  --semantic-variant real
  --sequence-length "$SEQUENCE_LENGTH"
  --sequence-stride "$SEQUENCE_LENGTH"
  --batch-size "$BATCH_SIZE"
  --num-workers "$NUM_WORKERS"
  --max-steps "$STEPS"
  --lr-schedule-steps "$STEPS"
  --lr-head "$LR_HEAD"
  --lr-lora "$LR_LORA"
  --dtype "$DTYPE"
  --d-model "$D_MODEL"
  --n-heads "$N_HEADS"
  --cross-ffn-multiplier "$CROSS_FFN_MULTIPLIER"
  --cross-target-block "$CROSS_TARGET_BLOCK"
  --backbone-core-chunk-size "$BACKBONE_CORE_CHUNK_SIZE"
  --log-every 10
  --eval-every "$EVAL_EVERY"
  --eval-batches "$EVAL_BATCHES"
  --save-every "$SAVE_EVERY"
  --target-stride-macro "$TARGET_STRIDE_MACRO"
  --output "$OUTPUT"
)
if [[ "$BACKBONE_CHUNK_CHECKPOINT" == "1" ]]; then
  cmd+=(--backbone-chunk-checkpoint)
fi
if [[ "$GRADIENT_CHECKPOINTING" == "1" ]]; then
  cmd+=(--gradient-checkpointing)
fi

echo "[vnext train] output=$OUTPUT cores=8 steps=$STEPS sequence=$SEQUENCE_LENGTH"
echo "[vnext model] d_macro=$D_MODEL cross_target_block=$CROSS_TARGET_BLOCK qwen_core_chunk=$BACKBONE_CORE_CHUNK_SIZE checkpoint=$BACKBONE_CHUNK_CHECKPOINT"
echo "[vnext contract] fresh task init; real labels only; distillation=false"
exec "${cmd[@]}"
