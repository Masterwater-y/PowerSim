#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TSim}
cd "$ROOT"

export TMPDIR="${TMPDIR:-$ROOT/tmp}"
mkdir -p "$TMPDIR" logs

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
MAX_LEN=${MAX_LEN:-32768}
TARGET_WINDOWS=${TARGET_WINDOWS:-10000}
MIN_UOPS=${MIN_UOPS:-256}
JOBS_DEFAULT=${JOBS:-17}
JOBS_C01=${JOBS_C01:-$JOBS_DEFAULT}
JOBS_C04=${JOBS_C04:-12}
JOBS_C08=${JOBS_C08:-8}
JOBS_C16=${JOBS_C16:-8}
JOBS_C32=${JOBS_C32:-8}
CORE_PARALLEL=${CORE_PARALLEL:-1}
CACHE_JOBS=${CACHE_JOBS:-96}
CLEAN=${CLEAN:-1}
BUILD_CACHE=${BUILD_CACHE:-1}
DIRECT_CACHE=${DIRECT_CACHE:-0}
DIRECT_CACHE_SHARD_SIZE=${DIRECT_CACHE_SHARD_SIZE:-512}
DEDUP_THR=${DEDUP_THR:-0.05}
MACRO_SNAP_MAX_RETREAT=${MACRO_SNAP_MAX_RETREAT:-32}
LOCK_DIR=${LOCK_DIR:-$ROOT/tmp/build_v26.1_balanced.lock}
RUN_CORES=${RUN_CORES:-c01,c04,c08,c16,c32}

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "[lock][error] another build appears to be running: $LOCK_DIR" >&2
  echo "[lock][hint] remove it only after confirming no build_v26.1_balanced/build_windows process is active" >&2
  exit 9
fi

cleanup_children() {
  local children
  children=$(jobs -pr || true)
  if [[ -n "$children" ]]; then
    kill $children >/dev/null 2>&1 || true
  fi
}
terminate() {
  cleanup_children
  rmdir "$LOCK_DIR" >/dev/null 2>&1 || true
  exit 130
}
trap terminate INT TERM
trap 'rmdir "$LOCK_DIR" >/dev/null 2>&1 || true' EXIT

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
RAW_C32=${RAW_C32:-data/raw_trace_pool/activecore_train/c32_seedA}

OUT_C01=${OUT_C01:-data/windows_v26.1_balanced_c01}
OUT_C04=${OUT_C04:-data/windows_v26.1_balanced_c04}
OUT_C08=${OUT_C08:-data/windows_v26.1_balanced_c08}
OUT_C16=${OUT_C16:-data/windows_v26.1_balanced_c16}
OUT_C32=${OUT_C32:-data/windows_v26.1_balanced_c32}
COMB=${COMB:-data/windows_v26.1_balanced_all}

CAP_COMMON=${CAP_COMMON:-default=1500,W_chase_low=2000,W_chase_mid=2500,W_chase_high=2000,W_chase_extreme=2500,W_fs_low=2000,W_fs_mid=2500,W_fs_high=2000,W_fs_extreme=2500,W_stream_low=2000,W_stream_high=2000,W_stream_extreme=2500,W_feed_low=2000,W_feed_high=2000,W_fpcd_low=2000,W_fpcd_high=2000,W_adsctr_low=2000,W_adsctr_high=2000}
CAP_C01=${CAP_C01:-default=1800,W_chase_low=2400,W_chase_mid=3000,W_chase_high=2400,W_chase_extreme=3000,W_fs_low=2400,W_fs_mid=3000,W_fs_high=2400,W_fs_extreme=3000,W_stream_low=2400,W_stream_high=2400,W_stream_extreme=3000,W_feed_low=2400,W_feed_high=2400,W_fpcd_low=2400,W_fpcd_high=2400,W_adsctr_low=2400,W_adsctr_high=2400}

LABEL_KEYS=${LABEL_KEYS:-cpi_uop,branch_miss,l1d_ld_miss,l1d_st_miss,l2_ld_miss,l2_st_miss,llc_miss,dtlb_miss}

should_run_core() {
  case ",$RUN_CORES," in
    *",$1,"*) return 0 ;;
    *) return 1 ;;
  esac
}

