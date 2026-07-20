#!/usr/bin/env bash
# Detached deployment-side A/B/E evaluation for the completed mixed-core run.
# The deployment manifest currently provides 23 traces each for c4/c8/c16/c32.
set -euo pipefail

REPO=${REPO:-/data00/yinhaolang/LLMSim}
SCRIPT_PATH="$REPO/scripts/tmp/run_macro_v29_abe_c1_c32_deployment_all_in_one.sh"
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
STAMP=${STAMP:-$(date +%Y%m%d_%H%M%S)}
JOB_NAME=${JOB_NAME:-macro_v29_abe_c1_c32_deploy_$STAMP}
LOG_ROOT=${LOG_ROOT:-$REPO/logs/tmp/$JOB_NAME}
RESULT_ROOT=${RESULT_ROOT:-$REPO/eval_results/$JOB_NAME}
# Keep this project-local path short for multiprocessing AF_UNIX sockets.
TASK_TMPDIR=${TASK_TMPDIR:-$REPO/tmp/mv29d/$STAMP}

RUN_PREFIX=${RUN_PREFIX:-$REPO/ckpt/macro_v29_abe_c1_c32_s1_30k_seed1234_20260720_014144}
A_RUN_DIR=${A_RUN_DIR:-${RUN_PREFIX}_A}
B_RUN_DIR=${B_RUN_DIR:-${RUN_PREFIX}_B}
E_RUN_DIR=${E_RUN_DIR:-${RUN_PREFIX}_E}
SEMANTIC_CACHE=${SEMANTIC_CACHE:-$REPO/data/v29_macro_semantic_cache_c1_c32}
DATASET_MANIFEST=${DATASET_MANIFEST:-/data00/yinhaolang/TCSim/data/v29_global_time_dataset/manifest.json}
STATIC_MANIFEST=${STATIC_MANIFEST:-$REPO/data/v28_1/static_dict/manifest.jsonl}
DEPLOY_CORES=${DEPLOY_CORES:-4,8,16,32}

if [[ "${MACRO_V29_ABE_MIXED_DEPLOY_CHILD:-0}" != "1" ]]; then
  mkdir -p "$LOG_ROOT" "$RESULT_ROOT" "$TASK_TMPDIR"
  LAUNCHER_LOG="$LOG_ROOT/launcher.log"
  PID_FILE="$LOG_ROOT/launcher.pid"
  nohup env \
    MACRO_V29_ABE_MIXED_DEPLOY_CHILD=1 \
    REPO="$REPO" PY="$PY" STAMP="$STAMP" JOB_NAME="$JOB_NAME" \
    LOG_ROOT="$LOG_ROOT" RESULT_ROOT="$RESULT_ROOT" TASK_TMPDIR="$TASK_TMPDIR" \
    A_RUN_DIR="$A_RUN_DIR" B_RUN_DIR="$B_RUN_DIR" E_RUN_DIR="$E_RUN_DIR" \
    SEMANTIC_CACHE="$SEMANTIC_CACHE" DATASET_MANIFEST="$DATASET_MANIFEST" \
    STATIC_MANIFEST="$STATIC_MANIFEST" DEPLOY_CORES="$DEPLOY_CORES" \
    GPUS="${GPUS:-0,1,2,3,4,5,6,7}" \
    PROGRESS_EVERY="${PROGRESS_EVERY:-100}" \
    bash "$SCRIPT_PATH" >"$LAUNCHER_LOG" 2>&1 < /dev/null &
  LAUNCHER_PID=$!
  printf '%s\n' "$LAUNCHER_PID" > "$PID_FILE"
  echo "started pid=$LAUNCHER_PID"
  echo "log=$LAUNCHER_LOG"
  echo "status=$LOG_ROOT/status.txt"
  echo "results=$RESULT_ROOT"
  exit 0
fi

