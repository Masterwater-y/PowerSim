#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
cd "$ROOT"

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
CKPT=${CKPT:-ckpt/local_core_cpi_only_direct_scratch_8gpu_12000}
RAW=${RAW:-data/raw_v7_seedB_c08_infer17}
WORKLOAD=${WORKLOAD:-W_ads_ranking_proxy}
GPUS=${GPUS:-0,1}
MAX_LEN=${MAX_LEN:-32768}
MAX_WINDOWS=${MAX_WINDOWS:-0}
QUERY_PLACEMENT=${QUERY_PLACEMENT:-tail_local}
DEVICE=${DEVICE:-cuda}
TS=${TS:-$(date +%Y%m%d_%H%M%S)}
RUN_ROOT=${RUN_ROOT:-logs/cpi_only_adsproxy_core_timesync_${TS}}

if [[ ! -f "$CKPT/head_best.pt" || ! -d "$CKPT/lora_best" ]]; then
  echo "[error] checkpoint is missing head_best.pt or lora_best: $CKPT" >&2
  exit 1
fi
if [[ ! -f "$RAW/$WORKLOAD/stats.txt" || ! -d "$RAW/$WORKLOAD/tao_trace" ]]; then
  echo "[error] raw workload files missing: $RAW/$WORKLOAD" >&2
  exit 1
fi

IFS=',' read -r -a GPU_ARR <<< "$GPUS"
if [[ ${#GPU_ARR[@]} -eq 0 ]]; then
  echo "[error] GPUS is empty" >&2
  exit 2
fi

mkdir -p "$RUN_ROOT"

echo "[adsproxy-core-timesync] ckpt=$CKPT"
echo "[adsproxy-core-timesync] raw=$RAW workload=$WORKLOAD"
echo "[adsproxy-core-timesync] gpus=$GPUS device=$DEVICE max_windows=$MAX_WINDOWS"
echo "[adsproxy-core-timesync] out=$RUN_ROOT"

run_source() {
  local source="$1"
  local gpu="$2"
  local outdir="$RUN_ROOT/${source}"
  mkdir -p "$outdir"
  CKPT="$CKPT" \
  RAW="$RAW" \
  WORKLOAD="$WORKLOAD" \
  GPU="$gpu" \
  DEVICE="$DEVICE" \
  MAX_LEN="$MAX_LEN" \
  MAX_WINDOWS="$MAX_WINDOWS" \
  QUERY_PLACEMENT="$QUERY_PLACEMENT" \
  PLANNER_STATE_SOURCE="$source" \
  OUTDIR="$outdir" \
    bash scripts/run_v16_ads_oracle_cut_eval.sh > "$RUN_ROOT/${source}.log" 2>&1

  local dump="$outdir/${WORKLOAD}.windows.jsonl"
  "$PY" scripts/analyze_core_cpi_alignment_dump.py "$dump" \
    --out-json "$outdir/core_cpi_alignment.json" \
    > "$outdir/core_cpi_alignment.txt"
}

if [[ ${#GPU_ARR[@]} -ge 2 ]]; then
  run_source pred "${GPU_ARR[0]}" &
  pid_pred=$!
  run_source label "${GPU_ARR[1]}" &
  pid_label=$!
  wait "$pid_pred"
  wait "$pid_label"
else
  run_source pred "${GPU_ARR[0]}"
  run_source label "${GPU_ARR[0]}"
fi

echo
echo "[adsproxy-core-timesync] done"
echo "pred run   : $RUN_ROOT/pred/run.log"
echo "pred align : $RUN_ROOT/pred/alignment_analysis.txt"
echo "pred core  : $RUN_ROOT/pred/core_cpi_alignment.txt"
echo "label run  : $RUN_ROOT/label/run.log"
echo "label align: $RUN_ROOT/label/alignment_analysis.txt"
echo "label core : $RUN_ROOT/label/core_cpi_alignment.txt"
echo
echo "===== pred/free-running core CPI + time alignment ====="
cat "$RUN_ROOT/pred/core_cpi_alignment.txt"
echo
echo "===== label/oracle-cut core CPI + time alignment ====="
cat "$RUN_ROOT/label/core_cpi_alignment.txt"