if [[ "$CLEAN" == "1" ]]; then
  echo "[clean] remove selected v26.1 balanced windows/cache run_cores=$RUN_CORES"
  should_run_core c01 && rm -rf "$OUT_C01"
  should_run_core c04 && rm -rf "$OUT_C04"
  should_run_core c08 && rm -rf "$OUT_C08"
  should_run_core c16 && rm -rf "$OUT_C16"
  should_run_core c32 && rm -rf "$OUT_C32"
  rm -rf "$COMB"
fi

run_build() {
  local tag="$1"
  local raw="$2"
  local out="$3"
  local cap_spec="$4"
  local jobs="$5"
  local log="logs/build_v26.1_balanced_${tag}.log"

  if [[ ! -d "$raw" ]]; then
    echo "[build][$tag][error] missing raw root: $raw" >&2
    exit 2
  fi

  echo "[build][$tag] raw=$raw out=$out max_len=$MAX_LEN target_windows=$TARGET_WINDOWS jobs=$jobs log=$log"
  echo "[build][$tag] workloads=${WORKLOADS[*]}"
  local direct_args=()
  if [[ "$DIRECT_CACHE" == "1" ]]; then
    direct_args=(
      --direct-tensor-cache
      --direct-cache-shard-size "$DIRECT_CACHE_SHARD_SIZE"
      --cache-max-len "$MAX_LEN"
      --cache-label-keys "$LABEL_KEYS"
    )
  fi
  "$PY" data/build_windows.py \
    --raw "$raw" \
    --out "$out" \
    --tq-max-len "$MAX_LEN" \
    --tq-target-windows "$TARGET_WINDOWS" \
    --tq-min-uops-per-core "$MIN_UOPS" \
    --per-workload-cap "$cap_spec" \
    --per-workload-cap-seed 0 \
    --dedup-threshold "$DEDUP_THR" \
    --dedup-jobs "$jobs" \
    --jobs "$jobs" \
    --workloads "${WORKLOADS[@]}" \
    --query-placement tail_local \
    --uop-field-schema v26_14 \
    --macro-snap-max-retreat "$MACRO_SNAP_MAX_RETREAT" \
    --no-cache \
    "${direct_args[@]}" \
    > "$log" 2>&1
  if [[ "$DIRECT_CACHE" == "1" && -s "$out/windows.maxlen${MAX_LEN}.tensor_cache/manifest.pt" ]]; then
    echo "[build][$tag] cache=$out/windows.maxlen${MAX_LEN}.tensor_cache"
  else
    echo "[build][$tag] windows=$(wc -l < "$out/windows.jsonl")"
  fi
}

