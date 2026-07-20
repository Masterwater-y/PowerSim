#!/usr/bin/env bash
# One detached command: build/verify c1-c32 semantics, then train A, B, and E.
set -euo pipefail

REPO=${REPO:-/data00/yinhaolang/LLMSim}
SCRIPT_PATH="$REPO/scripts/tmp/run_macro_v29_abe_c1_c32_s1_30k_all_in_one.sh"
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
BASE_MODEL=${BASE_MODEL:-Qwen/Qwen2.5-Coder-1.5B-Instruct}
CORES=${CORES:-1,4,8,16,32}
STAMP=${STAMP:-$(date +%Y%m%d_%H%M%S)}
EXPERIMENT_NAME=${EXPERIMENT_NAME:-macro_v29_abe_c1_c32_s1_30k_seed1234_$STAMP}
LOG_ROOT=${LOG_ROOT:-$REPO/logs/tmp/$EXPERIMENT_NAME}
A_OUTPUT=${A_OUTPUT:-$REPO/ckpt/${EXPERIMENT_NAME}_A}
B_OUTPUT=${B_OUTPUT:-$REPO/ckpt/${EXPERIMENT_NAME}_B}
E_OUTPUT=${E_OUTPUT:-$REPO/ckpt/${EXPERIMENT_NAME}_E}
# Keep the project-local prefix short enough for Python multiprocessing AF_UNIX.
TASK_TMPDIR=${TASK_TMPDIR:-$REPO/tmp/mv29m/$STAMP}
SEMANTIC_CACHE=${SEMANTIC_CACHE:-$REPO/data/v29_macro_semantic_cache_c1_c32}
DATASET_MANIFEST=${DATASET_MANIFEST:-/data00/yinhaolang/TCSim/data/v29_global_time_dataset/manifest.json}
STATIC_MANIFEST=${STATIC_MANIFEST:-$REPO/data/v28_1/static_dict/manifest.jsonl}

if [[ "${MACRO_V29_ABE_MIXED_NOHUP_CHILD:-0}" != "1" ]]; then
  mkdir -p "$LOG_ROOT" "$TASK_TMPDIR"
  LAUNCHER_LOG="$LOG_ROOT/launcher.log"
  PID_FILE="$LOG_ROOT/launcher.pid"
  nohup env \
    MACRO_V29_ABE_MIXED_NOHUP_CHILD=1 \
    REPO="$REPO" PY="$PY" BASE_MODEL="$BASE_MODEL" CORES="$CORES" \
    STAMP="$STAMP" EXPERIMENT_NAME="$EXPERIMENT_NAME" LOG_ROOT="$LOG_ROOT" \
    A_OUTPUT="$A_OUTPUT" B_OUTPUT="$B_OUTPUT" E_OUTPUT="$E_OUTPUT" \
    TASK_TMPDIR="$TASK_TMPDIR" SEMANTIC_CACHE="$SEMANTIC_CACHE" \
    DATASET_MANIFEST="$DATASET_MANIFEST" STATIC_MANIFEST="$STATIC_MANIFEST" \
    GPUS="${GPUS:-0,1,2,3,4,5,6,7}" NPROC="${NPROC:-8}" \
    STEPS="${STEPS:-30000}" SEED="${SEED:-1234}" \
    RUN_PREFLIGHT="${RUN_PREFLIGHT:-1}" RUN_CACHE="${RUN_CACHE:-1}" \
    BATCH_SIZE="${BATCH_SIZE:-1}" NUM_WORKERS="${NUM_WORKERS:-4}" \
    MASTER_PORT_A="${MASTER_PORT_A:-29649}" \
    MASTER_PORT_B="${MASTER_PORT_B:-29659}" \
    MASTER_PORT_E="${MASTER_PORT_E:-29669}" \
    bash "$SCRIPT_PATH" >"$LAUNCHER_LOG" 2>&1 < /dev/null &
  LAUNCHER_PID=$!
  printf '%s\n' "$LAUNCHER_PID" > "$PID_FILE"
  echo "started pid=$LAUNCHER_PID"
  echo "log=$LAUNCHER_LOG"
  echo "A_output=$A_OUTPUT"
  echo "B_output=$B_OUTPUT"
  echo "E_output=$E_OUTPUT"
  exit 0
fi

