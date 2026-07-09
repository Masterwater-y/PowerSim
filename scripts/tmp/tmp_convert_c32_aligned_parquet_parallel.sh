#!/usr/bin/env bash
set -euo pipefail

# Parallel aligned-parquet conversion for C32 seedA raw traces.
# The underlying converter is per-workload sequential, so this driver runs
# multiple workload conversions concurrently.

ROOT=${ROOT:-/data00/yinhaolang/TSim}
cd "$ROOT"

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
RAW_ROOT=${RAW_ROOT:-data/raw_trace_pool/activecore_train/c32_seedA}
PARALLEL=${PARALLEL:-17}
ROW_GROUP_SIZE=${ROW_GROUP_SIZE:-65536}
OVERWRITE=${OVERWRITE:-0}
LOG_DIR=${LOG_DIR:-logs/convert_c32_aligned_parquet}

mkdir -p "$LOG_DIR"

if [[ ! -d "$RAW_ROOT" ]]; then
  echo "[error] missing RAW_ROOT=$RAW_ROOT" >&2
  exit 2
fi

mapfile -t WORKLOADS < <(
  find -L "$RAW_ROOT" -maxdepth 1 -type d -name 'W_*' -printf '%f\n' | sort
)

if (( ${#WORKLOADS[@]} == 0 )); then
  echo "[error] no W_* workloads under $RAW_ROOT" >&2
  exit 3
fi

echo "[config] RAW_ROOT=$RAW_ROOT"
echo "[config] PARALLEL=$PARALLEL ROW_GROUP_SIZE=$ROW_GROUP_SIZE OVERWRITE=$OVERWRITE"
echo "[config] workloads=${#WORKLOADS[@]}: ${WORKLOADS[*]}"
echo "[config] per-workload logs -> $LOG_DIR"

run_one() {
  local wd=$1
  local log="$LOG_DIR/${wd}.log"
  local -a extra=()
  if [[ "$OVERWRITE" == "1" ]]; then
    extra+=(--overwrite)
  fi
  echo "[start] $wd log=$log"
  "$PY" scripts/convert_trace_to_aligned_parquet.py \
    --raw-root "$RAW_ROOT" \
    --workloads "$wd" \
    --row-group-size "$ROW_GROUP_SIZE" \
    "${extra[@]}" \
    > "$log" 2>&1
  echo "[done]  $wd"
}

pids=()
for wd in "${WORKLOADS[@]}"; do
  while (( $(jobs -pr | wc -l) >= PARALLEL )); do
    wait -n
  done
  run_one "$wd" &
  pids+=("$!")
done

rc=0
for p in "${pids[@]}"; do
  if ! wait "$p"; then
    rc=1
  fi
done

total=$(
  find -L "$RAW_ROOT" -maxdepth 3 -type f -name '*.aligned.parquet' | wc -l | tr -d ' '
)
echo "[summary] aligned_parquet_files=$total expected=$(( ${#WORKLOADS[@]} * 32 ))"

if (( rc != 0 )); then
  echo "[summary] at least one workload conversion failed" >&2
  exit "$rc"
fi

echo "[summary] all conversions completed"
