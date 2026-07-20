#!/usr/bin/env bash
# Stage-1 same-checkpoint post-hoc semantic interventions; owns nohup.
set -euo pipefail

REPO=${REPO:-/data00/yinhaolang/LLMSim}
SCRIPT_PATH="$REPO/scripts/tmp/run_macro_v29_stage1_posthoc_c8_all_in_one.sh"
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
RUN_DIR=${RUN_DIR:-$REPO/ckpt/macro_v29_vnext_d384_crossmacro_c8_s1_30k_20260719_025211}
CHECKPOINT=${CHECKPOINT:-$RUN_DIR/trainable_step00030000.pt}
BASELINE_SUMMARY=${BASELINE_SUMMARY:-$REPO/eval_results/macro_v29_vnext_c8_seed1_deploy23_20260719_134644/summary.json}
DATASET_MANIFEST=${DATASET_MANIFEST:-/data00/yinhaolang/TCSim/data/v29_global_time_dataset/manifest.json}
STATIC_MANIFEST=${STATIC_MANIFEST:-$REPO/data/v28_1/static_dict/manifest.jsonl}
SEMANTIC_CACHE=${SEMANTIC_CACHE:-$REPO/data/v29_macro_semantic_cache_c8}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
INTERVENTION_SEED=${INTERVENTION_SEED:-20260719}
EXPERIMENT_NAME=${EXPERIMENT_NAME:-macro_v29_stage1_posthoc_c8_$(date +%Y%m%d_%H%M%S)}
EXPERIMENT_ROOT=${EXPERIMENT_ROOT:-$REPO/eval_results/$EXPERIMENT_NAME}
LOG_ROOT=${LOG_ROOT:-$REPO/logs/tmp/$EXPERIMENT_NAME}
TASK_TMPDIR=${TASK_TMPDIR:-$REPO/tmp/mv29stage1}

if [[ "${MACRO_V29_STAGE1_NOHUP_CHILD:-0}" != "1" ]]; then
  mkdir -p "$LOG_ROOT" "$EXPERIMENT_ROOT" "$TASK_TMPDIR"
  LAUNCHER_LOG="$LOG_ROOT/launcher.log"
  PID_FILE="$LOG_ROOT/launcher.pid"
  nohup env \
    MACRO_V29_STAGE1_NOHUP_CHILD=1 \
    REPO="$REPO" PY="$PY" RUN_DIR="$RUN_DIR" CHECKPOINT="$CHECKPOINT" \
    BASELINE_SUMMARY="$BASELINE_SUMMARY" \
    DATASET_MANIFEST="$DATASET_MANIFEST" STATIC_MANIFEST="$STATIC_MANIFEST" \
    SEMANTIC_CACHE="$SEMANTIC_CACHE" GPUS="$GPUS" \
    INTERVENTION_SEED="$INTERVENTION_SEED" \
    EXPERIMENT_NAME="$EXPERIMENT_NAME" EXPERIMENT_ROOT="$EXPERIMENT_ROOT" \
    LOG_ROOT="$LOG_ROOT" TASK_TMPDIR="$TASK_TMPDIR" \
    bash "$SCRIPT_PATH" >"$LAUNCHER_LOG" 2>&1 < /dev/null &
  LAUNCHER_PID=$!
  echo "$LAUNCHER_PID" >"$PID_FILE"
  echo "started pid=$LAUNCHER_PID"
  echo "log=$LAUNCHER_LOG"
  echo "output=$EXPERIMENT_ROOT"
  exit 0
fi

cd "$REPO"
mkdir -p "$LOG_ROOT" "$EXPERIMENT_ROOT" "$TASK_TMPDIR"
export TMPDIR="$TASK_TMPDIR"
export PYTHONPATH="$REPO:/data00/yinhaolang/TCSim:${PYTHONPATH:-}"
export HF_HUB_OFFLINE=${HF_HUB_OFFLINE:-1}
export TRANSFORMERS_OFFLINE=${TRANSFORMERS_OFFLINE:-1}
export TOKENIZERS_PARALLELISM=false

for variant in no_offline_hidden no_lora semantic_permute no_llm_branch; do
  echo "[stage1] begin variant=$variant at $(date --iso-8601=seconds)"
  "$PY" scripts/run_macro_v29_c8_deployment_suite.py \
    --run-dir "$RUN_DIR" \
    --dataset-manifest "$DATASET_MANIFEST" \
    --static-manifest "$STATIC_MANIFEST" \
    --semantic-cache "$SEMANTIC_CACHE" \
    --output-root "$EXPERIMENT_ROOT/$variant" \
    --gpus "$GPUS" \
    --split deployment_inference \
    --cores 8 \
    --max-steps 0 \
    --stride-macro 256 \
    --progress-every 100 \
    --tmp-root "$TASK_TMPDIR/$variant" \
    --posthoc-intervention "$variant" \
    --intervention-seed "$INTERVENTION_SEED" \
    --activation-diagnostics
  echo "[stage1] end variant=$variant at $(date --iso-8601=seconds)"
done

"$PY" scripts/summarize_macro_v29_stage1_posthoc.py \
  --experiment-root "$EXPERIMENT_ROOT" \
  --baseline-summary "$BASELINE_SUMMARY" \
  --checkpoint "$CHECKPOINT" \
  --bootstrap-seed "$INTERVENTION_SEED"

echo "[stage1] complete at $(date --iso-8601=seconds)"
