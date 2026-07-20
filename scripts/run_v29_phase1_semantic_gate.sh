#!/usr/bin/env bash
# run_v29_phase1_semantic_gate.sh — Phase 1 semantic-gate one-shot driver.
#
# Stages:
#   1) build_static_prompt.py       (real + pseudo + shuffle variants)
#   2) train_phase1.py --variant real       (SMOKE_STEPS then optional FULL)
#   3) train_phase1.py --variant pseudo
#   4) train_phase1.py --variant side_only
#   5) semantic_gate_report.py      (aggregate PASS/FAIL)
#
# By default we run the SMOKE track (3 variants × 200 steps).  Set FULL=1 to
# also kick off the 8000-step full run for whichever variants are launched.
#
# Requires Phase 0 to be complete (static_dict + chunks + manifest).
#
# Env vars:
#   OUT           output root                (default data/v28_1)
#   CKPT_ROOT     ckpt output root           (default ckpt/phase1)
#   VARIANTS      comma-separated variants   (default real,pseudo,side_only)
#   GPUS          nproc-per-node             (default 8)
#   SMOKE_STEPS   default 200
#   FULL_STEPS    default 8000
#   FULL          set to 1 to also run full-length after smoke passes
#   BASE_MODEL    HF model id                (default Qwen/Qwen2.5-Coder-1.5B)
#   TRAIN_CORES   default 1                  (c01 primary + c04 auxiliary use "1,4")
#   VAL_CORES     default 1
#   BATCH         default 4
#   MAX_TOKENS    default 4096

set -euo pipefail

REPO=/data00/yinhaolang/LLMSim
cd "$REPO"

OUT=${OUT:-$REPO/data/v28_1}
CKPT_ROOT=${CKPT_ROOT:-$REPO/ckpt/phase1}
PROMPTS=${PROMPTS:-$OUT/prompts}
STATIC=${STATIC:-$OUT/static_dict}
CHUNKS=${CHUNKS:-$OUT/chunks}
MANIFEST=${MANIFEST:-$OUT/manifest.parquet}
VARIANTS=${VARIANTS:-real,pseudo,side_only}
GPUS=${GPUS:-8}
SMOKE_STEPS=${SMOKE_STEPS:-200}
FULL_STEPS=${FULL_STEPS:-8000}
FULL=${FULL:-0}
BASE_MODEL=${BASE_MODEL:-Qwen/Qwen2.5-Coder-1.5B-Instruct}
TRAIN_CORES=${TRAIN_CORES:-1}
VAL_CORES=${VAL_CORES:-1}
BATCH=${BATCH:-4}
MAX_TOKENS=${MAX_TOKENS:-4096}
LR_LORA=${LR_LORA:-1e-4}
LR_HEAD=${LR_HEAD:-5e-4}
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
TR=${TR:-/data00/yinhaolang/infer/.venv/bin/torchrun}
LOG_DIR=${LOG_DIR:-$REPO/logs/phase1}
mkdir -p "$LOG_DIR" "$CKPT_ROOT"
export PYTHONPATH="$REPO:/data00/yinhaolang/TCSim:${PYTHONPATH:-}"
export TOKENIZERS_PARALLELISM=false
export HF_HUB_OFFLINE=${HF_HUB_OFFLINE:-1}
export TRANSFORMERS_OFFLINE=${TRANSFORMERS_OFFLINE:-1}

echo "== Phase 1 semantic gate =="
echo "  variants=$VARIANTS  smoke_steps=$SMOKE_STEPS  full=$FULL  base=$BASE_MODEL"
echo "  train_cores=$TRAIN_CORES  val_cores=$VAL_CORES  batch=$BATCH  max_tokens=$MAX_TOKENS"

# ---------------------------------------------------------------------------
# Stage 1: build static prompts (only if missing).
# The prompt generator understands only asm variants (real/pseudo/shuffle/
# register_rename). "side_only" is a training-side switch that bypasses the
# LLM and therefore does not need a prompt file, so we drop it here.
# ---------------------------------------------------------------------------
PROMPT_VARIANTS=$(echo "$VARIANTS" | tr ',' '\n' | grep -v '^side_only$' | paste -sd, -)
if [[ -z "$PROMPT_VARIANTS" ]]; then
  echo "[skip] no LLM variants; only side_only requested"
