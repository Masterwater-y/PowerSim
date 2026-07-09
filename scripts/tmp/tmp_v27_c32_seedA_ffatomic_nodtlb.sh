#!/usr/bin/env bash
set -euo pipefail

# Temporary v27 data pipeline:
#   1. collect C32 seedA raw traces with gem5 FF-ATOMIC enabled
#   2. rebuild v27_ss training windows for c01/c04/c08/c16/c32
#   3. build tensor cache without dtlb_miss labels
#
# Usage:
#   bash scripts/tmp_v27_c32_seedA_ffatomic_nodtlb.sh          # collect + build
#   bash scripts/tmp_v27_c32_seedA_ffatomic_nodtlb.sh collect  # collect only
#   bash scripts/tmp_v27_c32_seedA_ffatomic_nodtlb.sh build    # build only
#   bash scripts/tmp_v27_c32_seedA_ffatomic_nodtlb.sh status   # show C32 progress

ACTION=${1:-all}

TSIM_ROOT=${TSIM_ROOT:-/data00/yinhaolang/TSim}
LLMSIM_ROOT=${LLMSIM_ROOT:-/data00/yinhaolang/LLMSim}
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}

cd "$TSIM_ROOT"

export TMPDIR="${TMPDIR:-$TSIM_ROOT/tmp}"
mkdir -p "$TMPDIR" "$TSIM_ROOT/logs" "$LLMSIM_ROOT/logs"

NUM_CORES=${NUM_CORES:-32}
SEED=${SEED:-0}
FF_ATOMIC=${FF_ATOMIC:-1}
PARALLEL=${PARALLEL:-17}

TARGET_PER_CORE=${TARGET_PER_CORE:-500000}
MIN_ACCEPT_PER_CORE=${MIN_ACCEPT_PER_CORE:-450000}
MAX_ACCEPT_PER_CORE=${MAX_ACCEPT_PER_CORE:-0}
PROBE_SCALE=${PROBE_SCALE:-1}
PROBE_STOP_REC=${PROBE_STOP_REC:-700000}
REUSE_PROBE_IF_SUFFICIENT=${REUSE_PROBE_IF_SUFFICIENT:-1}
MAX_FINAL_ATTEMPTS=${MAX_FINAL_ATTEMPTS:-4}
TIMEOUT_SECS=${TIMEOUT_SECS:-43200}
PROGRESS_INTERVAL=${PROGRESS_INTERVAL:-120}
VALIDATE_WINDOWS=${VALIDATE_WINDOWS:-0}

MAX_LEN=${MAX_LEN:-32768}
TARGET_WINDOWS=${TARGET_WINDOWS:-3000}
MIN_UOPS=${MIN_UOPS:-256}
BUILD_JOBS=${BUILD_JOBS:-32}
CACHE_JOBS=${CACHE_JOBS:-96}
DEDUP_THRESHOLD=${DEDUP_THRESHOLD:-0.05}
DIRECT_CACHE_SHARD_SIZE=${DIRECT_CACHE_SHARD_SIZE:-512}
CAP_COMMON=${CAP_COMMON:-default=1500,W_chase_low=2000,W_chase_mid=2500,W_chase_high=2000,W_chase_extreme=2500,W_fs_low=2000,W_fs_mid=2500,W_fs_high=2000,W_fs_extreme=2500,W_stream_low=2000,W_stream_high=2000,W_stream_extreme=2500,W_feed_low=2000,W_feed_high=2000,W_fpcd_low=2000,W_fpcd_high=2000,W_adsctr_low=2000,W_adsctr_high=2000}
CAP_C01=${CAP_C01:-default=1800,W_chase_low=2400,W_chase_mid=3000,W_chase_high=2400,W_chase_extreme=3000,W_fs_low=2400,W_fs_mid=3000,W_fs_high=2400,W_fs_extreme=3000,W_stream_low=2400,W_stream_high=2400,W_stream_extreme=3000,W_feed_low=2400,W_feed_high=2400,W_fpcd_low=2400,W_fpcd_high=2400,W_adsctr_low=2400,W_adsctr_high=2400}

RAW_C32=${RAW_C32:-$LLMSIM_ROOT/data/raw_trace_pool/activecore_train/c32_seedA}
OUT_COMB=${OUT_COMB:-data/windows_v27_ss_tail_local_c01_c04_c08_c16_c32}

# Collect all 17 current workload binaries, including phased_mix, so the C32
# raw pool is complete. Training windows below intentionally exclude phased_mix.
COLLECT_WORKLOADS=(
  ads_ctr
  ads_ranking_proxy
  branch_storm
  chase_dram
  compute_int
  false_sharing
  feed_ranking
  fp_compute_dense
  fp_lite
  graph_recall_proxy
  indirect
  int_div
  interest_graph_recall
  mlp_light
  phased_mix
  search_index_proxy
  stream
)

