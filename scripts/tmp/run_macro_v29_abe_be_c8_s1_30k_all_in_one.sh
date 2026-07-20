#!/usr/bin/env bash
# One detached command for the formal B/E retraining controls.
# Default behavior trains B then E.  Set RUN_DEPLOYMENT=1 to additionally run
# the matched 23-trace deployment suites and produce the A/B/E comparison.
set -euo pipefail

REPO=${REPO:-/data00/yinhaolang/LLMSim}
SCRIPT_PATH="$REPO/scripts/tmp/run_macro_v29_abe_be_c8_s1_30k_all_in_one.sh"
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
STAMP=${STAMP:-$(date +%Y%m%d_%H%M%S)}
EXPERIMENT_NAME=${EXPERIMENT_NAME:-macro_v29_abe_c8_s1_30k_seed1234_$STAMP}
LOG_ROOT=${LOG_ROOT:-$REPO/logs/tmp/$EXPERIMENT_NAME}
B_OUTPUT=${B_OUTPUT:-$REPO/ckpt/${EXPERIMENT_NAME}_B}
E_OUTPUT=${E_OUTPUT:-$REPO/ckpt/${EXPERIMENT_NAME}_E}
# Python multiprocessing appends pymp/listener suffixes to TMPDIR.  Keep the
# project-local prefix well below the Linux AF_UNIX 108-byte address limit.
TASK_TMPDIR=${TASK_TMPDIR:-$REPO/tmp/mv29abe/$STAMP}
SEMANTIC_CACHE=${SEMANTIC_CACHE:-$REPO/data/v29_macro_semantic_cache_c8}
A_RUN_DIR=${A_RUN_DIR:-$REPO/ckpt/macro_v29_vnext_d384_crossmacro_c8_s1_30k_20260719_025211}
A_SUMMARY=${A_SUMMARY:-$REPO/eval_results/macro_v29_vnext_c8_seed1_deploy23_20260719_134644/summary.json}
DATASET_MANIFEST=${DATASET_MANIFEST:-/data00/yinhaolang/TCSim/data/v29_global_time_dataset/manifest.json}
STATIC_MANIFEST=${STATIC_MANIFEST:-$REPO/data/v28_1/static_dict/manifest.jsonl}

if [[ "${MACRO_V29_ABE_BE_NOHUP_CHILD:-0}" != "1" ]]; then
  mkdir -p "$LOG_ROOT" "$TASK_TMPDIR"
  LAUNCHER_LOG="$LOG_ROOT/launcher.log"
  PID_FILE="$LOG_ROOT/launcher.pid"
  nohup env \
    MACRO_V29_ABE_BE_NOHUP_CHILD=1 \
    REPO="$REPO" PY="$PY" STAMP="$STAMP" \
    EXPERIMENT_NAME="$EXPERIMENT_NAME" LOG_ROOT="$LOG_ROOT" \
    B_OUTPUT="$B_OUTPUT" E_OUTPUT="$E_OUTPUT" TASK_TMPDIR="$TASK_TMPDIR" \
    SEMANTIC_CACHE="$SEMANTIC_CACHE" A_RUN_DIR="$A_RUN_DIR" \
    A_SUMMARY="$A_SUMMARY" DATASET_MANIFEST="$DATASET_MANIFEST" \
    STATIC_MANIFEST="$STATIC_MANIFEST" \
    GPUS="${GPUS:-0,1,2,3,4,5,6,7}" NPROC="${NPROC:-8}" \
    STEPS="${STEPS:-30000}" SEED="${SEED:-1234}" \
    RUN_PREFLIGHT="${RUN_PREFLIGHT:-1}" \
    RUN_DEPLOYMENT="${RUN_DEPLOYMENT:-0}" \
    MASTER_PORT_B="${MASTER_PORT_B:-29659}" \
    MASTER_PORT_E="${MASTER_PORT_E:-29669}" \
    bash "$SCRIPT_PATH" >"$LAUNCHER_LOG" 2>&1 < /dev/null &
  LAUNCHER_PID=$!
  printf '%s\n' "$LAUNCHER_PID" > "$PID_FILE"
  echo "started pid=$LAUNCHER_PID"
  echo "log=$LAUNCHER_LOG"
  echo "B_output=$B_OUTPUT"
  echo "E_output=$E_OUTPUT"
  exit 0
fi