build_rc=0
pids=()
launch_build() {
  local tag="$1"
  local raw="$2"
  local out="$3"
  local cap_spec="$4"
  local jobs="$5"
  run_build "$tag" "$raw" "$out" "$cap_spec" "$jobs" & pids+=("$!")
  while (( ${#pids[@]} >= CORE_PARALLEL )); do
    local p="${pids[0]}"
    if ! wait "$p"; then
      build_rc=1
    fi
    pids=("${pids[@]:1}")
  done
}

echo "[config] max_len=$MAX_LEN target_windows=$TARGET_WINDOWS core_parallel=$CORE_PARALLEL run_cores=$RUN_CORES jobs_c01=$JOBS_C01 jobs_c04=$JOBS_C04 jobs_c08=$JOBS_C08 jobs_c16=$JOBS_C16 jobs_c32=$JOBS_C32 cache_jobs=$CACHE_JOBS"
should_run_core c01 && launch_build c01 "$RAW_C01" "$OUT_C01" "$CAP_C01" "$JOBS_C01"
should_run_core c04 && launch_build c04 "$RAW_C04" "$OUT_C04" "$CAP_COMMON" "$JOBS_C04"
should_run_core c08 && launch_build c08 "$RAW_C08" "$OUT_C08" "$CAP_COMMON" "$JOBS_C08"
should_run_core c16 && launch_build c16 "$RAW_C16" "$OUT_C16" "$CAP_COMMON" "$JOBS_C16"
should_run_core c32 && launch_build c32 "$RAW_C32" "$OUT_C32" "$CAP_COMMON" "$JOBS_C32"

for p in "${pids[@]}"; do
  if ! wait "$p"; then
    build_rc=1
  fi
done
if [[ "${build_rc:-0}" != "0" ]]; then
  echo "[error] one or more per-core-count builds failed; see logs/build_v26.1_balanced_c*.log" >&2
  exit "$build_rc"
fi

for part in \
  "$OUT_C01/windows.jsonl" \
  "$OUT_C04/windows.jsonl" \
  "$OUT_C08/windows.jsonl" \
  "$OUT_C16/windows.jsonl" \
  "$OUT_C32/windows.jsonl"; do
  part_dir="$(dirname "$part")"
  if [[ ! -s "$part" && ! -s "$part_dir/windows.maxlen${MAX_LEN}.tensor_cache/manifest.pt" ]]; then
    echo "[error] missing or empty windows file: $part" >&2
    exit 3
  fi
done

if [[ "$DIRECT_CACHE" == "1" ]]; then
  echo "[merge-cache] -> $COMB/windows.maxlen${MAX_LEN}.tensor_cache"
  jsonl_args=()
  cache_args=()
  for out in "$OUT_C01" "$OUT_C04" "$OUT_C08" "$OUT_C16" "$OUT_C32"; do
    if [[ -s "$out/windows.maxlen${MAX_LEN}.tensor_cache/manifest.pt" ]]; then
      cache_args+=(--cache "$out/windows.maxlen${MAX_LEN}.tensor_cache")
    elif [[ -s "$out/windows.jsonl" ]]; then
      jsonl_args+=(--jsonl "$out/windows.jsonl")
    else
      echo "[error] missing part output: $out" >&2
      exit 3
    fi
  done
  rm -rf "$COMB"
  "$PY" scripts/merge_v26_tensor_cache_parts.py \
    --out-dir "$COMB" \
    --max-len "$MAX_LEN" \
    --label-keys "$LABEL_KEYS" \
    --jobs "$CACHE_JOBS" \
    --lines-per-shard 512 \
    "${jsonl_args[@]}" \
    "${cache_args[@]}"
  if [[ ! -s "$COMB/windows.maxlen${MAX_LEN}.tensor_cache/manifest.pt" ]]; then
    echo "[error] missing tensor cache manifest" >&2
    exit 4
  fi
  echo "[done] windows_stub=$COMB/windows.jsonl"
  echo "[done] cache=$COMB/windows.maxlen${MAX_LEN}.tensor_cache"
  exit 0
fi

echo "[merge] -> $COMB/windows.jsonl"
rm -rf "$COMB"
mkdir -p "$COMB"
cat \
  "$OUT_C01/windows.jsonl" \
  "$OUT_C04/windows.jsonl" \
  "$OUT_C08/windows.jsonl" \
  "$OUT_C16/windows.jsonl" \
  "$OUT_C32/windows.jsonl" \
  > "$COMB/windows.jsonl"

echo "[merge] total_windows=$(wc -l < "$COMB/windows.jsonl")"

"$PY" - "$COMB/windows.jsonl" <<'PY'
import json
import sys
from collections import Counter

path = sys.argv[1]
variants = Counter()
cores = Counter()
with open(path) as f:
    for line in f:
        if not line.startswith("{"):
            continue
        rec = json.loads(line)
        variants[rec.get("workload_variant") or rec.get("workload")] += 1
        cores[int(rec.get("n_core", 0) or 0)] += 1
print("[audit] n_core=" + json.dumps(dict(sorted(cores.items())), ensure_ascii=False))
print("[audit] variants=" + json.dumps(dict(sorted(variants.items())), ensure_ascii=False))
PY

if [[ "$BUILD_CACHE" == "1" ]]; then
  echo "[cache] build tensor cache max_len=$MAX_LEN"
  "$PY" scripts/prepare_dataset_cache.py \
    --data "$COMB/windows.jsonl" \
    --max-len "$MAX_LEN" \
    --label-keys "$LABEL_KEYS" \
    --jobs "$CACHE_JOBS" \
    --lines-per-shard 512

  if [[ ! -s "$COMB/windows.maxlen${MAX_LEN}.tensor_cache/manifest.pt" ]]; then
    echo "[error] missing tensor cache manifest" >&2
    exit 4
  fi
fi

echo "[done] windows=$COMB/windows.jsonl"
echo "[done] cache=$COMB/windows.maxlen${MAX_LEN}.tensor_cache"
