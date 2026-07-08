#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TSim}
cd "$ROOT"

export TMPDIR="${TMPDIR:-$ROOT/tmp}"
mkdir -p "$TMPDIR" logs

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
MAX_LEN=${MAX_LEN:-32768}
CAP=${CAP:-600}
TARGET_WINDOWS=${TARGET_WINDOWS:-1200}
MIN_UOPS=${MIN_UOPS:-256}
JOBS=${JOBS:-17}
CLEAN=${CLEAN:-1}

DEFAULT_WORKLOADS=(
  W_ads_ctr W_ads_ranking_proxy W_branch_storm W_chase_dram
  W_compute_int W_false_sharing W_feed_ranking W_fp_compute_dense
  W_fp_lite W_graph_recall_proxy W_indirect W_int_div
  W_interest_graph_recall W_mlp_light W_search_index_proxy W_stream
)
read -r -a WORKLOADS <<< "${WORKLOADS:-${DEFAULT_WORKLOADS[*]}}"

RAW_C01=${RAW_C01:-data/raw_trace_pool/activecore_train/c01_seedA}
RAW_C04=${RAW_C04:-data/raw_trace_pool/activecore_train/c04_seedA}
RAW_C08=${RAW_C08:-data/raw_trace_pool/activecore_train/c08_seedA}
RAW_C16=${RAW_C16:-data/raw_trace_pool/activecore_train/c16_seedA}

OUT_C01=${OUT_C01:-data/windows_v26_clean14_tail_local_c01}
OUT_C04=${OUT_C04:-data/windows_v26_clean14_tail_local_c04}
OUT_C08=${OUT_C08:-data/windows_v26_clean14_tail_local_c08}
OUT_C16=${OUT_C16:-data/windows_v26_clean14_tail_local_c16}
COMB=${COMB:-data/windows_v26_clean14_tail_local_all}

if [[ "$CLEAN" == "1" ]]; then
  echo "[clean] remove old v26 clean14 windows/cache"
  rm -rf "$OUT_C01" "$OUT_C04" "$OUT_C08" "$OUT_C16" "$COMB"
fi

run_build() {
  local tag="$1"
  local raw="$2"
  local out="$3"
  local log="logs/build_v26_clean14_tail_local_${tag}.log"

  if [[ ! -d "$raw" ]]; then
    echo "[build][$tag][error] missing raw root: $raw" >&2
    exit 2
  fi

  echo "[build][$tag] raw=$raw out=$out jobs=$JOBS log=$log"
  echo "[build][$tag] workloads=${WORKLOADS[*]}"
  cap_args=()
  if [[ "$CAP" != "0" && "$CAP" != "none" && "$CAP" != "NONE" && "$CAP" != "all" && "$CAP" != "ALL" ]]; then
    cap_args=(--per-workload-cap "$CAP" --per-workload-cap-seed 0)
  fi
  "$PY" data/build_windows.py \
    --raw "$raw" \
    --out "$out" \
    --tq-max-len "$MAX_LEN" \
    --tq-target-windows "$TARGET_WINDOWS" \
    --tq-min-uops-per-core "$MIN_UOPS" \
    "${cap_args[@]}" \
    --dedup-threshold 0.05 \
    --dedup-jobs "$JOBS" \
    --jobs "$JOBS" \
    --workloads "${WORKLOADS[@]}" \
    --query-placement tail_local \
    --uop-field-schema v26_14 \
    --no-cache \
    > "$log" 2>&1
  echo "[build][$tag] done"
}

pids=()
run_build c01 "$RAW_C01" "$OUT_C01" & pids+=("$!")
run_build c04 "$RAW_C04" "$OUT_C04" & pids+=("$!")
run_build c08 "$RAW_C08" "$OUT_C08" & pids+=("$!")
run_build c16 "$RAW_C16" "$OUT_C16" & pids+=("$!")

for p in "${pids[@]}"; do
  wait "$p"
done

for part in \
  "$OUT_C01/windows.jsonl" \
  "$OUT_C04/windows.jsonl" \
  "$OUT_C08/windows.jsonl" \
  "$OUT_C16/windows.jsonl"; do
  if [[ ! -s "$part" ]]; then
    echo "[error] missing or empty windows file: $part" >&2
    exit 3
  fi
done

echo "[merge] -> $COMB/windows.jsonl"
rm -rf "$COMB"
mkdir -p "$COMB"
cat \
  "$OUT_C01/windows.jsonl" \
  "$OUT_C04/windows.jsonl" \
  "$OUT_C08/windows.jsonl" \
  "$OUT_C16/windows.jsonl" \
  > "$COMB/windows.jsonl"

echo "[merge] windows=$(wc -l < "$COMB/windows.jsonl")"

echo "[cache] build tensor cache"
"$PY" scripts/prepare_dataset_cache.py \
  --data "$COMB/windows.jsonl" \
  --max-len "$MAX_LEN" \
  --label-keys cpi_uop,branch_miss,l1d_ld_miss,l1d_st_miss,l2_ld_miss,l2_st_miss,llc_miss,dtlb_miss \
  --jobs "$JOBS" \
  --lines-per-shard 512

if [[ ! -s "$COMB/windows.maxlen${MAX_LEN}.tensor_cache/manifest.pt" ]]; then
  echo "[error] missing tensor cache manifest" >&2
  exit 4
fi

echo "[done] windows=$COMB/windows.jsonl"
echo "[done] cache=$COMB/windows.maxlen${MAX_LEN}.tensor_cache"
