#!/usr/bin/env bash
set -euo pipefail

THIS_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
source "${THIS_DIR}/env.sh"

PYTHON_BIN="${PYTHON_BIN:-/root/miniconda3/envs/yinhaolang/bin/python}"
TAOGEN_ROOT="${TAOGEN_ROOT:-${TAO_ROOT}/datagen}"
RAW_ROOT="${RAW_ROOT:-${TAO_ROOT}/tmp/holdout_mixed_raw_$(date +%Y%m%d_%H%M%S)}"
RUN_ROOT="${RUN_ROOT:-${TAO_ROOT}/runs/holdout_mixed_100k_$(date +%Y%m%d_%H%M%S)}"
NUM_CORES="${NUM_CORES:-4}"
FORCE=0
RUN_GEM5=1
DRY_RUN=0

WORKLOADS="${WORKLOADS:-H01_mixed_service H02_sharded_kv H03_analytics_scan}"

usage() {
  cat <<EOF
usage: $0 [common validation options] [--skip-gem5] [--force] [--dry-run]

Holdout wrapper:
  optionally build/run H01-H03 gem5 raw traces, then delegate validation to
  scripts/_validate_100k_common.sh. If datasets already exist under DATA_ROOT,
  use --skip-gem5 to validate them directly.
EOF
}

COMMON_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --raw-root) RAW_ROOT="$2"; COMMON_ARGS+=("$1" "$2"); shift 2 ;;
    --run-root) RUN_ROOT="$2"; COMMON_ARGS+=("$1" "$2"); shift 2 ;;
    --num-cores) NUM_CORES="$2"; COMMON_ARGS+=("$1" "$2"); shift 2 ;;
    --workloads) WORKLOADS="$2"; COMMON_ARGS+=("$1" "$2"); shift 2 ;;
    --skip-gem5) RUN_GEM5=0; shift ;;
    --force) FORCE=1; COMMON_ARGS+=("$1"); shift ;;
    --dry-run) DRY_RUN=1; COMMON_ARGS+=("$1"); shift ;;
    -h|--help) usage; "${THIS_DIR}/_validate_100k_common.sh" --help; exit 0 ;;
    *)
      COMMON_ARGS+=("$1")
      if [[ $# -ge 2 && "$2" != --* ]]; then
        COMMON_ARGS+=("$2")
        shift 2
      else
        shift
      fi
      ;;
  esac
done

PY_BINDIR="$(cd "$(dirname "${PYTHON_BIN}")" && pwd)"
export PATH="${PY_BINDIR}:${PATH}"

mkdir -p "${RAW_ROOT}"

if [[ "${RUN_GEM5}" -eq 1 && "${DRY_RUN}" -eq 0 ]]; then
  collect_args=(
    --group h
    --workloads "${WORKLOADS}"
    --num-cores "${NUM_CORES}"
    --raw-root "${RAW_ROOT}"
  )
  if [[ "${FORCE}" -eq 1 ]]; then
    collect_args+=(--force)
  fi
  "${THIS_DIR}/14_collect_multicore_raw.sh" "${collect_args[@]}"
else
  echo "[holdout] skip gem5/raw generation"
fi

export RAW_ROOT RUN_ROOT NUM_CORES WORKLOADS LOG_PREFIX="${LOG_PREFIX:-holdout}"
exec "${THIS_DIR}/_validate_100k_common.sh" "${COMMON_ARGS[@]}"
