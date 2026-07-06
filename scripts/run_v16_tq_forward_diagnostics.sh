#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
cd "$ROOT"

TS=${TS:-$(date +%Y%m%d_%H%M%S)}
CKPT=${CKPT:-ckpt/v16_v9core_tail_local_delta_rank_8gpu_8000/step_005000}
MAX_WINDOWS=${MAX_WINDOWS:-0}
GPUS_CSV=${GPUS_CSV:-0,1,2}
RUN_ROOT=${RUN_ROOT:-logs/v16_tq_forward_diagnostics_${TS}}

IFS=',' read -r -a GPUS <<< "$GPUS_CSV"
if [[ ${#GPUS[@]} -lt 3 ]]; then
  echo "[tq-forward][error] need at least 3 GPUs in GPUS_CSV, got: $GPUS_CSV" >&2
  exit 2
fi

mkdir -p "$RUN_ROOT"

echo "[tq-forward] root=$ROOT"
echo "[tq-forward] ckpt=$CKPT"
echo "[tq-forward] max_windows=$MAX_WINDOWS"
echo "[tq-forward] gpus=$GPUS_CSV"
echo "[tq-forward] run_root=$RUN_ROOT"

declare -a PIDS=()
declare -a NAMES=()

run_one() {
  local name="$1"
  local gpu="$2"
  local raw="$3"
  local workload="$4"

  local outdir="${RUN_ROOT}/${name}"
  local logfile="${RUN_ROOT}/${name}.log"

  echo "[tq-forward] launch name=$name gpu=$gpu raw=$raw workload=$workload"
  (
    CKPT="$CKPT" \
    GPU="$gpu" \
    RAW="$raw" \
    WORKLOAD="$workload" \
    PLANNER_STATE_SOURCE="tq_forward" \
    TAG="$name" \
    TS="$TS" \
    OUTDIR="$outdir" \
    MAX_WINDOWS="$MAX_WINDOWS" \
    bash scripts/run_v16_ads_oracle_cut_eval.sh
  ) > "$logfile" 2>&1 &

  PIDS+=("$!")
  NAMES+=("$name")
}

run_one "v16_c16_phased_tq_forward" "${GPUS[0]}" "data/raw_trace_pool/activecore_eval/c16_seedB_infer17" "W_phased_mix"
run_one "v16_c08_ads_tq_forward" "${GPUS[1]}" "data/raw_trace_pool/activecore_eval/c08_seedB_infer17" "W_ads_ranking_proxy"
run_one "v16_c16_ads_tq_forward" "${GPUS[2]}" "data/raw_trace_pool/activecore_eval/c16_seedB_infer17" "W_ads_ranking_proxy"

echo "[tq-forward] all jobs launched"

status=0
for i in "${!PIDS[@]}"; do
  pid="${PIDS[$i]}"
  name="${NAMES[$i]}"
  if wait "$pid"; then
    echo "[tq-forward] done name=$name"
  else
    rc=$?
    echo "[tq-forward][error] failed name=$name rc=$rc log=${RUN_ROOT}/${name}.log" >&2
    status=$rc
  fi
done

echo
echo "[tq-forward] summary files:"
for name in "${NAMES[@]}"; do
  echo "  ${RUN_ROOT}/${name}/alignment_analysis.txt"
done

echo
echo "[tq-forward] tail summaries:"
for name in "${NAMES[@]}"; do
  analysis="${RUN_ROOT}/${name}/alignment_analysis.txt"
  echo "===== $name ====="
  if [[ -s "$analysis" ]]; then
    cat "$analysis"
  else
    echo "[tq-forward][warn] missing analysis: $analysis"
    tail -80 "${RUN_ROOT}/${name}.log" || true
  fi
  echo
done

exit "$status"
