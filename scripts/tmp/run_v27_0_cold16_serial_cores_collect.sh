#!/usr/bin/env bash
# TCSim entrypoint for v27.0-cold16 raw collection.
#
# The model/dataset project is TCSim.  gem5 workloads and the low-level
# collector live in the sibling TSim repository, which is invoked explicitly.
# Core-count groups are serial; workloads inside a core-count group are parallel.
# Each completed core group keeps aligned parquet and deletes only the much
# larger records/labels JSONL files after conversion has succeeded.
set -euo pipefail

TSIM_ROOT=${TSIM_ROOT:-/data00/yinhaolang/TSim}
CORES_LIST=${CORES_LIST:-"1 4 8 16 32"}
COLLECT_PARALLEL=${COLLECT_PARALLEL:-18}
CONVERT_PARALLEL=${CONVERT_PARALLEL:-18}
TARGET_PER_CORE=${TARGET_PER_CORE:-750000}
MIN_ACCEPT_PER_CORE=${MIN_ACCEPT_PER_CORE:-500000}
MAX_ACCEPT_PER_CORE=${MAX_ACCEPT_PER_CORE:-1000000}
TIMEOUT_SECS=${TIMEOUT_SECS:-21600}
MAX_FINAL_ATTEMPTS=${MAX_FINAL_ATTEMPTS:-3}

run_seed() {
  local seed=$1
  local mode=$2
  local run_tag="v27_0_cold16_seed${seed}"
  local stamp
  stamp=$(date +%Y%m%d_%H%M%S)

  echo "[launcher] seed=${seed} mode=${mode} cores=${CORES_LIST}"
  echo "[launcher] core groups are serial; workload parallelism=${COLLECT_PARALLEL}"

  SEED="$seed" \
  MODE="$mode" \
  RUN_TAG="$run_tag" \
  DATA_PREFIX="data/raw_${run_tag}" \
  LOG_ROOT="logs/tmp/${run_tag}_serial_cores_${stamp}" \
  CORES_LIST="$CORES_LIST" \
  CORE_PARALLEL=1 \
  COLLECT_PARALLEL="$COLLECT_PARALLEL" \
  CONVERT_PARALLEL="$CONVERT_PARALLEL" \
  DROP_RAW_JSONL_AFTER_ALIGN=1 \
  TARGET_PER_CORE="$TARGET_PER_CORE" \
  MIN_ACCEPT_PER_CORE="$MIN_ACCEPT_PER_CORE" \
  MAX_ACCEPT_PER_CORE="$MAX_ACCEPT_PER_CORE" \
  STRICT_NATURAL_ROI=1 \
  PROBE_SCALE=1 \
  PROBE_STOP_REC=0 \
  REUSE_PROBE_IF_SUFFICIENT=1 \
  RUN_TO_COMPLETION=1 \
  TIMEOUT_SECS="$TIMEOUT_SECS" \
  MAX_FINAL_ATTEMPTS="$MAX_FINAL_ATTEMPTS" \
  bash "$TSIM_ROOT/scripts/tmp/tmp_v27_ffatomic_allcores_collect_align.sh"
}

run_seed 0 all
run_seed 1 train

echo "[launcher] complete $(date '+%F %T')"