cd "$REPO"
mkdir -p "$LOG_ROOT" "$TASK_TMPDIR"
if (( ${#TASK_TMPDIR} > 64 )); then
  echo "[ABE][ERROR] TASK_TMPDIR is too long for multiprocessing sockets: $TASK_TMPDIR" >&2
  exit 5
fi
export TMPDIR="$TASK_TMPDIR"
export PYTHONPATH="$REPO:/data00/yinhaolang/TCSim:${PYTHONPATH:-}"
STATUS_FILE="$LOG_ROOT/status.txt"
printf 'RUNNING\n' > "$STATUS_FILE"
mark_failed() {
  code=$?
  printf 'FAIL exit=%s\n' "$code" > "$STATUS_FILE"
  exit "$code"
}
trap mark_failed ERR

for required in \
  "$DATASET_MANIFEST" \
  "$STATIC_MANIFEST" \
  "$SEMANTIC_CACHE/manifest.json" \
  "$A_RUN_DIR/run.json" \
  "$A_RUN_DIR/final_report.json"; do
  if [[ ! -f "$required" ]]; then
    echo "[ABE][ERROR] missing prerequisite: $required" >&2
    exit 2
  fi
done
if [[ -e "$B_OUTPUT/run.json" || -e "$E_OUTPUT/run.json" ]]; then
  echo "[ABE][ERROR] B or E output already exists; use a fresh EXPERIMENT_NAME" >&2
  exit 3
fi

echo "[ABE] A reference=$A_RUN_DIR"
echo "[ABE] B output=$B_OUTPUT"
echo "[ABE] E output=$E_OUTPUT"
echo "[ABE] fixed contract: c8 seed=${SEED:-1234} S=1 steps=${STEPS:-30000} fresh-init"

if [[ "${RUN_PREFLIGHT:-1}" == "1" ]]; then
  echo "[1/3] A/B/E model, data and checkpoint contract tests"
  "$PY" -m unittest \
    tests.test_macro_v29_model \
    tests.test_macro_v29_semantic_model \
    tests.test_macro_v29_contract -v \
    2>&1 | tee "$LOG_ROOT/preflight.log"
fi

echo "[2/3] train formal B: ordinary causal Transformer + real semantic cache"
VARIANT=B \
OUTPUT="$B_OUTPUT" \
SEMANTIC_CACHE="$SEMANTIC_CACHE" \
MANIFEST="$DATASET_MANIFEST" \
STATIC_MANIFEST="$STATIC_MANIFEST" \
GPUS="${GPUS:-0,1,2,3,4,5,6,7}" \
NPROC="${NPROC:-8}" \
MASTER_PORT="${MASTER_PORT_B:-29659}" \
STEPS="${STEPS:-30000}" \
SEED="${SEED:-1234}" \
bash scripts/train_macro_v29_abe_variant_c8.sh \
  2>&1 | tee "$LOG_ROOT/train_B.log"

echo "[3/3] train formal E: identical Transformer + learned-null semantic input"
VARIANT=E \
OUTPUT="$E_OUTPUT" \
SEMANTIC_CACHE="$SEMANTIC_CACHE" \
MANIFEST="$DATASET_MANIFEST" \
STATIC_MANIFEST="$STATIC_MANIFEST" \
GPUS="${GPUS:-0,1,2,3,4,5,6,7}" \
NPROC="${NPROC:-8}" \
MASTER_PORT="${MASTER_PORT_E:-29669}" \
STEPS="${STEPS:-30000}" \
SEED="${SEED:-1234}" \
bash scripts/train_macro_v29_abe_variant_c8.sh \
  2>&1 | tee "$LOG_ROOT/train_E.log"

if [[ "${RUN_DEPLOYMENT:-0}" == "1" ]]; then
  if [[ ! -f "$A_SUMMARY" ]]; then
    echo "[ABE][ERROR] deployment comparison requires A summary: $A_SUMMARY" >&2
    exit 4
  fi
  B_EVAL="$REPO/eval_results/${EXPERIMENT_NAME}_B_deploy23"
  E_EVAL="$REPO/eval_results/${EXPERIMENT_NAME}_E_deploy23"
  ABE_EVAL="$REPO/eval_results/${EXPERIMENT_NAME}_comparison"
  echo "[deploy] B 23-trace c8 free-running suite"
  "$PY" scripts/run_macro_v29_c8_deployment_suite.py \
    --run-dir "$B_OUTPUT" \
    --dataset-manifest "$DATASET_MANIFEST" \
    --static-manifest "$STATIC_MANIFEST" \
    --semantic-cache "$SEMANTIC_CACHE" \
    --output-root "$B_EVAL" \
    --gpus "${GPUS:-0,1,2,3,4,5,6,7}" \
    --split deployment_inference --cores 8 --max-steps 0 \
    --stride-macro 256 --max-step-cycles 1024 --progress-every 100 \
    --tmp-root "$TASK_TMPDIR/deploy_B" \
    2>&1 | tee "$LOG_ROOT/deploy_B.log"
  echo "[deploy] E 23-trace c8 free-running suite"
  "$PY" scripts/run_macro_v29_c8_deployment_suite.py \
    --run-dir "$E_OUTPUT" \
    --dataset-manifest "$DATASET_MANIFEST" \
    --static-manifest "$STATIC_MANIFEST" \
    --output-root "$E_EVAL" \
    --gpus "${GPUS:-0,1,2,3,4,5,6,7}" \
    --split deployment_inference --cores 8 --max-steps 0 \
    --stride-macro 256 --max-step-cycles 1024 --progress-every 100 \
    --tmp-root "$TASK_TMPDIR/deploy_E" \
    2>&1 | tee "$LOG_ROOT/deploy_E.log"
  "$PY" scripts/summarize_macro_v29_abe.py \
    --a-summary "$A_SUMMARY" \
    --b-summary "$B_EVAL/summary.json" \
    --e-summary "$E_EVAL/summary.json" \
    --output-root "$ABE_EVAL" \
    2>&1 | tee "$LOG_ROOT/summarize_abe.log"
fi

trap - ERR
printf 'PASS\n' > "$STATUS_FILE"
echo "[ABE done] B=$B_OUTPUT"
echo "[ABE done] E=$E_OUTPUT"
echo "[ABE done] deployment=${RUN_DEPLOYMENT:-0}"
