#!/usr/bin/env bash
# tmp_run_macro_v29_20k_8gpu.sh — one-shot 8-GPU 20k-step training launcher
# for the macro-v29 native-token pipeline.  Uses the prebuilt token cache and
# writes every temporary artefact under $REPO/tmp so /tmp stays clean.
#
# Usage (from the LLMSim root, after the token cache has finished):
#
#   nohup bash scripts/tmp_run_macro_v29_20k_8gpu.sh > \
#     tmp/macro_v29_8gpu_20k/nohup.log 2>&1 &
#   echo $!
#
# Env vars (all optional):
#   RUN_NAME        directory suffix under tmp/ and ckpt/
#   TARGET_STEPS    max_steps for the training loop (default 20000)
#   GPUS            comma list, default 0..7
#   MASTER_PORT     torchrun rendezvous port (default 29617)
#   BATCH_SIZE, MAX_TOKENS, SEQ_LEN, LR_HEAD, LR_LORA, LOG_EVERY,
#     EVAL_EVERY, SAVE_EVERY, EVAL_BATCHES, WARMUP_FRACTION, DTYPE
#   TRAIN_CORES     default 1,4,8,16,32 (the standard c01..c32 mix)
#   VALIDATION_SPLIT default train; use deployment_inference for deployment-side validation
#   DIAGNOSE_GRADIENTS_FROM_STEP debug-only local-gradient inspection (-1 disables)
#   LR_SCHEDULE_STEPS total LR schedule length; defaults to TARGET_STEPS
#   TOKEN_CACHE     default $REPO/data/v29_macro_token_cache
#
# The script fails fast if the token cache manifest is missing so we never
# silently fall back to online tokenization on 8 GPUs.

set -euo pipefail

REPO=/data00/yinhaolang/LLMSim
cd "$REPO"

RUN_NAME=${RUN_NAME:-macro_v29_real_8gpu_20k}
TARGET_STEPS=${TARGET_STEPS:-20000}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
NPROC=${NPROC:-8}
MASTER_PORT=${MASTER_PORT:-29628}

BATCH_SIZE=${BATCH_SIZE:-1}
MAX_TOKENS=${MAX_TOKENS:-4096}
SEQ_LEN=${SEQ_LEN:-1}
LR_HEAD=${LR_HEAD:-3e-4}
LR_LORA=${LR_LORA:-1e-4}
LOG_EVERY=${LOG_EVERY:-10}
EVAL_EVERY=${EVAL_EVERY:-500}
SAVE_EVERY=${SAVE_EVERY:-1000}
EVAL_BATCHES=${EVAL_BATCHES:-32}
WARMUP_FRACTION=${WARMUP_FRACTION:-0.03}
DTYPE=${DTYPE:-bf16}
TRAIN_CORES=${TRAIN_CORES:-1,4,8,16,32}
VALIDATION_SPLIT=${VALIDATION_SPLIT:-train}
FREEZE=${FREEZE:-0}
GRAD_CKPT=${GRAD_CKPT:-1}
DIAGNOSE_GRADIENTS_FROM_STEP=${DIAGNOSE_GRADIENTS_FROM_STEP:--1}
LR_SCHEDULE_STEPS=${LR_SCHEDULE_STEPS:-$TARGET_STEPS}

BASE_MODEL=${BASE_MODEL:-Qwen/Qwen2.5-Coder-1.5B-Instruct}
MANIFEST=${MANIFEST:-/data00/yinhaolang/TCSim/data/v29_global_time_dataset/manifest.json}
STATIC_MANIFEST=${STATIC_MANIFEST:-$REPO/data/v28_1/static_dict/manifest.jsonl}
TOKEN_CACHE=${TOKEN_CACHE:-$REPO/data/v29_macro_token_cache}

if [[ ! -f "$TOKEN_CACHE/manifest.json" ]]; then
  echo "[launch][ERROR] token cache manifest missing: $TOKEN_CACHE/manifest.json" >&2
  echo "  Run scripts/build_macro_v29_token_cache.sh first." >&2
  exit 2
fi

TMP_ROOT=${TMP_ROOT:-$REPO/tmp/$RUN_NAME}
CKPT_DIR=${CKPT_DIR:-$REPO/ckpt/$RUN_NAME}
LOG_DIR=${LOG_DIR:-$REPO/tmp/$RUN_NAME/logs}
mkdir -p "$TMP_ROOT" "$CKPT_DIR" "$LOG_DIR"

