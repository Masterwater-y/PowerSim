#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
cd "$ROOT"

export TMPDIR="${TMPDIR:-$ROOT/tmp}"
mkdir -p "$TMPDIR" logs

CKPT=${CKPT:-ckpt/v22_v16_bind_split_direct_no_tstart_8gpu_12000}
CORES=${CORES:-"04 08 16 32"}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
MAX_LEN=${MAX_LEN:-32768}
MAX_WINDOWS=${MAX_WINDOWS:-0}
QUERY_PLACEMENT=${QUERY_PLACEMENT:-tail_local}
PROGRESS_EVERY=${PROGRESS_EVERY:-30}
RUN_TS=${RUN_TS:-$(date +%Y%m%d_%H%M%S)}
DRIVER_LOG=${DRIVER_LOG:-logs/eval_v22_bind_split_direct_c04_c08_c16_c32_${RUN_TS}.driver.log}

WORKLOADS=${WORKLOADS:-"W_ads_ctr W_ads_ranking_proxy W_branch_storm W_chase_dram W_compute_int W_false_sharing W_feed_ranking W_fp_compute_dense W_fp_lite W_graph_recall_proxy W_indirect W_int_div W_interest_graph_recall W_mlp_light W_phased_mix W_search_index_proxy W_stream"}

if [[ ! -f "$CKPT/head_best.pt" || ! -d "$CKPT/lora_best" ]]; then
  echo "[v22-eval][error] checkpoint missing head_best.pt or lora_best: $CKPT" >&2
  exit 1
fi

{
  echo "[v22-eval] start $(date '+%F %T')"
  echo "[v22-eval] CKPT=$CKPT"
  echo "[v22-eval] CORES=$CORES"
  echo "[v22-eval] GPUS=$GPUS"
  echo "[v22-eval] MAX_LEN=$MAX_LEN MAX_WINDOWS=$MAX_WINDOWS"
  echo "[v22-eval] QUERY_PLACEMENT=$QUERY_PLACEMENT"
  echo "[v22-eval] WORKLOADS=$WORKLOADS"
  echo "[v22-eval] RUN_TS=$RUN_TS"
  echo

  for C in $CORES; do
    RAW="data/raw_v7_seedB_c${C}_infer17"
    TAG="v22_bind_split_direct_best_c${C}_full_${RUN_TS}"

    if [[ ! -d "$RAW" ]]; then
      echo "[v22-eval][error] raw root not found: $RAW" >&2
      exit 2
    fi

    echo "============================================================"
    echo "[v22-eval] c${C} start $(date '+%F %T')"
    echo "[v22-eval] RAW=$RAW"
    echo "[v22-eval] TAG=$TAG"
    echo "============================================================"

    CKPT="$CKPT" \
    RAW="$RAW" \
    TAG="$TAG" \
    GPUS="$GPUS" \
    MAX_LEN="$MAX_LEN" \
    MAX_WINDOWS="$MAX_WINDOWS" \
    QUERY_PLACEMENT="$QUERY_PLACEMENT" \
    PROGRESS_EVERY="$PROGRESS_EVERY" \
    WORKLOADS="$WORKLOADS" \
    FOREGROUND=1 \
      bash scripts/run_cpi_only_c08_full_eval.sh

    echo "[v22-eval] c${C} done $(date '+%F %T')"
    echo
  done

  echo "[v22-eval] all done $(date '+%F %T')"
  echo "[v22-eval] summaries:"
  for C in $CORES; do
    echo "  rg \"FINAL SUMMARY|AGG|logs in\" logs/eval_parallel_v22_bind_split_direct_best_c${C}_full_${RUN_TS}_*"
  done
} 2>&1 | tee "$DRIVER_LOG"

echo "[v22-eval] driver_log=$DRIVER_LOG"
