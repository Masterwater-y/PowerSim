#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
cd "$ROOT"

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
CKPT=${CKPT:-ckpt/local_core_cpi_only_direct_scratch_8gpu_12000}
RAW=${RAW:-data/raw_v7_seedB_c08_infer17}
DEVICE=${DEVICE:-cuda}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
MAX_LEN=${MAX_LEN:-32768}
MAX_WINDOWS=${MAX_WINDOWS:-0}
QUERY_PLACEMENT=${QUERY_PLACEMENT:-tail_local}
TS=${TS:-$(date +%Y%m%d_%H%M%S)}
RUN_ROOT=${RUN_ROOT:-logs/tq_forward_bad_workloads_cpi_only_${TS}}

WORKLOADS=${WORKLOADS:-"W_search_index_proxy W_compute_int W_branch_storm W_chase_dram W_stream W_phased_mix W_ads_ranking_proxy"}

if [[ ! -f "$CKPT/head_best.pt" || ! -d "$CKPT/lora_best" ]]; then
  echo "[error] checkpoint is missing head_best.pt or lora_best: $CKPT" >&2
  exit 1
fi
if [[ ! -d "$RAW" ]]; then
  echo "[error] raw eval root not found: $RAW" >&2
  exit 1
fi
for W in $WORKLOADS; do
  if [[ ! -f "$RAW/$W/stats.txt" || ! -d "$RAW/$W/tao_trace" ]]; then
    echo "[error] raw workload files missing: $RAW/$W" >&2
    exit 1
  fi
done

IFS=',' read -r -a GPU_ARR <<< "$GPUS"
if [[ ${#GPU_ARR[@]} -eq 0 ]]; then
  echo "[error] GPUS is empty" >&2
  exit 2
fi

mkdir -p "$RUN_ROOT"

echo "[tq-forward-diag] ckpt=$CKPT"
echo "[tq-forward-diag] raw=$RAW"
echo "[tq-forward-diag] workloads=$WORKLOADS"
echo "[tq-forward-diag] gpus=$GPUS device=$DEVICE max_windows=$MAX_WINDOWS"
echo "[tq-forward-diag] out=$RUN_ROOT"
echo

run_one() {
  local workload="$1"
  local gpu="$2"
  local outdir="$RUN_ROOT/$workload"
  local log="$RUN_ROOT/${workload}.driver.log"
  mkdir -p "$outdir"
  {
    echo "[job] workload=$workload gpu=$gpu start=$(date '+%F %T')"
    CKPT="$CKPT" \
    RAW="$RAW" \
    WORKLOAD="$workload" \
    GPU="$gpu" \
    DEVICE="$DEVICE" \
    MAX_LEN="$MAX_LEN" \
    MAX_WINDOWS="$MAX_WINDOWS" \
    QUERY_PLACEMENT="$QUERY_PLACEMENT" \
    PLANNER_STATE_SOURCE=tq_forward \
    OUTDIR="$outdir" \
      bash scripts/run_v16_ads_oracle_cut_eval.sh

    local dump="$outdir/${workload}.windows.jsonl"
    "$PY" scripts/analyze_core_cpi_alignment_dump.py "$dump" \
      --out-json "$outdir/core_cpi_alignment.json" \
      > "$outdir/core_cpi_alignment.txt"
    echo "[job] workload=$workload done=$(date '+%F %T')"
  } > "$log" 2>&1
}

read -r -a QUEUE <<< "$WORKLOADS"
declare -A GPU_PID
declare -A GPU_WORKLOAD
FAILS=0

launch_one() {
  local workload="$1"
  local gpu="$2"
  echo "[launch] gpu=$gpu workload=$workload"
  run_one "$workload" "$gpu" &
  GPU_PID[$gpu]=$!
  GPU_WORKLOAD[$gpu]=$workload
}

for gpu in "${GPU_ARR[@]}"; do
  if [[ ${#QUEUE[@]} -eq 0 ]]; then
    break
  fi
  workload=${QUEUE[0]}
  QUEUE=("${QUEUE[@]:1}")
  launch_one "$workload" "$gpu"
done

while true; do
  running=0
  for gpu in "${!GPU_PID[@]}"; do
    pid=${GPU_PID[$gpu]}
    workload=${GPU_WORKLOAD[$gpu]}
    if kill -0 "$pid" 2>/dev/null; then
      running=$((running + 1))
      continue
    fi

    rc=0
    wait "$pid" 2>/dev/null || rc=$?
    unset "GPU_PID[$gpu]"
    unset "GPU_WORKLOAD[$gpu]"
    if [[ $rc -ne 0 ]]; then
      echo "[error] workload=$workload gpu=$gpu rc=$rc log=$RUN_ROOT/${workload}.driver.log" >&2
      tail -80 "$RUN_ROOT/${workload}.driver.log" >&2 || true
      FAILS=$((FAILS + 1))
    else
      echo "[done] workload=$workload gpu=$gpu"
      grep -E "^(aggregate_cpi_uop|within_window_core|pred_true_start_err_mean_abs|pred_true_end_err_mean_abs|planner_tail_skew)" \
        "$RUN_ROOT/$workload/core_cpi_alignment.txt" || true
    fi

    if [[ ${#QUEUE[@]} -gt 0 ]]; then
      next=${QUEUE[0]}
      QUEUE=("${QUEUE[@]:1}")
      launch_one "$next" "$gpu"
      running=$((running + 1))
    fi
  done

  if [[ $running -eq 0 && ${#QUEUE[@]} -eq 0 ]]; then
    break
  fi
  sleep 5
done

echo
echo "[tq-forward-diag] finished fails=$FAILS out=$RUN_ROOT"
echo
for W in $WORKLOADS; do
  if [[ -s "$RUN_ROOT/$W/core_cpi_alignment.txt" ]]; then
    echo "===== $W ====="
    sed -n '1,18p' "$RUN_ROOT/$W/core_cpi_alignment.txt"
    echo
  fi
done

exit "$FAILS"