STAMP=$(date +%Y%m%dT%H%M%S)
TRAIN_LOG="$LOG_DIR/train_${STAMP}.log"
MONITOR_LOG="$LOG_DIR/monitor_${STAMP}.log"
PID_FILE="$TMP_ROOT/train.pid"
STATE_FILE="$TMP_ROOT/launcher_state.json"

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
TR=${TR:-/data00/yinhaolang/infer/.venv/bin/torchrun}

export CUDA_VISIBLE_DEVICES="$GPUS"
export TMPDIR="$TMP_ROOT"
# NOTE: intentionally do NOT override HF_HOME / TRANSFORMERS_CACHE.  The Qwen
# base model already lives in the user's default HF cache (~/.cache/huggingface)
# and pointing HF at an empty tmp dir together with HF_HUB_OFFLINE=1 breaks
# tokenizer/model loading on every rank.
export TOKENIZERS_PARALLELISM=false
export HF_HUB_OFFLINE=${HF_HUB_OFFLINE:-1}
export TRANSFORMERS_OFFLINE=${TRANSFORMERS_OFFLINE:-1}
export OMP_NUM_THREADS=${OMP_NUM_THREADS:-4}
export NCCL_P2P_DISABLE=${NCCL_P2P_DISABLE:-0}
export NCCL_IB_DISABLE=${NCCL_IB_DISABLE:-1}
export PYTORCH_CUDA_ALLOC_CONF=${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}
export PYTHONPATH="$REPO:/data00/yinhaolang/TCSim:${PYTHONPATH:-}"

echo "== macro-v29 8-GPU $TARGET_STEPS-step launcher ==" | tee -a "$MONITOR_LOG"
echo "  run_name       = $RUN_NAME"           | tee -a "$MONITOR_LOG"
echo "  ckpt_dir       = $CKPT_DIR"           | tee -a "$MONITOR_LOG"
echo "  train_log      = $TRAIN_LOG"          | tee -a "$MONITOR_LOG"
echo "  monitor_log    = $MONITOR_LOG"        | tee -a "$MONITOR_LOG"
echo "  gpus           = $GPUS"               | tee -a "$MONITOR_LOG"
echo "  base_model     = $BASE_MODEL"         | tee -a "$MONITOR_LOG"
echo "  token_cache    = $TOKEN_CACHE"        | tee -a "$MONITOR_LOG"
echo "  target_steps   = $TARGET_STEPS"       | tee -a "$MONITOR_LOG"
echo "  cores          = $TRAIN_CORES"        | tee -a "$MONITOR_LOG"
echo "  validation     = $VALIDATION_SPLIT"   | tee -a "$MONITOR_LOG"
echo "  batch/seq/max  = $BATCH_SIZE / $SEQ_LEN / $MAX_TOKENS" | tee -a "$MONITOR_LOG"
echo "  lr head/lora   = $LR_HEAD / $LR_LORA" | tee -a "$MONITOR_LOG"
echo "  log/eval/save  = $LOG_EVERY / $EVAL_EVERY / $SAVE_EVERY (eval_batches=$EVAL_BATCHES)" \
    | tee -a "$MONITOR_LOG"

cmd=(
  "$TR" --standalone --nproc-per-node="$NPROC" --master_port="$MASTER_PORT"
  train/train_macro_v29.py
  --manifest "$MANIFEST"
  --static-manifest "$STATIC_MANIFEST"
  --train-split train
  --validation-split "$VALIDATION_SPLIT"
  --cores "$TRAIN_CORES"
  --base-model "$BASE_MODEL"
  --semantic-variant real
  --sequence-length "$SEQ_LEN"
  --sequence-stride "$SEQ_LEN"
  --max-tokens "$MAX_TOKENS"
  --batch-size "$BATCH_SIZE"
  --max-steps "$TARGET_STEPS"
  --lr-head "$LR_HEAD"
  --lr-lora "$LR_LORA"
  --warmup-fraction "$WARMUP_FRACTION"
  --lr-schedule-steps "$LR_SCHEDULE_STEPS"
  --dtype "$DTYPE"
  --log-every "$LOG_EVERY"
  --eval-every "$EVAL_EVERY"
  --save-every "$SAVE_EVERY"
  --eval-batches "$EVAL_BATCHES"
  --token-cache-root "$TOKEN_CACHE"
  --output "$CKPT_DIR"
)
if [[ "$GRAD_CKPT" == "1" ]]; then
  cmd+=(--gradient-checkpointing)
