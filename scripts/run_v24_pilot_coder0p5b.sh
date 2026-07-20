#!/usr/bin/env bash
# scripts/run_v24_pilot_coder0p5b.sh
#
# One-shot launcher for v24-pilot training:
#   1. 200-step smoke on Qwen2.5-Coder-0.5B (LoRA) to check loss doesn't NaN
#   2. If smoke's final loss looks sane, immediately kick off full 8000-step SFT
#
# Both stages run under nohup, logs land in logs/.
# Override env vars if needed (BASE_MODEL, STEPS, BS, MAX_LEN, GPUS, ...).

set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
cd "$ROOT"
mkdir -p logs ckpt

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
TR=${TR:-/data00/yinhaolang/infer/.venv/bin/torchrun}
GPUS=${GPUS:-8}
BASE_MODEL=${BASE_MODEL:-Qwen/Qwen2.5-Coder-0.5B}
DATA=${DATA:-data/windows_v16_v9core_tail_local_all/windows.jsonl}
MAX_LEN=${MAX_LEN:-32768}
BS=${BS:-2}
SMOKE_STEPS=${SMOKE_STEPS:-200}
FULL_STEPS=${FULL_STEPS:-8000}
LR_LORA=${LR_LORA:-1e-4}
LR_HEAD=${LR_HEAD:-1e-3}
LR_EMB=${LR_EMB:-1e-3}

SMOKE_OUT=${SMOKE_OUT:-ckpt/v24_smoke_coder0p5b_${SMOKE_STEPS}}
FULL_OUT=${FULL_OUT:-ckpt/v24_pilot_coder0p5b_${FULL_STEPS}}
SMOKE_LOG=logs/v24_smoke_coder0p5b_${SMOKE_STEPS}.log
FULL_LOG=logs/v24_pilot_coder0p5b_${FULL_STEPS}.log

echo "[cfg] BASE_MODEL=$BASE_MODEL GPUS=$GPUS MAX_LEN=$MAX_LEN BS=$BS"
echo "[cfg] SMOKE_STEPS=$SMOKE_STEPS -> $SMOKE_OUT"
echo "[cfg] FULL_STEPS=$FULL_STEPS -> $FULL_OUT"
echo "[cfg] logs: $SMOKE_LOG , $FULL_LOG"

launch_cmd=(
  "$TR" --nproc_per_node="$GPUS"
  train/train_lora.py
  --data "$DATA"
  --base-model "$BASE_MODEL"
  --max-len "$MAX_LEN"
  --bs "$BS"
  --grad-accum 1
  --lr-lora "$LR_LORA"
  --lr-head "$LR_HEAD"
  --lr-emb "$LR_EMB"
  --local-fuse-mode add
  --loss-weight-mode fixed
  --lambda-cpi-abs 1.0
  --lambda-cycles 1.0
  --lambda-aux-pmu 0.05
  --lambda-centered-cpi 0.3
  --num-workers 2
)

run_stage() {
  local tag="$1" out="$2" log="$3" steps="$4" log_every="$5" eval_every="$6" val_frac="$7" save_every="$8"
  mkdir -p "$out"
  echo "[$tag] launching -> log=$log out=$out steps=$steps"
  nohup "${launch_cmd[@]}" \
    --out "$out" \
    --steps "$steps" \
    --log-every "$log_every" \
    --eval-every "$eval_every" \
    --save-every "$save_every" \
    --val-frac "$val_frac" \
    > "$log" 2>&1 &
  local pid=$!
  echo "[$tag] pid=$pid"
  wait "$pid"
  local rc=$?
  echo "[$tag] rc=$rc"
  return "$rc"
}

smoke_ok() {
  # PASS if we see finite loss > 0 in the last 40 log lines and no NaN/RuntimeError
  local log="$1"
  if ! [[ -s "$log" ]]; then return 1; fi
  if tail -n 200 "$log" | grep -Eiq "nan|error|traceback|RuntimeError|CUDA out of memory"; then
    echo "[gate] smoke log contains error markers; not proceeding"
    return 1
  fi
  local ok
  ok=$(tail -n 200 "$log" | grep -Ec "loss[= ][0-9]|val_loss")
  if (( ok < 1 )); then
    echo "[gate] no loss line found in smoke log"
    return 1
  fi
  return 0
}

echo "==== STAGE 1: SMOKE ===="
run_stage smoke "$SMOKE_OUT" "$SMOKE_LOG" "$SMOKE_STEPS" 10 100 0.05 0 || true

if smoke_ok "$SMOKE_LOG"; then
  echo "==== STAGE 2: FULL ===="
  run_stage full "$FULL_OUT" "$FULL_LOG" "$FULL_STEPS" 20 500 0.10 500 || true
  echo "[done] tail full log:"
  tail -n 40 "$FULL_LOG" || true
else
  echo "==== STAGE 2: SKIPPED (smoke failed gate) ===="
  echo "[hint] inspect: $SMOKE_LOG"
fi

echo "[all] finished."
