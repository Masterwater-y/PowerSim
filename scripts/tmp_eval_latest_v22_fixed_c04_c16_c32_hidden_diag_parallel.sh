#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
cd "$ROOT"

CKPT_ROOT=${CKPT_ROOT:-ckpt/v22_fixed_centered_soft_c32mix_8gpu_18000}

find_latest_ckpt() {
  local root="$1"
  local latest=""
  local max_step=-1
  local d base step
  for d in "$root"/step_*; do
    [[ -d "$d" && -f "$d/head_best.pt" && -d "$d/lora_best" ]] || continue
    base=$(basename "$d")
    step=${base#step_}
    step=$((10#$step))
    if (( step > max_step )); then
      max_step=$step
      latest="$d"
    fi
  done
  if [[ -n "$latest" ]]; then
    printf '%s\n' "$latest"
    return 0
  fi
  if [[ -f "$root/head_best.pt" && -d "$root/lora_best" ]]; then
    printf '%s\n' "$root"
    return 0
  fi
  return 1
}

if [[ -z "${CKPT:-}" ]]; then
  CKPT=$(find_latest_ckpt "$CKPT_ROOT") || {
    echo "[multi-hidden][error] no usable ckpt under $CKPT_ROOT" >&2
    exit 2
  }
fi

TS=${TS:-$(date +%Y%m%d_%H%M%S)}
MAX_WINDOWS=${MAX_WINDOWS:-0}
LOAD_MAX_ROWS_PER_CORE=${LOAD_MAX_ROWS_PER_CORE:-0}
PLANNER_MODES=${PLANNER_MODES:-"pred"}
PROGRESS_EVERY=${PROGRESS_EVERY:-60}
WORKLOADS=${WORKLOADS:-"W_ads_ctr W_ads_ranking_proxy W_branch_storm W_chase_dram W_compute_int W_false_sharing W_feed_ranking W_fp_compute_dense W_fp_lite W_graph_recall_proxy W_indirect W_int_div W_interest_graph_recall W_mlp_light W_phased_mix W_search_index_proxy W_stream"}

mkdir -p logs

declare -a PIDS=()
declare -a NAMES=()
declare -a LOGS=()

run_core() {
  local c="$1"
  local gpus="$2"
  local raw="data/raw_v7_seedB_c${c}_infer17"
  local out="logs/v22_fixed_latest_c${c}_hidden_diag_${TS}"
  local log="logs/v22_fixed_latest_c${c}_hidden_diag_${TS}.driver.log"

  if [[ ! -d "$raw" ]]; then
    echo "[multi-hidden][error] raw root not found: $raw" >&2
    exit 2
  fi

  echo "[multi-hidden] launch c${c} gpus=${gpus} raw=${raw} out=${out} log=${log}"
  CKPT="$CKPT" \
  RAW="$raw" \
  OUT_ROOT="$out" \
  GPUS_CSV="$gpus" \
  MAX_WINDOWS="$MAX_WINDOWS" \
  LOAD_MAX_ROWS_PER_CORE="$LOAD_MAX_ROWS_PER_CORE" \
  PLANNER_MODES="$PLANNER_MODES" \
  WORKLOADS="$WORKLOADS" \
  DUMP_LLM_HIDDEN_METRICS=1 \
  HIDDEN_TOP_K="${HIDDEN_TOP_K:-16}" \
  PROGRESS_EVERY="$PROGRESS_EVERY" \
    bash scripts/run_v22_c32_core_spread_and_label_diag.sh > "$log" 2>&1
}

echo "[multi-hidden] ckpt=$CKPT"
echo "[multi-hidden] max_windows=$MAX_WINDOWS load_max_rows_per_core=$LOAD_MAX_ROWS_PER_CORE modes=$PLANNER_MODES"
echo "[multi-hidden] timestamp=$TS"

run_core 04 "${GPUS_C04:-0}" &
PIDS+=("$!")
NAMES+=("c04")
LOGS+=("logs/v22_fixed_latest_c04_hidden_diag_${TS}.driver.log")

run_core 16 "${GPUS_C16:-1,2}" &
PIDS+=("$!")
NAMES+=("c16")
LOGS+=("logs/v22_fixed_latest_c16_hidden_diag_${TS}.driver.log")

run_core 32 "${GPUS_C32:-3,4,5,6,7}" &
PIDS+=("$!")
NAMES+=("c32")
LOGS+=("logs/v22_fixed_latest_c32_hidden_diag_${TS}.driver.log")

status=0
for i in "${!PIDS[@]}"; do
  if wait "${PIDS[$i]}"; then
    echo "[multi-hidden] done ${NAMES[$i]}"
  else
    rc=$?
    status=$rc
    echo "[multi-hidden][error] failed ${NAMES[$i]} rc=$rc log=${LOGS[$i]}" >&2
    tail -120 "${LOGS[$i]}" >&2 || true
  fi
done

echo "[multi-hidden] outputs:"
for c in 04 16 32; do
  echo "  logs/v22_fixed_latest_c${c}_hidden_diag_${TS}"
done

exit "$status"
