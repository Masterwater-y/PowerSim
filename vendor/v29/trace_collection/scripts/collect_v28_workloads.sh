#!/usr/bin/env bash
# Collect one core-count slice of the v28 business-oriented workload set using
# the existing strict natural-ROI collector.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COLLECT=${COLLECT:-"$ROOT/scripts/collect_parallel_500k.sh"}
BIN_DIR=${BIN_DIR:-"$ROOT/workloads/bin"}
NUM_CORES=${NUM_CORES:-8}
SEED=${SEED:-0}
VALIDATE_WINDOWS=${VALIDATE_WINDOWS:-0}

TRAIN_WORKLOADS=(
  v28_int_alu_dense
  v28_int_div_serial
  v28_fp_alu_dense
  v28_simd_sse_dense
  v28_cache_L1_mixed
  v28_cache_L2_mixed
  v28_memory_seq_moderate
  v28_memory_random_mlp
  v28_coh_readmostly_sparse
  v28_marine_base
  v28_gofeed_base
  v28_flink_base
  v28_mysql_base
  v28_redis_base
  v28_pytorch_base
  v28_bvc_encoder_base
)

HELDOUT_WORKLOADS=(
  v28_marine_heldout
  v28_gofeed_heldout
  v28_flink_heldout
  v28_mysql_heldout
  v28_redis_heldout
  v28_pytorch_heldout
  v28_bvc_encoder_heldout
)

mode=${1:-train}
if [[ $# -gt 0 ]]; then shift; fi
case "$mode" in
  train) WORKLOADS=("${TRAIN_WORKLOADS[@]}") ;;
  heldout) WORKLOADS=("${HELDOUT_WORKLOADS[@]}") ;;
  all) WORKLOADS=("${TRAIN_WORKLOADS[@]}" "${HELDOUT_WORKLOADS[@]}") ;;
  smoke) WORKLOADS=(v28_cache_L2_mixed v28_marine_base v28_mysql_base v28_pytorch_base v28_bvc_encoder_base) ;;
  *)
    echo "usage: $0 [train|heldout|all|smoke] [extra workload names...]" >&2
    exit 2
    ;;
esac
if [[ $# -gt 0 ]]; then WORKLOADS+=("$@"); fi

OUT_BASE=${OUT_BASE:-"$ROOT/data/raw_v28_${mode}_c${NUM_CORES}"}
echo "[v28] mode=$mode cores=$NUM_CORES seed=$SEED out=$OUT_BASE"
echo "[v28] workloads=${WORKLOADS[*]}"

BIN_DIR="$BIN_DIR" \
OUT_BASE="$OUT_BASE" \
NUM_CORES="$NUM_CORES" \
SEED="$SEED" \
VALIDATE_WINDOWS="$VALIDATE_WINDOWS" \
bash "$COLLECT" "${WORKLOADS[@]}"
