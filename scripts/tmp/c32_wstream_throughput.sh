#!/usr/bin/env bash
# One-shot C32 W_stream cache-hit throughput smoke.
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TSim}
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
cd "$ROOT"

CKPT=${CKPT:-ckpt/v27_ss_tw5000_8l_t32768_bs1_20k_20260709_015028}
RAW=${RAW:-data/raw_trace_pool/activecore_eval/c32_seedB_infer17}
WORKLOAD=${WORKLOAD:-W_stream}
GPU=${GPU:-0}
MAX_WINDOWS=${MAX_WINDOWS:-300}
MAX_LEN=${MAX_LEN:-32768}
QUERY_PLACEMENT=${QUERY_PLACEMENT:-tail_local}
PLANNER_STATE_SOURCE=${PLANNER_STATE_SOURCE:-pred}
INFER_DTYPE=${INFER_DTYPE:-bf16}
SDPA_BACKEND=${SDPA_BACKEND:-no_flash}
EVAL_CACHE_DIR=${EVAL_CACHE_DIR:-data/eval_columnar_cache}
CACHE_BUILD_IF_MISSING=${CACHE_BUILD_IF_MISSING:-1}

TS=$(date +%Y%m%d_%H%M%S)
LOG_DIR=${LOG_DIR:-logs/tmp/c32_wstream_throughput_${TS}}
mkdir -p "$LOG_DIR" "$EVAL_CACHE_DIR"

echo "[meta] ROOT=$ROOT"
echo "[meta] CKPT=$CKPT"
echo "[meta] RAW=$RAW"
echo "[meta] WORKLOAD=$WORKLOAD"
echo "[meta] GPU=$GPU"
echo "[meta] MAX_WINDOWS=$MAX_WINDOWS"
echo "[meta] LOG_DIR=$LOG_DIR"
echo

if [[ ! -d "$RAW/$WORKLOAD/tao_trace" ]]; then
  echo "[error] missing trace dir: $RAW/$WORKLOAD/tao_trace" >&2
  exit 2
fi

cache_path=$("$PY" - "$RAW" "$WORKLOAD" "$EVAL_CACHE_DIR" <<'PY'
import sys
from eval.eval_quota_cycles import _eval_cache_path

raw, workload, cache_dir = sys.argv[1:4]
path, files, _sig = _eval_cache_path(
    f"{raw}/{workload}/tao_trace", 8192, cache_dir)
print(path or "")
PY
)

aligned_count=$("$PY" - "$RAW" "$WORKLOAD" <<'PY'
import sys
from eval.eval_quota_cycles import _eval_cache_path

raw, workload = sys.argv[1:3]
_path, files, _sig = _eval_cache_path(
    f"{raw}/{workload}/tao_trace", 8192, "data/eval_columnar_cache")
print(sum(1 for fp in files.values() if "aligned" in fp))
PY
)

echo "[cache] expected=$cache_path"
echo "[cache] aligned=$aligned_count/32"
if (( aligned_count < 32 )); then
  echo "[error] aligned parquet incomplete for $WORKLOAD" >&2
  echo "        run scripts/tmp/c32_eval_cache_and_throughput.sh or convert aligned parquet first" >&2
  exit 3
fi

if [[ ! -s "$cache_path" ]]; then
  if [[ "$CACHE_BUILD_IF_MISSING" != "1" ]]; then
    echo "[error] missing eval cache: $cache_path" >&2
    exit 4
  fi
  echo "[cache] missing; building only $WORKLOAD before throughput"
  CUDA_VISIBLE_DEVICES="$GPU" "$PY" eval/eval_quota_cycles.py \
    --raw-root "$RAW" \
    --workload "$WORKLOAD" \
    --ckpt "$CKPT" \
    --max-len "$MAX_LEN" \
    --max-windows 1 \
    --query-placement "$QUERY_PLACEMENT" \
    --planner-state-source "$PLANNER_STATE_SOURCE" \
    --device cuda \
    --infer-dtype "$INFER_DTYPE" \
    --sdpa-backend "$SDPA_BACKEND" \
    --eval-cache-mode auto \
    --eval-cache-dir "$EVAL_CACHE_DIR" \
    2>&1 | tee "$LOG_DIR/cache_build_${WORKLOAD}.log"
else
  echo "[cache] hit"
fi
echo

throughput_log="$LOG_DIR/throughput_${WORKLOAD}.log"
echo "[run] throughput -> $throughput_log"
/usr/bin/time -v env CUDA_VISIBLE_DEVICES="$GPU" \
  "$PY" eval/eval_quota_cycles.py \
    --raw-root "$RAW" \
    --workload "$WORKLOAD" \
    --ckpt "$CKPT" \
    --max-len "$MAX_LEN" \
    --max-windows "$MAX_WINDOWS" \
    --query-placement "$QUERY_PLACEMENT" \
    --planner-state-source "$PLANNER_STATE_SOURCE" \
    --device cuda \
    --infer-dtype "$INFER_DTYPE" \
    --sdpa-backend "$SDPA_BACKEND" \
    --eval-cache-mode auto \
    --eval-cache-dir "$EVAL_CACHE_DIR" \
  2>&1 | tee "$throughput_log"

echo
echo "[summary] log=$throughput_log"
if command -v rg >/dev/null 2>&1; then
  rg "eval-cache|\\[$WORKLOAD\\].*windows|timing\\(avg/window\\)|per-window cpi_uop MAPE" \
    "$throughput_log" | tail -n 80 || true
else
  grep -E "eval-cache|\\[$WORKLOAD\\].*windows|timing\\(avg/window\\)|per-window cpi_uop MAPE" \
    "$throughput_log" | tail -n 80 || true
fi

echo
echo "[done] logs=$LOG_DIR"
