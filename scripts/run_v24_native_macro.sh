#!/usr/bin/env bash
# scripts/run_v24_native_macro.sh
#
# One-shot v24 native-macro pipeline:
#   1. Rebuild tensor cache in native_macro mode (reuses existing windows.jsonl)
#   2. Smoke 200 steps of LoRA SFT on Qwen2.5-Coder-1.5B-Instruct
#   3. If smoke does not NaN in first 20 log lines, kick full 8000 steps
#
# All artifacts go under logs/ and ckpt/.
# Reuses data/windows_v16_v9core_tail_local_all/windows.jsonl (no gem5 needed).

set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
cd "$ROOT"
mkdir -p logs ckpt

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
TR=${TR:-/data00/yinhaolang/infer/.venv/bin/torchrun}

BASE_MODEL=${BASE_MODEL:-Qwen/Qwen2.5-Coder-1.5B-Instruct}
GPUS=${GPUS:-8}
DATA=${DATA:-data/windows_v16_v9core_tail_local_all/windows.jsonl}
MAX_LEN=${MAX_LEN:-32768}
BS=${BS:-1}
SMOKE_STEPS=${SMOKE_STEPS:-200}
FULL_STEPS=${FULL_STEPS:-8000}
LR_LORA=${LR_LORA:-3e-5}
LR_HEAD=${LR_HEAD:-3e-4}
LR_EMB=${LR_EMB:-3e-4}
CACHE_JOBS=${CACHE_JOBS:-8}

CACHE_DIR="${DATA%.jsonl}.maxlen${MAX_LEN}.tensor_cache.native"
SMOKE_OUT=${SMOKE_OUT:-ckpt/v24_native_smoke_$(basename "$BASE_MODEL")_${SMOKE_STEPS}}
FULL_OUT=${FULL_OUT:-ckpt/v24_native_full_$(basename "$BASE_MODEL")_${FULL_STEPS}}
CACHE_LOG=logs/v24_native_cache.log
SMOKE_LOG=logs/v24_native_smoke.log
FULL_LOG=logs/v24_native_full.log

echo "[cfg] BASE_MODEL=$BASE_MODEL  GPUS=$GPUS  MAX_LEN=$MAX_LEN  BS=$BS"
echo "[cfg] LR_LORA=$LR_LORA  LR_HEAD=$LR_HEAD  LR_EMB=$LR_EMB"
echo "[cfg] cache -> $CACHE_DIR"
echo "[cfg] smoke -> $SMOKE_OUT ($SMOKE_STEPS steps)"
echo "[cfg] full  -> $FULL_OUT ($FULL_STEPS steps)"

echo "==== STAGE 0: build native-macro tensor cache ===="
"$PY" scripts/prepare_dataset_cache.py \
  --data "$DATA" \
  --base-model "$BASE_MODEL" \
  --max-len "$MAX_LEN" \
  --format tensor \
  --input-mode global \
  --inject-mode native_macro \
  --cache-out "$CACHE_DIR" \
  --jobs "$CACHE_JOBS" \
  --lines-per-shard 512 \
  > "$CACHE_LOG" 2>&1
tail -n 5 "$CACHE_LOG"
if [[ ! -s "$CACHE_DIR/manifest.pt" ]]; then
  echo "[fatal] cache manifest missing; see $CACHE_LOG"; exit 2
fi

launch_train() {
  local tag=$1 out=$2 log=$3 steps=$4 log_every=$5 eval_every=$6 val_frac=$7 save_every=$8
  mkdir -p "$out"
  echo "[$tag] launching -> log=$log out=$out steps=$steps"
  nohup "$TR" --nproc_per_node="$GPUS" \
    train/train_lora.py \
      --data "$DATA" \
      --base-model "$BASE_MODEL" \
      --inject-mode native_macro \
      --cache-path "$CACHE_DIR" \
      --out "$out" \
      --max-len "$MAX_LEN" \
      --steps "$steps" \
      --bs "$BS" \
      --grad-accum 1 \
      --lr-lora "$LR_LORA" \
      --lr-head "$LR_HEAD" \
      --lr-emb "$LR_EMB" \
      --local-fuse-mode add \
      --loss-weight-mode fixed \
      --lambda-cpi-abs 1.0 \
      --lambda-cycles 0.3 \
      --lambda-aux-pmu 0.05 \
      --lambda-centered-cpi 0.1 \
      --log-every "$log_every" \
      --eval-every "$eval_every" \
      --save-every "$save_every" \
      --val-frac "$val_frac" \
      --num-workers 2 \
    > "$log" 2>&1 &
  local pid=$!
  echo "[$tag] pid=$pid"
  wait "$pid"
  return $?
}

smoke_ok() {
  local log=$1
  [[ -s "$log" ]] || return 1
  if tail -n 400 "$log" | grep -Eiq "non-finite|nan|traceback|CUDA out of memory|RuntimeError"; then
    return 1
  fi
  # need at least one printed loss line
  tail -n 400 "$log" | grep -Eq "\\[step [0-9]+\\] loss=" || return 1
  return 0
}

echo "==== STAGE 1: SMOKE ===="
set +e
launch_train smoke "$SMOKE_OUT" "$SMOKE_LOG" "$SMOKE_STEPS" 10 100 0.05 0
smoke_rc=$?
set -e
echo "[smoke] rc=$smoke_rc"

if smoke_ok "$SMOKE_LOG"; then
  echo "==== STAGE 2: FULL ===="
  set +e
  launch_train full "$FULL_OUT" "$FULL_LOG" "$FULL_STEPS" 20 500 0.10 500
  full_rc=$?
  set -e
  echo "[full] rc=$full_rc"
  tail -n 40 "$FULL_LOG" || true
else
  echo "==== STAGE 2: SKIPPED (smoke failed gate) ===="
  echo "[hint] inspect: $SMOKE_LOG"
  echo "---last 40 lines of smoke log---"
  tail -n 40 "$SMOKE_LOG" || true
fi

echo "[all] finished."