cd "$REPO"
mkdir -p "$LOG_ROOT" "$TASK_TMPDIR"
if (( ${#TASK_TMPDIR} > 64 )); then
  echo "[ABE mixed][ERROR] TASK_TMPDIR too long: $TASK_TMPDIR" >&2
  exit 5
fi
export TMPDIR="$TASK_TMPDIR"
export PYTHONPATH="$REPO:/data00/yinhaolang/TCSim:${PYTHONPATH:-}"
export TOKENIZERS_PARALLELISM=false
export HF_HUB_OFFLINE=${HF_HUB_OFFLINE:-1}
export TRANSFORMERS_OFFLINE=${TRANSFORMERS_OFFLINE:-1}
STATUS_FILE="$LOG_ROOT/status.txt"
printf 'RUNNING\n' > "$STATUS_FILE"
mark_failed() {
  code=$?
  printf 'FAIL exit=%s\n' "$code" > "$STATUS_FILE"
  exit "$code"
}
trap mark_failed ERR

for required in "$DATASET_MANIFEST" "$STATIC_MANIFEST"; do
  if [[ ! -f "$required" ]]; then
    echo "[ABE mixed][ERROR] missing prerequisite: $required" >&2
    exit 2
  fi
done
if [[ -e "$A_OUTPUT/run.json" || -e "$B_OUTPUT/run.json" || -e "$E_OUTPUT/run.json" ]]; then
  echo "[ABE mixed][ERROR] output already exists; use a fresh EXPERIMENT_NAME" >&2
  exit 3
fi

echo "[ABE mixed] model=$BASE_MODEL cores=$CORES S=1 steps=${STEPS:-30000}"
echo "[ABE mixed] train/validation are disjoint guarded partitions of split=train"
echo "[ABE mixed] A=$A_OUTPUT"
echo "[ABE mixed] B=$B_OUTPUT"
echo "[ABE mixed] E=$E_OUTPUT"

if [[ "${RUN_PREFLIGHT:-1}" == "1" ]]; then
  echo "[1/5] model, mixed-core sampler, data and checkpoint contract tests"
  "$PY" -m unittest \
    tests.test_macro_v29_model \
    tests.test_macro_v29_semantic_model \
    tests.test_macro_v29_contract -v \
    2>&1 | tee "$LOG_ROOT/preflight.log"
fi

if [[ "${RUN_CACHE:-1}" == "1" ]]; then
  echo "[2/5] build or strictly verify c1-c32 semantic cache"
  BASE_MODEL="$BASE_MODEL" \
  MANIFEST="$DATASET_MANIFEST" \
  STATIC_MANIFEST="$STATIC_MANIFEST" \
  SEMANTIC_CACHE="$SEMANTIC_CACHE" \
  CORES="$CORES" DEVICE="${CACHE_DEVICE:-cuda:0}" \
  DTYPE=bf16 BATCH_SIZE="${CACHE_BATCH_SIZE:-32}" \
  bash scripts/build_macro_v29_semantic_cache.sh \
    2>&1 | tee "$LOG_ROOT/build_semantic_cache.log"
elif [[ ! -f "$SEMANTIC_CACHE/manifest.json" ]]; then
  echo "[ABE mixed][ERROR] RUN_CACHE=0 but cache is missing: $SEMANTIC_CACHE" >&2
  exit 4
fi

echo "[3/5] train A: online Qwen LoRA + real cached semantics"
VARIANT=A OUTPUT="$A_OUTPUT" BASE_MODEL="$BASE_MODEL" CORES="$CORES" \
SEMANTIC_CACHE="$SEMANTIC_CACHE" MANIFEST="$DATASET_MANIFEST" \
STATIC_MANIFEST="$STATIC_MANIFEST" GPUS="${GPUS:-0,1,2,3,4,5,6,7}" \
NPROC="${NPROC:-8}" MASTER_PORT="${MASTER_PORT_A:-29649}" \
STEPS="${STEPS:-30000}" SEED="${SEED:-1234}" \
BATCH_SIZE="${BATCH_SIZE:-1}" NUM_WORKERS="${NUM_WORKERS:-4}" \
bash scripts/train_macro_v29_abe_variant.sh \
  2>&1 | tee "$LOG_ROOT/train_A.log"

echo "[4/5] train B: ordinary Transformer + real cached semantics"
VARIANT=B OUTPUT="$B_OUTPUT" BASE_MODEL="$BASE_MODEL" CORES="$CORES" \
SEMANTIC_CACHE="$SEMANTIC_CACHE" MANIFEST="$DATASET_MANIFEST" \
STATIC_MANIFEST="$STATIC_MANIFEST" GPUS="${GPUS:-0,1,2,3,4,5,6,7}" \
NPROC="${NPROC:-8}" MASTER_PORT="${MASTER_PORT_B:-29659}" \
STEPS="${STEPS:-30000}" SEED="${SEED:-1234}" \
BATCH_SIZE="${BATCH_SIZE:-1}" NUM_WORKERS="${NUM_WORKERS:-4}" \
bash scripts/train_macro_v29_abe_variant.sh \
  2>&1 | tee "$LOG_ROOT/train_B.log"

echo "[5/5] train E: identical Transformer + learned-null semantics"
VARIANT=E OUTPUT="$E_OUTPUT" BASE_MODEL="$BASE_MODEL" CORES="$CORES" \
SEMANTIC_CACHE="$SEMANTIC_CACHE" MANIFEST="$DATASET_MANIFEST" \
STATIC_MANIFEST="$STATIC_MANIFEST" GPUS="${GPUS:-0,1,2,3,4,5,6,7}" \
NPROC="${NPROC:-8}" MASTER_PORT="${MASTER_PORT_E:-29669}" \
STEPS="${STEPS:-30000}" SEED="${SEED:-1234}" \
BATCH_SIZE="${BATCH_SIZE:-1}" NUM_WORKERS="${NUM_WORKERS:-4}" \
bash scripts/train_macro_v29_abe_variant.sh \
  2>&1 | tee "$LOG_ROOT/train_E.log"

trap - ERR
printf 'PASS\n' > "$STATUS_FILE"
echo "[ABE mixed done] A=$A_OUTPUT"
echo "[ABE mixed done] B=$B_OUTPUT"
echo "[ABE mixed done] E=$E_OUTPUT"
