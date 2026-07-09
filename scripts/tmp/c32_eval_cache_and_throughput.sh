#!/usr/bin/env bash
# One-shot C32 eval-cache rebuild plus cache-hit throughput smoke.
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TSim}
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
cd "$ROOT"

CKPT=${CKPT:-ckpt/v27_ss_tw5000_8l_t32768_bs1_20k_20260709_015028}
RAW=${RAW:-data/raw_trace_pool/activecore_eval/c32_seedB_infer17}
NUM_CORES=${NUM_CORES:-32}
ROW_GROUP_SIZE=${ROW_GROUP_SIZE:-65536}
CONVERT_JOBS=${CONVERT_JOBS:-4}
OVERWRITE_ALIGNED=${OVERWRITE_ALIGNED:-0}

GPUS_CACHE=${GPUS_CACHE:-0,1}
CACHE_EVAL_MODE=${CACHE_EVAL_MODE:-rebuild}
CACHE_MAX_WINDOWS=${CACHE_MAX_WINDOWS:-1}

THROUGHPUT_WORKLOAD=${THROUGHPUT_WORKLOAD:-W_stream}
THROUGHPUT_GPU=${THROUGHPUT_GPU:-0}
THROUGHPUT_MAX_WINDOWS=${THROUGHPUT_MAX_WINDOWS:-300}

MAX_LEN=${MAX_LEN:-32768}
QUERY_PLACEMENT=${QUERY_PLACEMENT:-tail_local}
PLANNER_STATE_SOURCE=${PLANNER_STATE_SOURCE:-pred}
INFER_DTYPE=${INFER_DTYPE:-bf16}
SDPA_BACKEND=${SDPA_BACKEND:-no_flash}
EVAL_CACHE_DIR=${EVAL_CACHE_DIR:-data/eval_columnar_cache}

DEFAULT_WORKLOADS=(
  W_ads_ctr W_ads_ranking_proxy W_branch_storm W_chase_dram
  W_compute_int W_false_sharing W_feed_ranking W_fp_compute_dense
  W_fp_lite W_graph_recall_proxy W_indirect W_int_div
  W_interest_graph_recall W_mlp_light W_search_index_proxy W_stream
)
read -r -a WORKLOADS_ARR <<< "${WORKLOADS:-${DEFAULT_WORKLOADS[*]}}"
WORKLOADS_STR="${WORKLOADS_ARR[*]}"

TS=$(date +%Y%m%d_%H%M%S)
LOG_DIR=${LOG_DIR:-logs/tmp/c32_eval_cache_throughput_${TS}}
mkdir -p "$LOG_DIR" "$EVAL_CACHE_DIR"

RAW_REAL=$(readlink -f "$RAW" || true)
if [[ -z "$RAW_REAL" || ! -d "$RAW_REAL" ]]; then
  echo "[error] missing RAW=$RAW" >&2
  exit 2
fi

echo "[meta] ROOT=$ROOT"
echo "[meta] CKPT=$CKPT"
echo "[meta] RAW=$RAW"
echo "[meta] RAW_REAL=$RAW_REAL"
echo "[meta] WORKLOADS=$WORKLOADS_STR"
echo "[meta] LOG_DIR=$LOG_DIR"
echo "[meta] EVAL_CACHE_DIR=$EVAL_CACHE_DIR"
echo

for wl in "${WORKLOADS_ARR[@]}"; do
  if [[ ! -d "$RAW_REAL/$wl/tao_trace" ]]; then
    echo "[error] missing tao_trace for workload=$wl path=$RAW_REAL/$wl/tao_trace" >&2
    exit 3
  fi
done

count_aligned() {
  local total=0
  local wl n
  for wl in "${WORKLOADS_ARR[@]}"; do
    n=$(find -L "$RAW_REAL/$wl/tao_trace" -maxdepth 1 -type f \
      -name '*.aligned.parquet' 2>/dev/null | wc -l | tr -d ' ')
    total=$((total + n))
  done
  echo "$total"
}

