#!/usr/bin/env bash
# Train one formal c8 A/B/E retraining variant.  The all-in-one launcher below
# invokes this script for B and E with identical data, seed and training budget.
set -euo pipefail

REPO=${REPO:-/data00/yinhaolang/LLMSim}
TORCHRUN=${TORCHRUN:-/data00/yinhaolang/infer/.venv/bin/torchrun}
BASE_MODEL=${BASE_MODEL:-Qwen/Qwen2.5-Coder-1.5B-Instruct}
MANIFEST=${MANIFEST:-/data00/yinhaolang/TCSim/data/v29_global_time_dataset/manifest.json}
STATIC_MANIFEST=${STATIC_MANIFEST:-$REPO/data/v28_1/static_dict/manifest.jsonl}
SEMANTIC_CACHE=${SEMANTIC_CACHE:-$REPO/data/v29_macro_semantic_cache_c8}
VARIANT=${VARIANT:?set VARIANT=B or VARIANT=E}
OUTPUT=${OUTPUT:?set a fresh OUTPUT directory}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
NPROC=${NPROC:-8}
MASTER_PORT=${MASTER_PORT:-29659}
STEPS=${STEPS:-30000}
SEED=${SEED:-1234}
BATCH_SIZE=${BATCH_SIZE:-1}
NUM_WORKERS=${NUM_WORKERS:-4}
LR_HEAD=${LR_HEAD:-3e-4}
LR_ONLINE_BACKBONE=${LR_ONLINE_BACKBONE:-3e-4}
DTYPE=${DTYPE:-bf16}
EVAL_EVERY=${EVAL_EVERY:-500}
EVAL_BATCHES=${EVAL_BATCHES:-32}
SAVE_EVERY=${SAVE_EVERY:-500}
SEQUENCE_LENGTH=${SEQUENCE_LENGTH:-1}
TARGET_STRIDE_MACRO=${TARGET_STRIDE_MACRO:-256}
D_MODEL=${D_MODEL:-384}
N_HEADS=${N_HEADS:-8}
ONLINE_INPUT_DIM=${ONLINE_INPUT_DIM:-1536}
ONLINE_TRANSFORMER_LAYERS=${ONLINE_TRANSFORMER_LAYERS:-5}
ONLINE_TRANSFORMER_HEADS=${ONLINE_TRANSFORMER_HEADS:-8}
ONLINE_TRANSFORMER_FFN_MULTIPLIER=${ONLINE_TRANSFORMER_FFN_MULTIPLIER:-4}
CROSS_FFN_MULTIPLIER=${CROSS_FFN_MULTIPLIER:-4}
CROSS_TARGET_BLOCK=${CROSS_TARGET_BLOCK:-8}

case "$VARIANT" in
  B)
    SEMANTIC_INPUT_MODE=cached_macro_soft_token
    SEMANTIC_SOURCE=real_cache
    ;;
  E)
    SEMANTIC_INPUT_MODE=learned_null_macro_token
    SEMANTIC_SOURCE=learned_null
    ;;
  *)
    echo "[ABE train][ERROR] VARIANT must be B or E, got: $VARIANT" >&2
    exit 2
    ;;
esac

cd "$REPO"
for required in "$MANIFEST" "$STATIC_MANIFEST"; do
  if [[ ! -f "$required" ]]; then
    echo "[ABE train][ERROR] missing prerequisite: $required" >&2
    exit 3
  fi
done
if [[ "$VARIANT" == "B" && ! -f "$SEMANTIC_CACHE/manifest.json" ]]; then
  echo "[ABE train][ERROR] B requires semantic cache: $SEMANTIC_CACHE/manifest.json" >&2
  exit 4
fi
if [[ -e "$OUTPUT/run.json" ]]; then
  echo "[ABE train][ERROR] refusing to overwrite existing run: $OUTPUT" >&2
  exit 5
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
  --experiment-variant "$VARIANT"
  --online-backbone-type causal_transformer
  --semantic-source "$SEMANTIC_SOURCE"
  --semantic-input-mode "$SEMANTIC_INPUT_MODE"
  --semantic-variant real
  --online-input-dim "$ONLINE_INPUT_DIM"
  --online-transformer-layers "$ONLINE_TRANSFORMER_LAYERS"
  --online-transformer-heads "$ONLINE_TRANSFORMER_HEADS"
  --online-transformer-ffn-multiplier "$ONLINE_TRANSFORMER_FFN_MULTIPLIER"
  --sequence-length "$SEQUENCE_LENGTH"
  --sequence-stride "$SEQUENCE_LENGTH"
  --batch-size "$BATCH_SIZE"
  --num-workers "$NUM_WORKERS"
  --max-steps "$STEPS"
  --lr-schedule-steps "$STEPS"
  --lr-head "$LR_HEAD"
  --lr-online-backbone "$LR_ONLINE_BACKBONE"
  --dtype "$DTYPE"
  --d-model "$D_MODEL"
  --n-heads "$N_HEADS"
  --cross-ffn-multiplier "$CROSS_FFN_MULTIPLIER"
  --cross-target-block "$CROSS_TARGET_BLOCK"
  --seed "$SEED"
  --log-every 10
  --eval-every "$EVAL_EVERY"
  --eval-batches "$EVAL_BATCHES"
  --save-every "$SAVE_EVERY"
  --target-stride-macro "$TARGET_STRIDE_MACRO"
  --output "$OUTPUT"
)
if [[ "$VARIANT" == "B" ]]; then
  cmd+=(--semantic-cache-root "$SEMANTIC_CACHE")
fi

echo "[ABE train] variant=$VARIANT output=$OUTPUT seed=$SEED steps=$STEPS c8 S=$SEQUENCE_LENGTH"
echo "[ABE online] causal-transformer layers=$ONLINE_TRANSFORMER_LAYERS d=$D_MODEL heads=$ONLINE_TRANSFORMER_HEADS ffn=${ONLINE_TRANSFORMER_FFN_MULTIPLIER}x"
echo "[ABE contract] source=$SEMANTIC_SOURCE input=$SEMANTIC_INPUT_MODE fresh-init no-distillation"
exec "${cmd[@]}"