TRAIN_WORKLOADS=(
  W_ads_ctr
  W_ads_ranking_proxy
  W_branch_storm
  W_chase_dram
  W_compute_int
  W_false_sharing
  W_feed_ranking
  W_fp_compute_dense
  W_fp_lite
  W_graph_recall_proxy
  W_indirect
  W_int_div
  W_interest_graph_recall
  W_mlp_light
  W_search_index_proxy
  W_stream
)

LABEL_KEYS_NO_DTLB="cpi_uop,branch_miss,l1d_ld_miss,l1d_st_miss,l2_ld_miss,l2_st_miss,llc_miss"

print_config() {
  echo "[config] ACTION=$ACTION"
  echo "[config] TSIM_ROOT=$TSIM_ROOT"
  echo "[config] LLMSIM_ROOT=$LLMSIM_ROOT"
  echo "[config] RAW_C32=$RAW_C32"
  echo "[config] NUM_CORES=$NUM_CORES SEED=$SEED FF_ATOMIC=$FF_ATOMIC PARALLEL=$PARALLEL"
  echo "[config] TARGET_PER_CORE=$TARGET_PER_CORE MIN_ACCEPT_PER_CORE=$MIN_ACCEPT_PER_CORE MAX_ACCEPT_PER_CORE=$MAX_ACCEPT_PER_CORE"
  echo "[config] TIMEOUT_SECS=$TIMEOUT_SECS MAX_FINAL_ATTEMPTS=$MAX_FINAL_ATTEMPTS"
  echo "[config] MAX_LEN=$MAX_LEN TARGET_WINDOWS=$TARGET_WINDOWS MIN_UOPS=$MIN_UOPS"
  echo "[config] BUILD_JOBS=$BUILD_JOBS CACHE_JOBS=$CACHE_JOBS DEDUP_THRESHOLD=$DEDUP_THRESHOLD DIRECT_CACHE_SHARD_SIZE=$DIRECT_CACHE_SHARD_SIZE"
  echo "[config] CAP_COMMON=$CAP_COMMON"
  echo "[config] CAP_C01=$CAP_C01"
  echo "[config] OUT_COMB=$OUT_COMB"
  echo "[config] collect_workloads=${#COLLECT_WORKLOADS[@]} train_workloads=${#TRAIN_WORKLOADS[@]}"
}

run_collect() {
  print_config
  echo "[collect] start $(date '+%F %T')"
  mkdir -p "$RAW_C32"
  (
    cd "$LLMSIM_ROOT"
    OUT_BASE="$RAW_C32" \
    NUM_CORES="$NUM_CORES" \
    SEED="$SEED" \
    FF_ATOMIC="$FF_ATOMIC" \
    PARALLEL="$PARALLEL" \
    TARGET_PER_CORE="$TARGET_PER_CORE" \
    MIN_ACCEPT_PER_CORE="$MIN_ACCEPT_PER_CORE" \
    MAX_ACCEPT_PER_CORE="$MAX_ACCEPT_PER_CORE" \
    PROBE_SCALE="$PROBE_SCALE" \
    PROBE_STOP_REC="$PROBE_STOP_REC" \
    REUSE_PROBE_IF_SUFFICIENT="$REUSE_PROBE_IF_SUFFICIENT" \
    MAX_FINAL_ATTEMPTS="$MAX_FINAL_ATTEMPTS" \
    TIMEOUT_SECS="$TIMEOUT_SECS" \
    PROGRESS_INTERVAL="$PROGRESS_INTERVAL" \
    VALIDATE_WINDOWS="$VALIDATE_WINDOWS" \
    bash "$LLMSIM_ROOT/scripts/collect_parallel_500k.sh" "${COLLECT_WORKLOADS[@]}"
  )
  echo "[collect] done $(date '+%F %T')"
}

run_status() {
  echo "[status] RAW_C32=$RAW_C32"
  find "$RAW_C32" -maxdepth 1 -type d -name 'W_*' 2>/dev/null | sort | sed 's#^.*/#  #'
  echo
  echo "[status] completed=$(find "$RAW_C32" -maxdepth 1 -type d -name 'W_*' 2>/dev/null | wc -l | tr -d ' ')/${#COLLECT_WORKLOADS[@]}"
  echo
  echo "[status] running gem5/collect processes:"
  ps -eo pid,etime,pcpu,pmem,cmd | grep -E '[g]em5\.opt|[c]ollect_parallel_500k' || true
}

