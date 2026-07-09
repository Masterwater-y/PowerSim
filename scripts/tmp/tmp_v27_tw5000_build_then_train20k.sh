#!/usr/bin/env bash
set -euo pipefail

TSIM_ROOT=${TSIM_ROOT:-/data00/yinhaolang/TSim}
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
cd "$TSIM_ROOT"

MAX_LEN=${MAX_LEN:-32768}
TARGET_WINDOWS=${TARGET_WINDOWS:-5000}
BUILD_JOBS=${BUILD_JOBS:-19}
CACHE_JOBS=${CACHE_JOBS:-96}
OUT_COMB=${OUT_COMB:-data/windows_v27_ss_tail_local_c01_c04_c08_c16_c32}

RUN_NAME=${RUN_NAME:-v27_ss_tw5000_8l_t32768_bs1_20k_$(date +%Y%m%d_%H%M%S)}
OUT=${OUT:-ckpt/$RUN_NAME}

mkdir -p logs ckpt tmp

echo "[all] preflight $(date '+%F %T')"
"$PY" -m py_compile \
  data/build_windows.py \
  scripts/merge_v26_tensor_cache_parts.py \
  model/v26_kvqr.py \
  train/dataset.py \
  train/train_v26_kvqr.py

echo "[all] build TARGET_WINDOWS=$TARGET_WINDOWS BUILD_JOBS=$BUILD_JOBS CACHE_JOBS=$CACHE_JOBS"
TARGET_WINDOWS="$TARGET_WINDOWS" \
BUILD_JOBS="$BUILD_JOBS" \
CACHE_JOBS="$CACHE_JOBS" \
MAX_LEN="$MAX_LEN" \
OUT_COMB="$OUT_COMB" \
bash scripts/tmp_v27_c32_seedA_ffatomic_nodtlb.sh build

DATA="$OUT_COMB/windows.jsonl"
CACHE_PATH="$OUT_COMB/windows.maxlen${MAX_LEN}.tensor_cache"

if [[ ! -s "$DATA" ]]; then
  echo "[all][error] missing data jsonl: $DATA" >&2
  exit 2
fi
if [[ ! -s "$CACHE_PATH/manifest.pt" ]]; then
  echo "[all][error] missing tensor cache manifest: $CACHE_PATH/manifest.pt" >&2
  exit 3
fi

echo "[all] train RUN_NAME=$RUN_NAME OUT=$OUT"
echo "[all] DATA=$DATA"
echo "[all] CACHE_PATH=$CACHE_PATH"

RUN_NAME="$RUN_NAME" \
OUT="$OUT" \
DATA="$DATA" \
CACHE_PATH="$CACHE_PATH" \
TARGET_STEPS=${TARGET_STEPS:-20000} \
GPUS=${GPUS:-0,1,2,3,4,5,6,7} \
NPROC=${NPROC:-8} \
BS=${BS:-1} \
D_MODEL=${D_MODEL:-320} \
N_HEADS=${N_HEADS:-8} \
N_LAYERS=${N_LAYERS:-8} \
FFN_DIM=${FFN_DIM:-1280} \
FIELD_DIM=${FIELD_DIM:-96} \
HEAD_HIDDEN=${HEAD_HIDDEN:-256} \
AMP_DTYPE=${AMP_DTYPE:-bf16} \
REQUIRE_FLASH_ATTN=${REQUIRE_FLASH_ATTN:-0} \
SDPA_BACKEND=${SDPA_BACKEND:-no_flash} \
MAX_UOPS_PER_CORE="$MAX_LEN" \
TRAIN_MAX_UOPS_PER_CORE=${TRAIN_MAX_UOPS_PER_CORE:-0} \
TRAIN_MAX_TOTAL_UOPS=${TRAIN_MAX_TOTAL_UOPS:-32768} \
LENGTH_BUCKET_SIZE=${LENGTH_BUCKET_SIZE:-2048} \
EVAL_EVERY=${EVAL_EVERY:-1000} \
EVAL_BATCHES=${EVAL_BATCHES:-0} \
SAVE_EVERY=${SAVE_EVERY:-500} \
NUM_WORKERS=${NUM_WORKERS:-2} \
PREFETCH_FACTOR=${PREFETCH_FACTOR:-2} \
MONITOR_INTERVAL=${MONITOR_INTERVAL:-30} \
bash scripts/launch_v26_kvqr_20k_watchdog.sh

echo "[all] watchdog started"
echo "[all] train log: logs/${RUN_NAME}.current.log"
echo "[all] watchdog log: logs/watchdog/${RUN_NAME}.nohup.log"
