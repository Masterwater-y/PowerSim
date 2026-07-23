#!/usr/bin/env bash
# v27.0-cold16 raw collection launcher.
#
# Policy:
#   - c01 -> c04 -> c08 -> c16 -> c32 are strictly serial;
#   - workloads within the current core-count run concurrently;
#   - seed0 (16 train + 2 heldout) completes before seed1 (16 train) starts.
#
# This is intentionally a launcher only.  The underlying collector preserves
# the cold-start FF-Atomic -> first WORKBEGIN O3+Ruby -> full ROI contract.
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TSim}
CORES_LIST=${CORES_LIST:-"1 4 8 16 32"}
COLLECT_PARALLEL=${COLLECT_PARALLEL:-18}
CONVERT_PARALLEL=${CONVERT_PARALLEL:-18}
TARGET_PER_CORE=${TARGET_PER_CORE:-500000}
MIN_ACCEPT_PER_CORE=${MIN_ACCEPT_PER_CORE:-450000}
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
  TARGET_PER_CORE="$TARGET_PER_CORE" \
  MIN_ACCEPT_PER_CORE="$MIN_ACCEPT_PER_CORE" \
  MAX_ACCEPT_PER_CORE=0 \
  PROBE_SCALE=1 \
  PROBE_STOP_REC=0 \
  REUSE_PROBE_IF_SUFFICIENT=1 \
  RUN_TO_COMPLETION=1 \
  TIMEOUT_SECS="$TIMEOUT_SECS" \
  MAX_FINAL_ATTEMPTS="$MAX_FINAL_ATTEMPTS" \
  bash "$ROOT/scripts/tmp/tmp_v27_ffatomic_allcores_collect_align.sh"
}

run_seed 0 all
run_seed 1 train

echo "[launcher] complete $(date '+%F %T')"
