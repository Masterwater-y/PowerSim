#!/usr/bin/env bash
# Detached post-training A/B/E deployment evaluation.  This script never
# launches training: it evaluates the completed B/E runs, reuses the existing
# A deployment summary, then writes paired A-vs-B, B-vs-E and A-vs-E reports.
set -euo pipefail

REPO=${REPO:-/data00/yinhaolang/LLMSim}
SCRIPT_PATH="$REPO/scripts/tmp/run_macro_v29_abe_c8_deployment_all_in_one.sh"
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
STAMP=${STAMP:-$(date +%Y%m%d_%H%M%S)}
JOB_NAME=${JOB_NAME:-macro_v29_abe_c8_deploy23_$STAMP}
LOG_ROOT=${LOG_ROOT:-$REPO/logs/tmp/$JOB_NAME}
TASK_TMPDIR=${TASK_TMPDIR:-$REPO/tmp/mv29abe_eval/$STAMP}

B_RUN_DIR=${B_RUN_DIR:-$REPO/ckpt/macro_v29_abe_c8_s1_30k_seed1234_20260720_000540_B}
E_RUN_DIR=${E_RUN_DIR:-$REPO/ckpt/macro_v29_abe_c8_s1_30k_seed1234_20260720_000540_E}
A_SUMMARY=${A_SUMMARY:-$REPO/eval_results/macro_v29_vnext_c8_seed1_deploy23_20260719_134644/summary.json}
SEMANTIC_CACHE=${SEMANTIC_CACHE:-$REPO/data/v29_macro_semantic_cache_c8}
DATASET_MANIFEST=${DATASET_MANIFEST:-/data00/yinhaolang/TCSim/data/v29_global_time_dataset/manifest.json}
STATIC_MANIFEST=${STATIC_MANIFEST:-$REPO/data/v28_1/static_dict/manifest.jsonl}
B_OUTPUT=${B_OUTPUT:-$REPO/eval_results/${JOB_NAME}_B}
E_OUTPUT=${E_OUTPUT:-$REPO/eval_results/${JOB_NAME}_E}
COMPARISON_OUTPUT=${COMPARISON_OUTPUT:-$REPO/eval_results/${JOB_NAME}_comparison}

if [[ "${MACRO_V29_ABE_DEPLOY_NOHUP_CHILD:-0}" != "1" ]]; then
  mkdir -p "$LOG_ROOT" "$TASK_TMPDIR"
  LAUNCHER_LOG="$LOG_ROOT/launcher.log"
  PID_FILE="$LOG_ROOT/launcher.pid"
  nohup env \
    MACRO_V29_ABE_DEPLOY_NOHUP_CHILD=1 \
    REPO="$REPO" PY="$PY" STAMP="$STAMP" JOB_NAME="$JOB_NAME" \
    LOG_ROOT="$LOG_ROOT" TASK_TMPDIR="$TASK_TMPDIR" \
    B_RUN_DIR="$B_RUN_DIR" E_RUN_DIR="$E_RUN_DIR" \
    A_SUMMARY="$A_SUMMARY" SEMANTIC_CACHE="$SEMANTIC_CACHE" \
    DATASET_MANIFEST="$DATASET_MANIFEST" STATIC_MANIFEST="$STATIC_MANIFEST" \
    B_OUTPUT="$B_OUTPUT" E_OUTPUT="$E_OUTPUT" \
    COMPARISON_OUTPUT="$COMPARISON_OUTPUT" \
    GPUS="${GPUS:-0,1,2,3,4,5,6,7}" \
    PROGRESS_EVERY="${PROGRESS_EVERY:-100}" \
    bash "$SCRIPT_PATH" >"$LAUNCHER_LOG" 2>&1 < /dev/null &
  LAUNCHER_PID=$!
  printf '%s\n' "$LAUNCHER_PID" > "$PID_FILE"
  echo "started pid=$LAUNCHER_PID"
  echo "log=$LAUNCHER_LOG"
  echo "B_output=$B_OUTPUT"
  echo "E_output=$E_OUTPUT"
  echo "comparison=$COMPARISON_OUTPUT"
  exit 0
fi

