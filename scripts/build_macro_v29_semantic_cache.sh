#!/usr/bin/env bash
# Build the frozen-Qwen semantic table for every selected c8 binary before
# training or deployment.  The online process never loads this offline model.
set -euo pipefail

REPO=${REPO:-/data00/yinhaolang/LLMSim}
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
BASE_MODEL=${BASE_MODEL:-Qwen/Qwen2.5-Coder-1.5B-Instruct}
MANIFEST=${MANIFEST:-/data00/yinhaolang/TCSim/data/v29_global_time_dataset/manifest.json}
STATIC_MANIFEST=${STATIC_MANIFEST:-$REPO/data/v28_1/static_dict/manifest.jsonl}
SEMANTIC_CACHE=${SEMANTIC_CACHE:-$REPO/data/v29_macro_semantic_cache_c8}
CORES=${CORES:-8}
SPLITS=${SPLITS:-train,validation,development_heldout,seed0_inference,deployment_inference,final_untouched}
WORKLOADS=${WORKLOADS:-}
DEVICE=${DEVICE:-cuda:0}
DTYPE=${DTYPE:-bf16}
BATCH_SIZE=${BATCH_SIZE:-32}
VERIFY_SAMPLES=${VERIFY_SAMPLES:-4}

cd "$REPO"
export PYTHONPATH="$REPO:/data00/yinhaolang/TCSim:${PYTHONPATH:-}"
export TOKENIZERS_PARALLELISM=false
export HF_HUB_OFFLINE=${HF_HUB_OFFLINE:-1}
export TRANSFORMERS_OFFLINE=${TRANSFORMERS_OFFLINE:-1}

cmd=(
  "$PY" data/build_macro_v29_semantic_cache.py
  --manifest "$MANIFEST"
  --static-manifest "$STATIC_MANIFEST"
  --cache-root "$SEMANTIC_CACHE"
  --base-model "$BASE_MODEL"
  --cores "$CORES"
  --splits "$SPLITS"
  --device "$DEVICE"
  --dtype "$DTYPE"
  --batch-size "$BATCH_SIZE"
  --verify-samples "$VERIFY_SAMPLES"
)
if [[ -n "$WORKLOADS" ]]; then
  cmd+=(--workloads "$WORKLOADS")
fi
if [[ "${FORCE:-0}" == "1" ]]; then
  cmd+=(--force)
fi

echo "[semantic cache launch] root=$SEMANTIC_CACHE cores=$CORES device=$DEVICE"
exec "${cmd[@]}"