fi
if [[ "$FREEZE" == "1" ]]; then
  cmd+=(--freeze-backbone)
fi
if [[ "$DIAGNOSE_GRADIENTS_FROM_STEP" -ge 0 ]]; then
  cmd+=(--diagnose-gradients-from-step "$DIAGNOSE_GRADIENTS_FROM_STEP")
fi

echo "  cmd            = ${cmd[*]}" | tee -a "$MONITOR_LOG"

# Launch training in its own session so the monitor and the launcher can exit
# independently if the user detaches.
setsid "${cmd[@]}" > "$TRAIN_LOG" 2>&1 &
TRAIN_PID=$!
echo "$TRAIN_PID" > "$PID_FILE"
cat > "$STATE_FILE" <<JSON
{
  "run_name": "$RUN_NAME",
  "train_pid": $TRAIN_PID,
  "train_log": "$TRAIN_LOG",
  "monitor_log": "$MONITOR_LOG",
  "ckpt_dir": "$CKPT_DIR",
  "target_steps": $TARGET_STEPS,
  "started_at": "$STAMP"
}
JSON
echo "[launch] spawned train pid=$TRAIN_PID"    | tee -a "$MONITOR_LOG"
echo "[launch] tail -f $TRAIN_LOG for live loss" | tee -a "$MONITOR_LOG"

# Background monitor: log a compact status line every MONITOR_INTERVAL seconds.
# The training script itself already prints structured `[macro train step=...]`
# lines every LOG_EVERY steps, so we grep those for progress.
MONITOR_INTERVAL=${MONITOR_INTERVAL:-120}
(
  while kill -0 "$TRAIN_PID" 2>/dev/null; do
    NOW=$(date '+%Y-%m-%dT%H:%M:%S')
    LAST_STEP=$(
      grep -oE '\[macro train step=[0-9]+\]' "$TRAIN_LOG" 2>/dev/null \
        | tail -1 \
        | grep -oE '[0-9]+' \
        || echo 0
    )
    LAST_VALIDATION=$(
      grep -oE "\[macro validation step=[0-9]+\][^\n]*" "$TRAIN_LOG" 2>/dev/null \
        | tail -1 \
        || echo "-"
    )
    LATEST_CKPT=$(ls -1t "$CKPT_DIR"/trainable_step*.pt 2>/dev/null | head -1 || true)
    LATEST_CKPT_NAME=${LATEST_CKPT:+$(basename "$LATEST_CKPT")}
    GPU_UTIL=$(
      nvidia-smi --query-gpu=utilization.gpu,memory.used \
        --format=csv,noheader,nounits 2>/dev/null \
        | awk -F',' '{gsub(/ /, ""); printf("gpu%d=%s%%/%sMiB ", NR-1, $1, $2)}' \
        || echo "gpu=?"
    )
    LOG_AGE=$(( $(date +%s) - $(stat -c %Y "$TRAIN_LOG" 2>/dev/null || date +%s) ))
    printf '[monitor %s] pid=%s step=%s ckpt=%s log_age=%ss %s\n' \
      "$NOW" "$TRAIN_PID" "$LAST_STEP" "${LATEST_CKPT_NAME:--}" "$LOG_AGE" "$GPU_UTIL" \
      >> "$MONITOR_LOG"
    if [[ "$LAST_VALIDATION" != "-" ]]; then
      printf '[monitor validation %s] %s\n' "$NOW" "$LAST_VALIDATION" >> "$MONITOR_LOG"
    fi
    sleep "$MONITOR_INTERVAL"
  done
  if [[ -f "$CKPT_DIR/final_report.json" ]] \
      && jq -e '.status == "PASS"' "$CKPT_DIR/final_report.json" >/dev/null 2>&1; then
    FINAL_STATUS=PASS
  else
    FINAL_STATUS=FAILED
  fi
  echo "[monitor] train pid=$TRAIN_PID exited status=$FINAL_STATUS at $(date '+%Y-%m-%dT%H:%M:%S')" \
    >> "$MONITOR_LOG"
) &
MONITOR_PID=$!
echo "[launch] monitor pid=$MONITOR_PID interval=${MONITOR_INTERVAL}s" | tee -a "$MONITOR_LOG"

echo "[launch] tips:"
echo "  tail -f $TRAIN_LOG              # per-step loss and validation"
echo "  tail -f $MONITOR_LOG            # coarse status + GPU utilisation"
echo "  ls -lt $CKPT_DIR/trainable_step*.pt | head"
echo "  cat $STATE_FILE"
