#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
cd "$ROOT"

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
CKPT=${CKPT:-ckpt/v14_headonly_qwen3_0p6b_c01_c04_c08_c16_8000}
BASE_MODEL=${BASE_MODEL:-Qwen/Qwen3-0.6B-Base}
WORKLOAD=${WORKLOAD:-W_ads_ranking_proxy}
CORES=${CORES:-04 08 16}
GPUS=${GPUS:-0 1 2}
MAX_LEN=${MAX_LEN:-32768}
TRAIN_MAX_LEN=${TRAIN_MAX_LEN:-32768}
MAX_WINDOWS=${MAX_WINDOWS:-0}
OUT=${OUT:-logs/eval_v14_planner_label_ads_$(date +%Y%m%d_%H%M%S)}

mkdir -p "$OUT"
read -r -a GPU_LIST <<< "$GPUS"

echo "[run] out=$OUT"
echo "[run] ckpt=$CKPT"
echo "[run] cores=$CORES gpus=$GPUS workload=$WORKLOAD"

i=0
for C in $CORES; do
  GPU=${GPU_LIST[$((i % ${#GPU_LIST[@]}))]}
  RAW="data/raw_trace_pool/activecore_eval/c${C}_seedB_infer17"
  LOG="$OUT/c${C}_${WORKLOAD}.log"
  if [[ ! -d "$RAW" ]]; then
    echo "[error] missing $RAW" >&2
    exit 2
  fi

  echo "[start] c${C} gpu=$GPU log=$LOG"
  nohup env CUDA_VISIBLE_DEVICES="$GPU" HF_HUB_OFFLINE=1 "$PY" eval/eval_quota_cycles.py \
    --raw-root "$RAW" \
    --workload "$WORKLOAD" \
    --ckpt "$CKPT" \
    --base-model "$BASE_MODEL" \
    --dt-target 8000 \
    --dt-max 12000 \
    --max-len "$MAX_LEN" \
    --train-max-len "$TRAIN_MAX_LEN" \
    --max-windows "$MAX_WINDOWS" \
    --planner-state-source label \
    > "$LOG" 2>&1 &
  echo "$!" > "$OUT/c${C}.pid"
  i=$((i + 1))
done

echo "[done] launched. progress:"
echo "tail -n 40 $OUT/c04_${WORKLOAD}.log"