cd "$REPO"
mkdir -p "$LOG_ROOT" "$TASK_TMPDIR"
if (( ${#TASK_TMPDIR} > 64 )); then
  echo "[ABE deploy][ERROR] TASK_TMPDIR is too long: $TASK_TMPDIR" >&2
  exit 5
fi
export TMPDIR="$TASK_TMPDIR"
export PYTHONPATH="$REPO:/data00/yinhaolang/TCSim:${PYTHONPATH:-}"
export TOKENIZERS_PARALLELISM=false
export HF_HUB_OFFLINE=${HF_HUB_OFFLINE:-1}
export TRANSFORMERS_OFFLINE=${TRANSFORMERS_OFFLINE:-1}

STATUS_FILE="$LOG_ROOT/status.txt"
FINAL_STATUS=RUNNING
printf 'RUNNING\n' > "$STATUS_FILE"
record_exit() {
  code=$?
  if [[ "$FINAL_STATUS" != "PASS" ]]; then
    printf 'FAIL exit=%s\n' "$code" > "$STATUS_FILE"
  fi
}
trap record_exit EXIT

for required in \
  "$B_RUN_DIR/run.json" "$B_RUN_DIR/final_report.json" \
  "$E_RUN_DIR/run.json" "$E_RUN_DIR/final_report.json" \
  "$A_SUMMARY" "$SEMANTIC_CACHE/manifest.json" \
  "$DATASET_MANIFEST" "$STATIC_MANIFEST"; do
  if [[ ! -f "$required" ]]; then
    echo "[ABE deploy][ERROR] missing prerequisite: $required" >&2
    exit 2
  fi
done
if [[ -e "$B_OUTPUT/summary.json" || -e "$E_OUTPUT/summary.json" ]]; then
  echo "[ABE deploy][ERROR] output already contains a summary; use a fresh JOB_NAME" >&2
  exit 3
fi

echo "[1/3] B deployment: real semantic cache + ordinary Transformer"
"$PY" scripts/run_macro_v29_c8_deployment_suite.py \
  --run-dir "$B_RUN_DIR" \
  --dataset-manifest "$DATASET_MANIFEST" \
  --static-manifest "$STATIC_MANIFEST" \
  --semantic-cache "$SEMANTIC_CACHE" \
  --output-root "$B_OUTPUT" \
  --gpus "${GPUS:-0,1,2,3,4,5,6,7}" \
  --split deployment_inference \
  --cores 8 \
  --max-steps 0 \
  --stride-macro 256 \
  --max-step-cycles 1024 \
  --progress-every "${PROGRESS_EVERY:-100}" \
  --tmp-root "$TASK_TMPDIR/B" \
  2>&1 | tee "$LOG_ROOT/deploy_B.log"

echo "[2/3] E deployment: learned-null input + ordinary Transformer"
"$PY" scripts/run_macro_v29_c8_deployment_suite.py \
  --run-dir "$E_RUN_DIR" \
  --dataset-manifest "$DATASET_MANIFEST" \
  --static-manifest "$STATIC_MANIFEST" \
  --output-root "$E_OUTPUT" \
  --gpus "${GPUS:-0,1,2,3,4,5,6,7}" \
  --split deployment_inference \
  --cores 8 \
  --max-steps 0 \
  --stride-macro 256 \
  --max-step-cycles 1024 \
  --progress-every "${PROGRESS_EVERY:-100}" \
  --tmp-root "$TASK_TMPDIR/E" \
  2>&1 | tee "$LOG_ROOT/deploy_E.log"

echo "[3/3] paired A/B/E summary"
"$PY" scripts/summarize_macro_v29_abe.py \
  --a-summary "$A_SUMMARY" \
  --b-summary "$B_OUTPUT/summary.json" \
  --e-summary "$E_OUTPUT/summary.json" \
  --output-root "$COMPARISON_OUTPUT" \
  2>&1 | tee "$LOG_ROOT/summarize.log"

FINAL_STATUS=PASS
printf 'PASS\n' > "$STATUS_FILE"
trap - EXIT
echo "[ABE deploy done] B=$B_OUTPUT/summary.json"
echo "[ABE deploy done] E=$E_OUTPUT/summary.json"
echo "[ABE deploy done] comparison=$COMPARISON_OUTPUT/summary.md"
