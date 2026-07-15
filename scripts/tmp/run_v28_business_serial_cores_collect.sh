#!/usr/bin/env bash
# v28 raw collection: core-count groups serial, workloads within a group fully
# parallel.  Existing output is never deleted by this script.
set -euo pipefail

TSIM_ROOT=${TSIM_ROOT:-/data00/yinhaolang/TSim}
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
CORES_LIST=${CORES_LIST:-"1 4 8 16 32"}
SEEDS=${SEEDS:-"0"}
MODE=${MODE:-all}
COLLECT_PARALLEL=${COLLECT_PARALLEL:-23}
CONVERT_PARALLEL=${CONVERT_PARALLEL:-23}
TARGET_PER_CORE=${TARGET_PER_CORE:-750000}
MIN_ACCEPT_PER_CORE=${MIN_ACCEPT_PER_CORE:-500000}
MAX_ACCEPT_PER_CORE=${MAX_ACCEPT_PER_CORE:-1000000}
TIMEOUT_SECS=${TIMEOUT_SECS:-21600}
DROP_RAW_JSONL_AFTER_ALIGN=${DROP_RAW_JSONL_AFTER_ALIGN:-1}
ROW_GROUP_SIZE=${ROW_GROUP_SIZE:-65536}
RUN_AUDIT=${RUN_AUDIT:-1}
DATASET_TAG=${DATASET_TAG:-v28_business_a1_sharedzipf}
AUDIT_OUT=${AUDIT_OUT:-data/${DATASET_TAG}_seed0_raw_audit.json}
L2_SIZE=${L2_SIZE:-1MiB}
L3_SIZE=${L3_SIZE:-8MiB}
NUM_L3_BANKS=${NUM_L3_BANKS:-8}
MEM_CHANNELS=${MEM_CHANNELS:-8}

TRAIN=(
  v28_int_alu_dense v28_int_div_serial v28_fp_alu_dense v28_simd_sse_dense
  v28_cache_L1_mixed v28_cache_L2_mixed
  v28_memory_seq_moderate v28_memory_random_mlp v28_coh_readmostly_sparse
  v28_marine_base v28_gofeed_base v28_flink_base v28_mysql_base
  v28_redis_base v28_pytorch_base v28_bvc_encoder_base
)
HELDOUT=(
  v28_marine_heldout v28_gofeed_heldout v28_flink_heldout
  v28_mysql_heldout v28_redis_heldout v28_pytorch_heldout
  v28_bvc_encoder_heldout
)
case "$MODE" in
  train) WORKLOADS=("${TRAIN[@]}") ;;
  heldout) WORKLOADS=("${HELDOUT[@]}") ;;
  all) WORKLOADS=("${TRAIN[@]}" "${HELDOUT[@]}") ;;
  *) echo "MODE must be train, heldout, or all" >&2; exit 2 ;;
esac

mkdir -p "$PWD/logs/tmp"
stamp=$(date +%Y%m%d_%H%M%S)
log_root="$PWD/logs/tmp/v28_business_collect_${stamp}"
mkdir -p "$log_root"
echo "[meta] detail_logs=$log_root"
echo "[meta] server_profile=l2:${L2_SIZE} l3:${NUM_L3_BANKS}x${L3_SIZE} dram:${MEM_CHANNELS}ch-DDR4-2400"

for seed in $SEEDS; do
  for ncore in $CORES_LIST; do
    printf -v ctag "c%02d" "$ncore"
    raw="$TSIM_ROOT/data/raw_${DATASET_TAG}_seed${seed}_${ctag}"
    core_log="$log_root/seed${seed}_${ctag}"
    mkdir -p "$core_log/align"
    echo "[collect] seed=$seed core=$ncore mode=$MODE raw=$raw detail=$core_log/collect.log"
    FF_ATOMIC=1 \
    PARALLEL="$COLLECT_PARALLEL" \
    NUM_CORES="$ncore" \
    OUT_BASE="$raw" \
    SEED="$seed" \
    TARGET_PER_CORE="$TARGET_PER_CORE" \
    MIN_ACCEPT_PER_CORE="$MIN_ACCEPT_PER_CORE" \
    MAX_ACCEPT_PER_CORE="$MAX_ACCEPT_PER_CORE" \
    L2_SIZE="$L2_SIZE" \
    L3_SIZE="$L3_SIZE" \
    NUM_L3_BANKS="$NUM_L3_BANKS" \
    MEM_CHANNELS="$MEM_CHANNELS" \
    STRICT_NATURAL_ROI=1 \
    PROBE_SCALE=1 \
    PROBE_STOP_REC=0 \
    REUSE_PROBE_IF_SUFFICIENT=1 \
    RUN_TO_COMPLETION=1 \
    TIMEOUT_SECS="$TIMEOUT_SECS" \
    bash "$TSIM_ROOT/scripts/collect_v28_workloads.sh" "$MODE" \
      >"$core_log/collect.log" 2>&1

    fails=0
    for wl in "${WORKLOADS[@]}"; do
      while (( $(jobs -pr | wc -l) >= CONVERT_PARALLEL )); do
        wait -n || fails=$((fails + 1))
      done
      "$PY" "$TSIM_ROOT/scripts/convert_trace_to_aligned_parquet.py" \
        --raw-root "$raw" --workloads "W_${wl}" --row-group-size "$ROW_GROUP_SIZE" \
        >"$core_log/align/W_${wl}.log" 2>&1 &
    done
    while (( $(jobs -pr | wc -l) > 0 )); do
      wait -n || fails=$((fails + 1))
    done
    expected=$((ncore * ${#WORKLOADS[@]}))
    actual=$(find -L "$raw" -maxdepth 3 -type f -name '*.aligned.parquet' | wc -l)
    if (( fails != 0 || actual < expected )); then
      echo "[align][FAIL] seed=$seed core=$ncore files=$actual/$expected fails=$fails" >&2
      exit 1
    fi
    if [[ "$DROP_RAW_JSONL_AFTER_ALIGN" == "1" ]]; then
      find "$raw" -type f \( -name '*.records.micro.jsonl' -o -name '*.labels.micro.jsonl' \) -delete
    fi
    echo "[done] seed=$seed core=$ncore aligned=$actual/$expected"
  done
done

if [[ "$RUN_AUDIT" == "1" ]]; then
  echo "[audit] out=$AUDIT_OUT"
  "$PY" scripts/audit_v28_raw_dataset.py \
    --root-glob "$TSIM_ROOT/data/raw_${DATASET_TAG}_seed*_c*" \
    --sample-regions 9 \
    --sample-uops-per-core 8192 \
    --out "$AUDIT_OUT"
fi

echo "[complete] logs=$log_root"