expected_aligned=$((NUM_CORES * ${#WORKLOADS_ARR[@]}))
aligned_before=$(count_aligned)
echo "[aligned] before=$aligned_before expected=$expected_aligned"

if (( aligned_before < expected_aligned || OVERWRITE_ALIGNED == 1 )); then
  echo "[aligned] converting raw jsonl -> aligned parquet"
  convert_fails=0
  for wl in "${WORKLOADS_ARR[@]}"; do
    while (( $(jobs -pr | wc -l) >= CONVERT_JOBS )); do
      wait -n || convert_fails=$((convert_fails + 1))
    done
    args=(
      scripts/convert_trace_to_aligned_parquet.py
      --raw-root "$RAW_REAL"
      --workloads "$wl"
      --row-group-size "$ROW_GROUP_SIZE"
    )
    if [[ "$OVERWRITE_ALIGNED" == "1" ]]; then
      args+=(--overwrite)
    fi
    echo "[convert] $wl -> $LOG_DIR/convert_${wl}.log"
    "$PY" "${args[@]}" > "$LOG_DIR/convert_${wl}.log" 2>&1 &
  done
  while (( $(jobs -pr | wc -l) > 0 )); do
    wait -n || convert_fails=$((convert_fails + 1))
  done
  if (( convert_fails > 0 )); then
    echo "[error] aligned parquet conversion failed count=$convert_fails" >&2
    exit 4
  fi
fi

aligned_after=$(count_aligned)
echo "[aligned] after=$aligned_after expected=$expected_aligned"
if (( aligned_after < expected_aligned )); then
  echo "[error] insufficient aligned parquet files" >&2
  exit 5
fi
echo

echo "[cache] rebuild columnar eval cache mode=$CACHE_EVAL_MODE"
TAG="v27_ss_tw5000_20k_best_c32_cachebuild"
CKPT="$CKPT" \
RAW="$RAW" \
TAG="$TAG" \
WORKLOADS="$WORKLOADS_STR" \
GPUS="$GPUS_CACHE" \
MAX_WINDOWS="$CACHE_MAX_WINDOWS" \
QUERY_PLACEMENT="$QUERY_PLACEMENT" \
PLANNER_STATE_SOURCE="$PLANNER_STATE_SOURCE" \
DEVICE=cuda \
INFER_DTYPE="$INFER_DTYPE" \
SDPA_BACKEND="$SDPA_BACKEND" \
EVAL_CACHE_MODE="$CACHE_EVAL_MODE" \
EVAL_CACHE_DIR="$EVAL_CACHE_DIR" \
PROGRESS_EVERY=0 \
bash scripts/eval_parallel.sh 2>&1 | tee "$LOG_DIR/cachebuild_eval_parallel.log"
echo

echo "[throughput] cache-hit smoke workload=$THROUGHPUT_WORKLOAD windows=$THROUGHPUT_MAX_WINDOWS gpu=$THROUGHPUT_GPU"
throughput_log="$LOG_DIR/throughput_${THROUGHPUT_WORKLOAD}.log"
/usr/bin/time -v env CUDA_VISIBLE_DEVICES="$THROUGHPUT_GPU" \
  "$PY" eval/eval_quota_cycles.py \
    --raw-root "$RAW" \
    --workload "$THROUGHPUT_WORKLOAD" \
    --ckpt "$CKPT" \
    --max-len "$MAX_LEN" \
    --max-windows "$THROUGHPUT_MAX_WINDOWS" \
    --query-placement "$QUERY_PLACEMENT" \
    --planner-state-source "$PLANNER_STATE_SOURCE" \
    --device cuda \
    --infer-dtype "$INFER_DTYPE" \
    --sdpa-backend "$SDPA_BACKEND" \
    --eval-cache-mode auto \
    --eval-cache-dir "$EVAL_CACHE_DIR" \
  2>&1 | tee "$throughput_log"

echo
echo "[summary] throughput log=$throughput_log"
if command -v rg >/dev/null 2>&1; then
  rg "eval-cache|\\[$THROUGHPUT_WORKLOAD\\].*windows|timing\\(avg/window\\)|per-window cpi_uop MAPE" \
    "$throughput_log" | tail -n 80 || true
else
  grep -E "eval-cache|\\[$THROUGHPUT_WORKLOAD\\].*windows|timing\\(avg/window\\)|per-window cpi_uop MAPE" \
    "$throughput_log" | tail -n 80 || true
fi

echo
echo "[done] logs=$LOG_DIR"
