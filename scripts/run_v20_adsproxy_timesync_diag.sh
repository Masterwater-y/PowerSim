#!/usr/bin/env bash
set -euo pipefail

cd "${ROOT:-/data00/yinhaolang/LLMSim}"

CKPT=${CKPT:-ckpt/v20_local_core_direct_fixed_8gpu_8000_spreadfix/step_003500}
RAW=${RAW:-data/raw_trace_pool/activecore_eval/c08_seedB_infer17}
WORKLOAD=${WORKLOAD:-W_ads_ranking_proxy}
GPUS=${GPUS:-0,1}
MAX_WINDOWS=${MAX_WINDOWS:-0}
MAX_LEN=${MAX_LEN:-32768}

TS=${TS:-$(date +%Y%m%d_%H%M%S)}
MODE=$([[ "$MAX_WINDOWS" == "0" ]] && echo full || echo "mw${MAX_WINDOWS}")
RUN_ROOT=${RUN_ROOT:-logs/v20_step3500_c08_adsproxy_timesync_${MODE}_${TS}}

echo "[v20-timesync] ckpt=$CKPT"
echo "[v20-timesync] workload=$WORKLOAD max_windows=$MAX_WINDOWS gpus=$GPUS"
echo "[v20-timesync] out=$RUN_ROOT"

RUN_ROOT="$RUN_ROOT" \
CKPT="$CKPT" \
RAW="$RAW" \
RUN_EVAL=0 \
RUN_ALIGNMENT=1 \
ALIGN_WORKLOADS="$WORKLOAD" \
PLANNER_SOURCES="pred label" \
ALIGN_GPUS="$GPUS" \
ALIGN_MAX_WINDOWS="$MAX_WINDOWS" \
QUERY_PLACEMENT=tail_local \
MAX_LEN="$MAX_LEN" \
bash scripts/run_v18_c08_diagnostics.sh

echo
echo "[v20-timesync] label analysis: $RUN_ROOT/align_${WORKLOAD}_label/alignment_analysis.txt"
echo "[v20-timesync] pred analysis : $RUN_ROOT/align_${WORKLOAD}_pred/alignment_analysis.txt"
