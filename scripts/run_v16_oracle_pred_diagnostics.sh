#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
cd "$ROOT"

TS=${TS:-$(date +%Y%m%d_%H%M%S)}
CKPT=${CKPT:-ckpt/v16_v9core_tail_local_delta_rank_8gpu_8000/step_005000}
MAX_WINDOWS=${MAX_WINDOWS:-0}
GPUS_CSV=${GPUS_CSV:-0,1,2,3}
RUN_ROOT=${RUN_ROOT:-logs/v16_oracle_pred_diagnostics_${TS}}

IFS=',' read -r -a GPUS <<< "$GPUS_CSV"
if [[ ${#GPUS[@]} -lt 4 ]]; then
  echo "[diag][error] need at least 4 GPUs in GPUS_CSV, got: $GPUS_CSV" >&2
  exit 2
fi

mkdir -p "$RUN_ROOT"

echo "[diag] root=$ROOT"
echo "[diag] ckpt=$CKPT"
echo "[diag] max_windows=$MAX_WINDOWS"
echo "[diag] gpus=$GPUS_CSV"
echo "[diag] run_root=$RUN_ROOT"

declare -a PIDS=()
declare -a NAMES=()

run_one() {
  local name="$1"
  local gpu="$2"
  local raw="$3"
  local workload="$4"
  local planner="$5"

  local outdir="${RUN_ROOT}/${name}"
  local logfile="${RUN_ROOT}/${name}.log"

  echo "[diag] launch name=$name gpu=$gpu raw=$raw workload=$workload planner=$planner"
  (
    CKPT="$CKPT" \
    GPU="$gpu" \
    RAW="$raw" \
    WORKLOAD="$workload" \
    PLANNER_STATE_SOURCE="$planner" \
    TAG="$name" \
    TS="$TS" \
    OUTDIR="$outdir" \
    MAX_WINDOWS="$MAX_WINDOWS" \
    bash scripts/run_v16_ads_oracle_cut_eval.sh
  ) > "$logfile" 2>&1 &

  PIDS+=("$!")
  NAMES+=("$name")
}

run_one "v16_c08_ads_label_cut" "${GPUS[0]}" "data/raw_trace_pool/activecore_eval/c08_seedB_infer17" "W_ads_ranking_proxy" "label"
run_one "v16_c08_ads_pred_cut" "${GPUS[1]}" "data/raw_trace_pool/activecore_eval/c08_seedB_infer17" "W_ads_ranking_proxy" "pred"
run_one "v16_c16_phased_label_cut" "${GPUS[2]}" "data/raw_trace_pool/activecore_eval/c16_seedB_infer17" "W_phased_mix" "label"
run_one "v16_c16_phased_pred_cut" "${GPUS[3]}" "data/raw_trace_pool/activecore_eval/c16_seedB_infer17" "W_phased_mix" "pred"

echo "[diag] all jobs launched"

status=0
for i in "${!PIDS[@]}"; do
  pid="${PIDS[$i]}"
  name="${NAMES[$i]}"
  if wait "$pid"; then
    echo "[diag] done name=$name"
  else
    rc=$?
    echo "[diag][error] failed name=$name rc=$rc log=${RUN_ROOT}/${name}.log" >&2
    status=$rc
  fi
done

echo
echo "[diag] summary files:"
for name in "${NAMES[@]}"; do
  echo "  ${RUN_ROOT}/${name}/alignment_analysis.txt"
done

echo
echo "[diag] tail summaries:"
for name in "${NAMES[@]}"; do
  analysis="${RUN_ROOT}/${name}/alignment_analysis.txt"
  echo "===== $name ====="
  if [[ -s "$analysis" ]]; then
    cat "$analysis"
  else
    echo "[diag][warn] missing analysis: $analysis"
    tail -80 "${RUN_ROOT}/${name}.log" || true
  fi
  echo
done

exit "$status"