build_one() {
  local tag=$1
  local raw="data/raw_trace_pool/activecore_train/${tag}_seedA"
  if [[ "$tag" == "c32" ]]; then
    raw="$RAW_C32"
  fi
  local cap_spec="$CAP_COMMON"
  if [[ "$tag" == "c01" ]]; then
    cap_spec="$CAP_C01"
  fi
  local out="data/windows_v27_ss_tail_local_${tag}"
  local log="logs/build_v27_ss_tail_local_${tag}.log"

  if [[ ! -d "$raw" ]]; then
    echo "[build][error] missing raw dir for $tag: $raw" >&2
    exit 2
  fi

  echo "[build][$tag] raw=$raw out=$out log=$log"
  rm -rf "$out"
  "$PY" data/build_windows.py \
    --raw "$raw" \
    --out "$out" \
    --tq-max-len "$MAX_LEN" \
    --tq-target-windows "$TARGET_WINDOWS" \
    --tq-min-uops-per-core "$MIN_UOPS" \
    --per-workload-cap "$cap_spec" \
    --per-workload-cap-seed 0 \
    --dedup-threshold "$DEDUP_THRESHOLD" \
    --dedup-jobs "$BUILD_JOBS" \
    --jobs "$BUILD_JOBS" \
    --workloads "${TRAIN_WORKLOADS[@]}" \
    --query-placement tail_local \
    --uop-field-schema v27_ss \
    --shared-state-features \
    --direct-tensor-cache \
    --direct-cache-shard-size "$DIRECT_CACHE_SHARD_SIZE" \
    --cache-max-len "$MAX_LEN" \
    --cache-label-keys "$LABEL_KEYS_NO_DTLB" \
    --no-cache \
    > "$log" 2>&1

  if [[ ! -s "$out/windows.maxlen${MAX_LEN}.tensor_cache/manifest.pt" ]]; then
    echo "[build][error] missing tensor cache manifest: $out/windows.maxlen${MAX_LEN}.tensor_cache/manifest.pt" >&2
    exit 3
  fi
  echo "[build][$tag] cache=$out/windows.maxlen${MAX_LEN}.tensor_cache"
}

run_build() {
  print_config
  echo "[build] start $(date '+%F %T')"

  build_one c01 &
  p1=$!
  build_one c04 &
  p2=$!
  build_one c08 &
  p3=$!
  build_one c16 &
  p4=$!
  build_one c32 &
  p5=$!
  wait "$p1" "$p2" "$p3" "$p4" "$p5"

  echo "[merge] -> $OUT_COMB/windows.maxlen${MAX_LEN}.tensor_cache"
  rm -rf "$OUT_COMB"
  "$PY" scripts/merge_v26_tensor_cache_parts.py \
    --out-dir "$OUT_COMB" \
    --max-len "$MAX_LEN" \
    --label-keys "$LABEL_KEYS_NO_DTLB" \
    --jobs "$CACHE_JOBS" \
    --lines-per-shard "$DIRECT_CACHE_SHARD_SIZE" \
    --uop-field-schema v27_ss \
    --uop-field-count 22 \
    --cache data/windows_v27_ss_tail_local_c01/windows.maxlen${MAX_LEN}.tensor_cache \
    --cache data/windows_v27_ss_tail_local_c04/windows.maxlen${MAX_LEN}.tensor_cache \
    --cache data/windows_v27_ss_tail_local_c08/windows.maxlen${MAX_LEN}.tensor_cache \
    --cache data/windows_v27_ss_tail_local_c16/windows.maxlen${MAX_LEN}.tensor_cache \
    --cache data/windows_v27_ss_tail_local_c32/windows.maxlen${MAX_LEN}.tensor_cache

  if [[ ! -s "$OUT_COMB/windows.maxlen${MAX_LEN}.tensor_cache/manifest.pt" ]]; then
    echo "[cache][error] missing tensor cache manifest" >&2
    exit 4
  fi

  "$PY" - "$OUT_COMB/windows.maxlen${MAX_LEN}.tensor_cache/manifest.pt" <<'PY'
import json
import sys
import torch

manifest = torch.load(sys.argv[1], map_location="cpu")
meta = manifest.get("meta", {})
print(json.dumps({
    "total_samples": manifest.get("total_samples"),
    "uop_field_schema": meta.get("uop_field_schema"),
    "uop_field_count": meta.get("uop_field_count"),
    "pmu_keys": meta.get("pmu_keys"),
}, ensure_ascii=False))
PY

  echo "[build] done $(date '+%F %T')"
  echo "[build] data=$OUT_COMB/windows.jsonl"
  echo "[build] cache=$OUT_COMB/windows.maxlen${MAX_LEN}.tensor_cache"
}

case "$ACTION" in
  all)
    run_collect
    run_build
    ;;
  collect)
    run_collect
    ;;
  build)
    run_build
    ;;
  status)
    run_status
    ;;
  *)
    echo "Usage: bash $0 [all|collect|build|status]" >&2
    exit 2
    ;;
esac
