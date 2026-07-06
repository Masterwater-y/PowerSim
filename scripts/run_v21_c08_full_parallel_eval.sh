#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
cd "$ROOT"

CKPT=${CKPT:-ckpt/v21_local_core_direct_cycles_aux_scratch_8gpu_12000}
RAW=${RAW:-data/raw_v7_seedB_c08_infer17}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
MAX_LEN=${MAX_LEN:-32768}
MAX_WINDOWS=${MAX_WINDOWS:-0}
QUERY_PLACEMENT=${QUERY_PLACEMENT:-tail_local}
PROGRESS_EVERY=${PROGRESS_EVERY:-30}
FOREGROUND=${FOREGROUND:-0}

WORKLOADS=${WORKLOADS:-"W_ads_ctr W_ads_ranking_proxy W_branch_storm W_chase_dram W_compute_int W_false_sharing W_feed_ranking W_fp_compute_dense W_fp_lite W_graph_recall_proxy W_indirect W_int_div W_interest_graph_recall W_mlp_light W_phased_mix W_search_index_proxy W_stream"}

if [[ ! -f "$CKPT/head_best.pt" || ! -d "$CKPT/lora_best" ]]; then
  echo "[v21-c08-parallel][error] checkpoint is missing head_best.pt or lora_best: $CKPT" >&2
  exit 1
fi

if [[ ! -d "$RAW" ]]; then
  echo "[v21-c08-parallel][error] raw eval root not found: $RAW" >&2
  exit 1
fi

for W in $WORKLOADS; do
  if [[ ! -f "$RAW/$W/stats.txt" || ! -d "$RAW/$W/tao_trace" ]]; then
    echo "[v21-c08-parallel][error] workload raw files missing for $W under $RAW" >&2
    exit 1
  fi
done

TS=${TS:-$(date +%Y%m%d_%H%M%S)}
TAG=${TAG:-v21_direct_cycles_aux_best_c08_full_parallel_${TS}}
LOG=${LOG:-logs/eval_${TAG}.nohup.log}
PID_FILE=${PID_FILE:-logs/eval_${TAG}.pid}

mkdir -p logs

echo "[v21-c08-parallel] CKPT=$CKPT"
echo "[v21-c08-parallel] RAW=$RAW"
echo "[v21-c08-parallel] GPUS=$GPUS"
echo "[v21-c08-parallel] MAX_WINDOWS=$MAX_WINDOWS (0 means full)"
echo "[v21-c08-parallel] QUERY_PLACEMENT=$QUERY_PLACEMENT"
echo "[v21-c08-parallel] WORKLOADS=$WORKLOADS"
echo "[v21-c08-parallel] backend=scripts/eval_parallel.sh via scripts/run_cpi_only_c08_full_eval.sh"

CKPT="$CKPT" \
RAW="$RAW" \
TAG="$TAG" \
LOG="$LOG" \
PID_FILE="$PID_FILE" \
GPUS="$GPUS" \
MAX_LEN="$MAX_LEN" \
MAX_WINDOWS="$MAX_WINDOWS" \
QUERY_PLACEMENT="$QUERY_PLACEMENT" \
PROGRESS_EVERY="$PROGRESS_EVERY" \
FOREGROUND="$FOREGROUND" \
WORKLOADS="$WORKLOADS" \
  bash scripts/run_cpi_only_c08_full_eval.sh

echo
echo "[v21-c08-parallel] monitor:"
echo "  tail -f $LOG"
echo
echo "[v21-c08-parallel] final summary after completion:"
echo "  rg \"FINAL SUMMARY|AGG|logs in\" $LOG"