elif [[ ! -f "$PROMPTS/manifest.jsonl" ]]; then
  echo "=== stage prompts ($PROMPT_VARIANTS) ==="
  "$PY" data/build_static_prompt.py \
    --static-dict-dir "$STATIC" \
    --out "$PROMPTS" \
    --variant "$PROMPT_VARIANTS" \
    2>&1 | tee "$LOG_DIR/stage_prompts.log"
else
  echo "[skip] prompts already built at $PROMPTS"
fi

train_one() {
  local variant=$1
  local steps=$2
  local suffix=$3
  local out="$CKPT_ROOT/${variant}_${suffix}"
  local log="$LOG_DIR/train_${variant}_${suffix}.log"
  mkdir -p "$out"
  if [[ -f "$out/final_report.json" ]]; then
    echo "[skip] $out already has final_report.json"
    return 0
  fi
  echo "=== train variant=$variant steps=$steps -> $out ==="
  local launcher
  if [[ "$GPUS" -gt 1 ]]; then
    launcher=("$TR" --standalone --nproc-per-node="$GPUS")
  else
    launcher=("$PY")
  fi
  "${launcher[@]}" train/train_phase1.py \
      --manifest "$MANIFEST" \
      --chunks-root "$CHUNKS" \
      --prompts-root "$PROMPTS" \
      --variant "$variant" \
      --cores "$TRAIN_CORES" \
      --val-cores "$VAL_CORES" \
      --base-model "$BASE_MODEL" \
      --max-tokens "$MAX_TOKENS" \
      --batch-size "$BATCH" \
      --max-steps "$steps" \
      --lr-lora "$LR_LORA" \
      --lr-head "$LR_HEAD" \
      --output "$out" \
      2>&1 | tee "$log"
}

# ---------------------------------------------------------------------------
# Stage 2-4: train each variant.
# SMOKE_STEPS=0 skips the smoke pass; the loop only kicks in when
# SMOKE_STEPS>0 and either FULL=0 (smoke-only) or SKIP_SMOKE!=1.
# ---------------------------------------------------------------------------
IFS=',' read -r -a VARIANT_ARR <<< "$VARIANTS"
SKIP_SMOKE=${SKIP_SMOKE:-0}
if [[ "$SMOKE_STEPS" -gt 0 && "$SKIP_SMOKE" != "1" ]]; then
  for v in "${VARIANT_ARR[@]}"; do
    train_one "$v" "$SMOKE_STEPS" "smoke${SMOKE_STEPS}"
  done
else
  echo "[skip smoke] SMOKE_STEPS=$SMOKE_STEPS SKIP_SMOKE=$SKIP_SMOKE"
fi

# ---------------------------------------------------------------------------
# Optional: full run after smoke (or as the only run when SMOKE_STEPS=0)
# ---------------------------------------------------------------------------
if [[ "$FULL" == "1" ]]; then
  for v in "${VARIANT_ARR[@]}"; do
    train_one "$v" "$FULL_STEPS" "full${FULL_STEPS}"
  done
fi

# ---------------------------------------------------------------------------
# Stage 5: semantic gate report
# ---------------------------------------------------------------------------
declare -A dirs
for v in "${VARIANT_ARR[@]}"; do
  if [[ "$FULL" == "1" ]]; then
    dirs["$v"]="$CKPT_ROOT/${v}_full${FULL_STEPS}"
  else
    dirs["$v"]="$CKPT_ROOT/${v}_smoke${SMOKE_STEPS}"
  fi
done
REPORT_ARGS=(--real "${dirs[real]:-$CKPT_ROOT/real_smoke${SMOKE_STEPS}}")
[[ -n "${dirs[pseudo]:-}" ]]    && REPORT_ARGS+=(--pseudo    "${dirs[pseudo]}")
[[ -n "${dirs[shuffle]:-}" ]]   && REPORT_ARGS+=(--shuffle   "${dirs[shuffle]}")
[[ -n "${dirs[side_only]:-}" ]] && REPORT_ARGS+=(--side-only "${dirs[side_only]}")

"$PY" eval/semantic_gate_report.py \
    "${REPORT_ARGS[@]}" \
    --output "$LOG_DIR/gate_summary.json" \
    2>&1 | tee "$LOG_DIR/gate_summary.log"

echo "[phase1] outputs: $CKPT_ROOT"
echo "[phase1] logs:    $LOG_DIR"
