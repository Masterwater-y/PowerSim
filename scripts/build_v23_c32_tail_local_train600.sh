#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
cd "$ROOT"

export TMPDIR="${TMPDIR:-$ROOT/tmp}"
mkdir -p "$TMPDIR" logs

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
BASE_MODEL=${BASE_MODEL:-Qwen/Qwen3-0.6B-Base}
MAX_LEN=${MAX_LEN:-32768}
CAP=${CAP:-600}
TARGET_WINDOWS=${TARGET_WINDOWS:-1200}
MIN_UOPS=${MIN_UOPS:-256}
JOBS=${JOBS:-8}
CLEAN=${CLEAN:-0}
BUILD_CACHE=${BUILD_CACHE:-1}

# There is no activecore_train/c32_seedA raw set yet. This intentionally builds
# a 32-core augmentation from the available seedB infer17 trace pool.
RAW_C32=${RAW_C32:-data/raw_trace_pool/activecore_eval/c32_seedB_infer17}
BASE_DATA=${BASE_DATA:-data/windows_v16_v9core_tail_local_all}
OUT_C32=${OUT_C32:-data/windows_v23_v16_tail_local_c32_seedB_infer17}
COMB=${COMB:-data/windows_v23_v16_tail_local_all_plus_c32_seedB}

DEFAULT_WORKLOADS=(
  W_ads_ctr W_ads_ranking_proxy W_branch_storm W_chase_dram
  W_compute_int W_false_sharing W_feed_ranking W_fp_compute_dense
  W_fp_lite W_graph_recall_proxy W_indirect W_int_div
  W_interest_graph_recall W_mlp_light W_phased_mix W_search_index_proxy
  W_stream
)
read -r -a WORKLOADS <<< "${WORKLOADS:-${DEFAULT_WORKLOADS[*]}}"

if [[ "$CLEAN" == "1" ]]; then
  echo "[clean] remove old c32/combined outputs"
  rm -rf "$OUT_C32" "$COMB"
fi

if [[ ! -d "$RAW_C32" ]]; then
  echo "[error] missing c32 raw root: $RAW_C32" >&2
  exit 2
fi
if [[ ! -s "$BASE_DATA/windows.jsonl" ]]; then
  echo "[error] missing base windows: $BASE_DATA/windows.jsonl" >&2
  exit 3
fi

echo "[build-c32] raw=$RAW_C32"
echo "[build-c32] out=$OUT_C32 jobs=$JOBS cap=$CAP target=$TARGET_WINDOWS"
echo "[build-c32] workloads=${WORKLOADS[*]}"
"$PY" data/build_windows.py \
  --raw "$RAW_C32" \
  --out "$OUT_C32" \
  --tq-max-len "$MAX_LEN" \
  --tq-target-windows "$TARGET_WINDOWS" \
  --tq-min-uops-per-core "$MIN_UOPS" \
  --per-workload-cap "$CAP" \
  --per-workload-cap-seed 0 \
  --dedup-threshold 0.05 \
  --dedup-jobs "$JOBS" \
  --jobs "$JOBS" \
  --workloads "${WORKLOADS[@]}" \
  --query-placement tail_local \
  --no-cache \
  > "logs/build_v23_v16_tail_local_c32_seedB.log" 2>&1

if [[ ! -s "$OUT_C32/windows.jsonl" ]]; then
  echo "[error] missing c32 windows: $OUT_C32/windows.jsonl" >&2
  exit 4
fi

echo "[merge] base=$BASE_DATA c32=$OUT_C32 -> $COMB"
rm -rf "$COMB"
mkdir -p "$COMB"
cat \
  "$BASE_DATA/windows.jsonl" \
  "$OUT_C32/windows.jsonl" \
  > "$COMB/windows.jsonl"

echo "[merge] base_windows=$(wc -l < "$BASE_DATA/windows.jsonl")"
echo "[merge] c32_windows=$(wc -l < "$OUT_C32/windows.jsonl")"
echo "[merge] total_windows=$(wc -l < "$COMB/windows.jsonl")"

if [[ "$BUILD_CACHE" == "1" ]]; then
  echo "[cache] build tensor cache for $COMB"
  "$PY" scripts/prepare_dataset_cache.py \
    --data "$COMB/windows.jsonl" \
    --base-model "$BASE_MODEL" \
    --max-len "$MAX_LEN" \
    --format tensor \
    --jobs "$JOBS" \
    --lines-per-shard 512

  if [[ ! -s "$COMB/windows.maxlen${MAX_LEN}.tensor_cache/manifest.pt" ]]; then
    echo "[error] missing tensor cache manifest" >&2
    exit 5
  fi
fi

echo "[done] c32_windows=$OUT_C32/windows.jsonl"
echo "[done] combined_windows=$COMB/windows.jsonl"
echo "[done] combined_cache=$COMB/windows.maxlen${MAX_LEN}.tensor_cache"
