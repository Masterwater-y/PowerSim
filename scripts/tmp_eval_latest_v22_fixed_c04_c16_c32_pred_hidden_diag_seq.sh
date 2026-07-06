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
    echo "[seq-pred][error] no usable ckpt under $CKPT_ROOT" >&2
    exit 2
  }
fi

TS=${TS:-$(date +%Y%m%d_%H%M%S)}
CORES=${CORES:-"04 16 32"}
GPUS_CSV=${GPUS_CSV:-0,1,2,3,4,5,6,7}
MAX_WINDOWS=${MAX_WINDOWS:-0}
LOAD_MAX_ROWS_PER_CORE=${LOAD_MAX_ROWS_PER_CORE:-0}
PROGRESS_EVERY=${PROGRESS_EVERY:-60}
WORKLOADS=${WORKLOADS:-"W_ads_ctr W_ads_ranking_proxy W_branch_storm W_chase_dram W_compute_int W_false_sharing W_feed_ranking W_fp_compute_dense W_fp_lite W_graph_recall_proxy W_indirect W_int_div W_interest_graph_recall W_mlp_light W_phased_mix W_search_index_proxy W_stream"}

mkdir -p logs

echo "[seq-pred] ckpt=$CKPT"
echo "[seq-pred] cores=$CORES gpus=$GPUS_CSV"
echo "[seq-pred] max_windows=$MAX_WINDOWS load_max_rows_per_core=$LOAD_MAX_ROWS_PER_CORE"
echo "[seq-pred] timestamp=$TS"

for C in $CORES; do
  RAW="data/raw_v7_seedB_c${C}_infer17"
  OUT_ROOT="logs/v22_fixed_latest_c${C}_pred_hidden_diag_seq_${TS}"

  if [[ ! -d "$RAW" ]]; then
    echo "[seq-pred][error] raw root not found: $RAW" >&2
    exit 2
  fi

  echo
  echo "============================================================"
  echo "[seq-pred] start c${C} $(date '+%F %T')"
  echo "[seq-pred] raw=$RAW"
  echo "[seq-pred] out=$OUT_ROOT"
  echo "============================================================"

  CKPT="$CKPT" \
  RAW="$RAW" \
  OUT_ROOT="$OUT_ROOT" \
  GPUS_CSV="$GPUS_CSV" \
  MAX_WINDOWS="$MAX_WINDOWS" \
  LOAD_MAX_ROWS_PER_CORE="$LOAD_MAX_ROWS_PER_CORE" \
  PLANNER_MODES=pred \
  WORKLOADS="$WORKLOADS" \
  DUMP_LLM_HIDDEN_METRICS=1 \
  HIDDEN_TOP_K="${HIDDEN_TOP_K:-16}" \
  PROGRESS_EVERY="$PROGRESS_EVERY" \
    bash scripts/run_v22_c32_core_spread_and_label_diag.sh

  echo "[seq-pred] done c${C} $(date '+%F %T')"
done

echo
echo "[seq-pred] all done"
for C in $CORES; do
  echo "  logs/v22_fixed_latest_c${C}_pred_hidden_diag_seq_${TS}"
done
