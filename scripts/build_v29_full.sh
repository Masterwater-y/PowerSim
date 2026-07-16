#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
cd "$ROOT"
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
OUT=${OUT:-data/v29_global_time_dataset}
RAW_ROOT_GLOB=${RAW_ROOT_GLOB:-/data00/yinhaolang/TSim/data/raw_v28_1_business_a2_sharedzipf_seed*_c*}
WORKERS=${WORKERS:-64}

mkdir -p "$OUT" logs/v29
exec "$PY" scripts/build_v29_dataset.py \
  --raw-root-glob "$RAW_ROOT_GLOB" \
  --out "$OUT" \
  --contract-file "${CONTRACT_FILE:-configs/v28_business_workloads.json}" \
  --horizons "${HORIZONS:-16,32,64,128,256,512,1024}" \
  --sample-period-cycles "${SAMPLE_PERIOD_CYCLES:-64}" \
  --block-cycles "${BLOCK_CYCLES:-65536}" \
  --min-uops-per-core "${MIN_UOPS_PER_CORE:-500000}" \
  --max-uops-per-core "${MAX_UOPS_PER_CORE:-1000000}" \
  --max-full-uop-cpi "${MAX_FULL_UOP_CPI:-10}" \
  --workers "$WORKERS" \
  --seeds "${SEEDS:-0,1}" \
  --core-counts "${CORE_COUNTS:-1,4,8,16,32}" \
  "$@"