cd "$REPO"
mkdir -p "$LOG_ROOT" "$RESULT_ROOT" "$TASK_TMPDIR"
if (( ${#TASK_TMPDIR} > 56 )); then
  echo "[ABE deploy][ERROR] TASK_TMPDIR too long: $TASK_TMPDIR" >&2
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
  "$A_RUN_DIR/run.json" "$A_RUN_DIR/final_report.json" \
  "$B_RUN_DIR/run.json" "$B_RUN_DIR/final_report.json" \
  "$E_RUN_DIR/run.json" "$E_RUN_DIR/final_report.json" \
  "$SEMANTIC_CACHE/manifest.json" "$DATASET_MANIFEST" "$STATIC_MANIFEST"; do
  if [[ ! -f "$required" ]]; then
    echo "[ABE deploy][ERROR] missing prerequisite: $required" >&2
    exit 2
  fi
done

"$PY" - "$A_RUN_DIR" "$B_RUN_DIR" "$E_RUN_DIR" <<'PY'
import json
import pathlib
import sys

expected = {
    "A": ("qwen_lora", "cached_macro_soft_token"),
    "B": ("causal_transformer", "cached_macro_soft_token"),
    "E": ("causal_transformer", "learned_null_macro_token"),
}
for name, raw_path in zip(("A", "B", "E"), sys.argv[1:]):
    root = pathlib.Path(raw_path)
    run = json.loads((root / "run.json").read_text())
    final = json.loads((root / "final_report.json").read_text())
    actual = (run.get("online_backbone_type"), run.get("semantic_input_mode"))
    if actual != expected[name]:
        raise RuntimeError(f"{name} contract mismatch: {actual} != {expected[name]}")
    if final.get("status") != "PASS":
        raise RuntimeError(f"{name} training status is not PASS")
    checkpoint = pathlib.Path(final["checkpoint"])
    if not checkpoint.is_file():
        raise FileNotFoundError(checkpoint)
    if int(run.get("seed", -1)) != 1234 or int(run.get("sequence_length", -1)) != 1:
        raise RuntimeError(f"{name} is not the paired seed=1234, S=1 run")
print("[preflight] A/B/E contracts and final checkpoints: PASS")
PY

IFS=',' read -r -a CORE_LIST <<< "$DEPLOY_CORES"
if (( ${#CORE_LIST[@]} == 0 )); then
  echo "[ABE deploy][ERROR] DEPLOY_CORES is empty" >&2
  exit 3
fi

for core_raw in "${CORE_LIST[@]}"; do
  core=$((10#$core_raw))
  case "$core" in
    4|8|16|32) ;;
    *)
      echo "[ABE deploy][ERROR] deployment_inference has no supported c$core suite" >&2
      exit 4
      ;;
  esac
  core_tag=$(printf 'c%02d' "$core")
  echo "================================================================================"
  echo "[ABE deploy] $core_tag: 23 matched deployment traces per variant"

  for variant in A B E; do
    case "$variant" in
      A) run_dir="$A_RUN_DIR" ;;
      B) run_dir="$B_RUN_DIR" ;;
      E) run_dir="$E_RUN_DIR" ;;
    esac
    output="$RESULT_ROOT/$core_tag/$variant"
    if [[ -e "$output/summary.json" ]]; then
      echo "[ABE deploy][ERROR] refusing to overwrite: $output/summary.json" >&2
      exit 6
    fi
    semantic_args=()
    if [[ "$variant" != "E" ]]; then
      semantic_args=(--semantic-cache "$SEMANTIC_CACHE")
    fi
    echo "[$core_tag/$variant] full free-running rollout"
    "$PY" scripts/run_macro_v29_c8_deployment_suite.py \
      --run-dir "$run_dir" \
      --dataset-manifest "$DATASET_MANIFEST" \
      --static-manifest "$STATIC_MANIFEST" \
      "${semantic_args[@]}" \
      --output-root "$output" \
      --gpus "${GPUS:-0,1,2,3,4,5,6,7}" \
      --split deployment_inference \
      --cores "$core" \
      --max-steps 0 \
      --stride-macro 256 \
      --max-step-cycles 1024 \
      --progress-every "${PROGRESS_EVERY:-100}" \
      --tmp-root "$TASK_TMPDIR/${core_tag}_${variant}" \
      2>&1 | tee "$LOG_ROOT/deploy_${core_tag}_${variant}.log"
  done

  "$PY" scripts/summarize_macro_v29_abe.py \
    --a-summary "$RESULT_ROOT/$core_tag/A/summary.json" \
    --b-summary "$RESULT_ROOT/$core_tag/B/summary.json" \
    --e-summary "$RESULT_ROOT/$core_tag/E/summary.json" \
    --output-root "$RESULT_ROOT/$core_tag/comparison" \
    2>&1 | tee "$LOG_ROOT/summarize_${core_tag}.log"
done

FINAL_STATUS=PASS
printf 'PASS\n' > "$STATUS_FILE"
trap - EXIT
echo "[ABE deploy done] status=$STATUS_FILE"
echo "[ABE deploy done] results=$RESULT_ROOT"
