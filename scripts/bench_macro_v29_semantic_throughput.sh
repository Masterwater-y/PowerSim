#!/usr/bin/env bash
# Bounded deployment-side free rollout.  The JSON report includes aggregate
# macro/s, UOP/s, mean step latency, and context/collate/Qwen stage timing.
set -euo pipefail

REPO=${REPO:-/data00/yinhaolang/LLMSim}
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
RUN_DIR=${RUN_DIR:?set RUN_DIR to a completed semantic training directory}
TRACE_ROOT=${TRACE_ROOT:?set TRACE_ROOT to one c8 packed trace}
STATIC_DICT=${STATIC_DICT:?set STATIC_DICT to that binary parquet}
SEMANTIC_CACHE=${SEMANTIC_CACHE:-$REPO/data/v29_macro_semantic_cache_c8}
MAX_STEPS=${MAX_STEPS:-120}
STRIDE_MACRO=${STRIDE_MACRO:-256}
DEVICE=${DEVICE:-cuda:0}
OUTPUT=${OUTPUT:-$RUN_DIR/deployment_throughput_smoke.json}

cd "$REPO"
export PYTHONPATH="$REPO:/data00/yinhaolang/TCSim:${PYTHONPATH:-}"
export HF_HUB_OFFLINE=${HF_HUB_OFFLINE:-1}
export TRANSFORMERS_OFFLINE=${TRANSFORMERS_OFFLINE:-1}

exec "$PY" eval/rollout_macro_v29_checkpoint.py \
  --run-dir "$RUN_DIR" \
  --trace-root "$TRACE_ROOT" \
  --static-dict "$STATIC_DICT" \
  --semantic-cache-root "$SEMANTIC_CACHE" \
  --max-steps "$MAX_STEPS" \
  --stride-macro "$STRIDE_MACRO" \
  --device "$DEVICE" \
  --output "$OUTPUT"
