#!/usr/bin/env bash
# Launch the complete A1 server32 + shared-Zipf v28 collection in background.
# Core-count slices are serial; all 23 workloads inside one slice run in parallel.
set -euo pipefail

ROOT=/data00/yinhaolang/TCSim
TSIM_ROOT=${TSIM_ROOT:-/data00/yinhaolang/TSim}
cd "$ROOT"

make -C "$TSIM_ROOT/workloads/v28" all >/dev/null
python3 scripts/audit_v28_workload_source.py >/dev/null

mkdir -p logs/tmp
stamp=$(date +%Y%m%d_%H%M%S)
log="$ROOT/logs/tmp/v28_business_a1_sharedzipf_full_${stamp}.log"

nohup env \
  TSIM_ROOT="$TSIM_ROOT" \
  DATASET_TAG=v28_business_a1_sharedzipf \
  CORES_LIST="1 4 8 16 32" \
  SEEDS=0 \
  MODE=all \
  COLLECT_PARALLEL=23 \
  CONVERT_PARALLEL=23 \
  TARGET_PER_CORE=750000 \
  MIN_ACCEPT_PER_CORE=500000 \
  MAX_ACCEPT_PER_CORE=1000000 \
  L2_SIZE=1MiB \
  L3_SIZE=8MiB \
  NUM_L3_BANKS=8 \
  MEM_CHANNELS=8 \
  DROP_RAW_JSONL_AFTER_ALIGN=1 \
  RUN_AUDIT=1 \
  bash scripts/tmp/run_v28_business_serial_cores_collect.sh \
  >"$log" 2>&1 &

pid=$!
echo "pid=$pid"
echo "log=$log"
echo "watch: tail -f $log"
