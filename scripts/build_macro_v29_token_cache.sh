#!/usr/bin/env bash
# build_macro_v29_token_cache.sh — one-shot native-token cache builder for the
# macro-v29 training run.  Wraps data/build_macro_v29_token_cache.py, defaults
# to caching every workload referenced by the TCSim v29 manifest for the
# ``real`` semantic variant with the LLMSim training tokenizer.
#
# Env vars (all optional):
#   MANIFEST         v29 dataset manifest.json
#   STATIC_MANIFEST  static-dict manifest.jsonl
#   CACHE_ROOT       output cache directory
#   BASE_MODEL       HF tokenizer id
#   VARIANTS         comma list of {real,pseudo,register_rename}
#   SPLITS           comma list of manifest splits to cover
#   WORKLOADS        optional workload filter
#   NUM_WORKERS      parallel tokenization workers (default 8)
#   FORCE            set to 1 to overwrite existing cache files

set -euo pipefail

REPO=/data00/yinhaolang/LLMSim
cd "$REPO"

MANIFEST=${MANIFEST:-/data00/yinhaolang/TCSim/data/v29_global_time_dataset/manifest.json}
STATIC_MANIFEST=${STATIC_MANIFEST:-$REPO/data/v28_1/static_dict/manifest.jsonl}
CACHE_ROOT=${CACHE_ROOT:-$REPO/data/v29_macro_token_cache}
BASE_MODEL=${BASE_MODEL:-Qwen/Qwen2.5-Coder-1.5B-Instruct}
VARIANTS=${VARIANTS:-real}
SPLITS=${SPLITS:-train,validation,development_heldout,seed0_inference,deployment_inference,final_untouched}
WORKLOADS=${WORKLOADS:-}
NUM_WORKERS=${NUM_WORKERS:-8}
FORCE=${FORCE:-0}

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
LOG_DIR=${LOG_DIR:-$REPO/logs/macro_v29_token_cache}
mkdir -p "$LOG_DIR" "$CACHE_ROOT"

export PYTHONPATH="$REPO:/data00/yinhaolang/TCSim:${PYTHONPATH:-}"
export TOKENIZERS_PARALLELISM=false
export HF_HUB_OFFLINE=${HF_HUB_OFFLINE:-1}
export TRANSFORMERS_OFFLINE=${TRANSFORMERS_OFFLINE:-1}

echo "== macro-v29 native-token cache =="
echo "  manifest        = $MANIFEST"
echo "  static-manifest = $STATIC_MANIFEST"
echo "  cache-root      = $CACHE_ROOT"
echo "  base-model      = $BASE_MODEL"
echo "  variants        = $VARIANTS"
echo "  splits          = $SPLITS"
echo "  workloads       = ${WORKLOADS:-<all>}"
echo "  num-workers     = $NUM_WORKERS   force=$FORCE"

STAMP=$(date +%Y%m%dT%H%M%S)
LOG_FILE="$LOG_DIR/build_${STAMP}.log"

cmd=(
  "$PY" data/build_macro_v29_token_cache.py
  --manifest "$MANIFEST"
  --static-manifest "$STATIC_MANIFEST"
  --cache-root "$CACHE_ROOT"
  --base-model "$BASE_MODEL"
  --variants "$VARIANTS"
  --splits "$SPLITS"
  --num-workers "$NUM_WORKERS"
)
if [[ -n "$WORKLOADS" ]]; then
  cmd+=(--workloads "$WORKLOADS")
fi
if [[ "$FORCE" == "1" ]]; then
  cmd+=(--force)
fi

echo "  log             = $LOG_FILE"
echo "  cmd             = ${cmd[*]}"
"${cmd[@]}" 2>&1 | tee "$LOG_FILE"
echo "[macro-v29 token cache] done -> $CACHE_ROOT"
