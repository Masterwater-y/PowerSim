#!/usr/bin/env bash
# Fast checkpoint validation on prebuilt windows + ids_cache.
# This does not read raw gem5 traces and therefore avoids the expensive
# JSONL merge/RD annotation path in eval_quota_cycles.py.
set -euo pipefail

ROOT=/data00/yinhaolang/LLMSim
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
cd "$ROOT"

export HF_HUB_OFFLINE=${HF_HUB_OFFLINE:-1}
export TOKENIZERS_PARALLELISM=${TOKENIZERS_PARALLELISM:-false}

DATA=${DATA:-data/windows_v7_c08_tq/windows.jsonl}
CKPT=${CKPT:-ckpt/v7_c08_absmiss_ddp8}
MAXLEN=${MAXLEN:-32768}
BS=${BS:-1}
GPU=${GPU:-0}
MAX_SAMPLES=${MAX_SAMPLES:-0}
NUM_WORKERS=${NUM_WORKERS:-0}
PROGRESS_EVERY=${PROGRESS_EVERY:-20}
LOG=${LOG:-logs/eval_windows_gpu.log}
WORKLOADS=${WORKLOADS:-}
NO_CACHE=${NO_CACHE:-0}

mkdir -p logs

args=(
  --data "$DATA"
  --ckpt "$CKPT"
  --max-len "$MAXLEN"
  --bs "$BS"
  --device cuda
  --num-workers "$NUM_WORKERS"
  --progress-every "$PROGRESS_EVERY"
)
if [[ "$NO_CACHE" == "1" ]]; then
  args+=(--no-cache)
else
  args+=(--require-cache)
fi
if [[ "$MAX_SAMPLES" != "0" ]]; then
  args+=(--max-samples "$MAX_SAMPLES")
fi
if [[ -n "$WORKLOADS" ]]; then
  for w in $WORKLOADS; do
    args+=(--workload "$w")
  done
fi

echo "[eval_windows] GPU=$GPU DATA=$DATA CKPT=$CKPT MAXLEN=$MAXLEN BS=$BS"
echo "[eval_windows] MAX_SAMPLES=$MAX_SAMPLES WORKLOADS=${WORKLOADS:-<all>} NO_CACHE=$NO_CACHE PROGRESS_EVERY=$PROGRESS_EVERY"
echo "[eval_windows] log=$LOG"

CUDA_VISIBLE_DEVICES="$GPU" "$PY" eval/eval.py "${args[@]}" > "$LOG" 2>&1
tail -120 "$LOG"
