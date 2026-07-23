#!/usr/bin/env bash
# Wrapper for collecting the isolated v27 workload set with the existing
# collect_parallel_500k.sh framework.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COLLECT=${COLLECT:-"$ROOT/scripts/collect_parallel_500k.sh"}
BIN_DIR=${BIN_DIR:-"$ROOT/workloads/bin"}
NUM_CORES=${NUM_CORES:-8}
SEED=${SEED:-0}
VALIDATE_WINDOWS=${VALIDATE_WINDOWS:-0}

TRAIN_WORKLOADS=(
  int_alu_dense
  int_div_serial
  fp_alu_dense
  simd_sse_dense
  stream_seq_L2
  stream_seq_DRAM
  random_DRAM
  chase_DRAM
  coh_read_share
  coh_write_share
  coh_false_share
  coh_asym_rw
  phase_coh_onset
  skew_hot_cold
  ranking_mix_private
)

HELDOUT_WORKLOADS=(
  phase_coh_decay
)

mode=${1:-train}
if [[ $# -gt 0 ]]; then
  shift
fi

case "$mode" in
  train)
    WORKLOADS=("${TRAIN_WORKLOADS[@]}")
    ;;
  heldout)
    WORKLOADS=("${HELDOUT_WORKLOADS[@]}")
    ;;
  all)
    WORKLOADS=("${TRAIN_WORKLOADS[@]}" "${HELDOUT_WORKLOADS[@]}")
    ;;
  smoke)
    WORKLOADS=(int_alu_dense stream_seq_L2 coh_write_share phase_coh_onset)
    ;;
  *)
    echo "usage: $0 [train|heldout|all|smoke] [extra workload names...]" >&2
    exit 2
    ;;
esac

if [[ $# -gt 0 ]]; then
  WORKLOADS+=("$@")
fi

OUT_BASE=${OUT_BASE:-"$ROOT/data/raw_v27_${mode}_c${NUM_CORES}"}

if [[ ! -d "$BIN_DIR" ]]; then
  echo "[v27] missing BIN_DIR=$BIN_DIR; run: make -C $ROOT/workloads" >&2
  exit 1
fi

echo "[v27] mode=$mode"
echo "[v27] BIN_DIR=$BIN_DIR"
echo "[v27] OUT_BASE=$OUT_BASE"
echo "[v27] NUM_CORES=$NUM_CORES SEED=$SEED VALIDATE_WINDOWS=$VALIDATE_WINDOWS"
echo "[v27] workloads=${WORKLOADS[*]}"

BIN_DIR="$BIN_DIR" \
OUT_BASE="$OUT_BASE" \
NUM_CORES="$NUM_CORES" \
SEED="$SEED" \
VALIDATE_WINDOWS="$VALIDATE_WINDOWS" \
bash "$COLLECT" "${WORKLOADS[@]}"
