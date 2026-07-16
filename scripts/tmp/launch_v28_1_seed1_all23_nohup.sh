#!/usr/bin/env bash
# Collect the frozen seed1 deployment set into non-overlapping raw roots.
# Core-count groups are serial; all 23 workloads in one group run in parallel.
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
TSIM_ROOT=${TSIM_ROOT:-/data00/yinhaolang/TSim}
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}

cd "$ROOT"

# Make sure collection uses the current workload binaries and source contract.
make -C "$TSIM_ROOT/workloads/v28" all >/dev/null
"$PY" scripts/audit_v28_workload_source.py >/dev/null

mkdir -p logs/tmp
stamp=$(date +%Y%m%d_%H%M%S)
log="$ROOT/logs/tmp/v28_1_seed1_all23_${stamp}.log"

nohup env \
  TSIM_ROOT="$TSIM_ROOT" \
  PY="$PY" \
  DATASET_TAG=v28_1_business_a2_sharedzipf \
  CORES_LIST="4 8 16 32" \
  SEEDS=1 \
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
  REUSE_PROBE_IF_SUFFICIENT=0 \
  RUN_AUDIT=1 \
  AUDIT_OUT="$ROOT/data/v28_1_business_a2_sharedzipf_seed01_raw_audit.json" \
  bash scripts/tmp/run_v28_business_serial_cores_collect.sh \
  >"$log" 2>&1 &

pid=$!
echo "pid=$pid"
echo "log=$log"
echo "raw=$TSIM_ROOT/data/raw_v28_1_business_a2_sharedzipf_seed1_c{04,08,16,32}"
echo "watch: tail -f $log"
