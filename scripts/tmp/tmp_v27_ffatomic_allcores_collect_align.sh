#!/usr/bin/env bash
# Collect v27 raw gem5 traces and build aligned parquet for all requested core
# counts. Both core-count groups and workloads within each group can run in
# parallel; CORE_PARALLEL controls the former.
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TSim}
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
CORES_LIST=${CORES_LIST:-"1 4 8 16 32"}
CORE_PARALLEL=${CORE_PARALLEL:-1}

# gem5 collection knobs.
COLLECT_PARALLEL=${COLLECT_PARALLEL:-18}
TARGET_PER_CORE=${TARGET_PER_CORE:-500000}
MIN_ACCEPT_PER_CORE=${MIN_ACCEPT_PER_CORE:-450000}
MAX_ACCEPT_PER_CORE=${MAX_ACCEPT_PER_CORE:-0}
PROBE_SCALE=${PROBE_SCALE:-1}
PROBE_STOP_REC=${PROBE_STOP_REC:-700000}
TIMEOUT_SECS=${TIMEOUT_SECS:-7200}
MAX_FINAL_ATTEMPTS=${MAX_FINAL_ATTEMPTS:-3}
SCALE_MARGIN_PCT=${SCALE_MARGIN_PCT:-110}
REUSE_PROBE_IF_SUFFICIENT=${REUSE_PROBE_IF_SUFFICIENT:-1}
RUN_TO_COMPLETION=${RUN_TO_COMPLETION:-1}
SEED=${SEED:-0}

# aligned parquet conversion knobs.
CONVERT_PARALLEL=${CONVERT_PARALLEL:-18}
ROW_GROUP_SIZE=${ROW_GROUP_SIZE:-65536}
OVERWRITE_ALIGNED=${OVERWRITE_ALIGNED:-0}
# Once all workload/core aligned parquet files are successfully materialized,
# the TCSim cold16 builder can consume them directly.  Set to 1 to release the
# much larger records/labels JSONL source files after each serial core group.
DROP_RAW_JSONL_AFTER_ALIGN=${DROP_RAW_JSONL_AFTER_ALIGN:-0}

MODE=${MODE:-all}
RUN_TAG=${RUN_TAG:-v27_ffatomic_seed${SEED}}
DATA_PREFIX=${DATA_PREFIX:-data/raw_${RUN_TAG}}
LOG_ROOT=${LOG_ROOT:-logs/tmp/${RUN_TAG}_$(date +%Y%m%d_%H%M%S)}

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

case "$MODE" in
  train)
    WORKLOADS=("${TRAIN_WORKLOADS[@]}")
    ;;
  heldout)
    WORKLOADS=("${HELDOUT_WORKLOADS[@]}")
    ;;
  all)
    WORKLOADS=("${TRAIN_WORKLOADS[@]}" "${HELDOUT_WORKLOADS[@]}")
    ;;
  *)
    echo "[error] MODE must be train, heldout, or all; got MODE=$MODE" >&2
    exit 2
    ;;
esac

cd "$ROOT"
mkdir -p "$LOG_ROOT"

echo "[meta] ROOT=$ROOT"
echo "[meta] PY=$PY"
echo "[meta] CORES_LIST=$CORES_LIST"
echo "[meta] CORE_PARALLEL=$CORE_PARALLEL"
echo "[meta] MODE=$MODE workloads=${#WORKLOADS[@]} ${WORKLOADS[*]}"
echo "[meta] RUN_TAG=$RUN_TAG"
echo "[meta] DATA_PREFIX=$DATA_PREFIX"
echo "[meta] LOG_ROOT=$LOG_ROOT"
echo "[meta] FF_ATOMIC=1 COLLECT_PARALLEL=$COLLECT_PARALLEL CONVERT_PARALLEL=$CONVERT_PARALLEL"
echo "[meta] TARGET_PER_CORE=$TARGET_PER_CORE MIN_ACCEPT_PER_CORE=$MIN_ACCEPT_PER_CORE MAX_ACCEPT_PER_CORE=$MAX_ACCEPT_PER_CORE"
echo "[meta] PROBE_SCALE=$PROBE_SCALE PROBE_STOP_REC=$PROBE_STOP_REC TIMEOUT_SECS=$TIMEOUT_SECS"
echo "[meta] RUN_TO_COMPLETION=$RUN_TO_COMPLETION"
echo "[meta] DROP_RAW_JSONL_AFTER_ALIGN=$DROP_RAW_JSONL_AFTER_ALIGN"
echo

echo "[build] make -C workloads"
make -C workloads
echo

count_aligned() {
  local raw_root=$1
  find -L "$raw_root" -maxdepth 3 -type f -name '*.aligned.parquet' 2>/dev/null \
    | wc -l | tr -d ' '
}

verify_raw_workloads() {
  local raw_root=$1
  local missing=0
  local wl
  for wl in "${WORKLOADS[@]}"; do
    local wd="W_${wl}"
    if [[ ! -d "$raw_root/$wd/tao_trace" ]]; then
      echo "[verify-raw][FAIL] missing $raw_root/$wd/tao_trace" >&2
      missing=$((missing + 1))
    else
      echo "[verify-raw][OK]   $wd"
    fi
  done
  return "$missing"
}

convert_aligned_for_core() {
  local raw_root=$1
  local ncore=$2
  local log_dir=$3
  local expected=$(( ncore * ${#WORKLOADS[@]} ))
  local before
  local after
  local fails=0
  local wl

  mkdir -p "$log_dir"
  before=$(count_aligned "$raw_root")
  echo "[align][c${ncore}] before=$before expected=$expected raw=$raw_root"

  if (( before >= expected && OVERWRITE_ALIGNED != 1 )); then
    echo "[align][c${ncore}] skip conversion: already complete"
  else
    for wl in "${WORKLOADS[@]}"; do
      local wd="W_${wl}"
      while (( $(jobs -pr | wc -l) >= CONVERT_PARALLEL )); do
        wait -n || fails=$((fails + 1))
      done
      {
        args=(
          scripts/convert_trace_to_aligned_parquet.py
          --raw-root "$raw_root"
          --workloads "$wd"
          --row-group-size "$ROW_GROUP_SIZE"
        )
        if [[ "$OVERWRITE_ALIGNED" == "1" ]]; then
          args+=(--overwrite)
        fi
        echo "[convert] c${ncore} $wd"
        "$PY" "${args[@]}"
      } > "$log_dir/${wd}.log" 2>&1 &
    done

    while (( $(jobs -pr | wc -l) > 0 )); do
      wait -n || fails=$((fails + 1))
    done
  fi

  after=$(count_aligned "$raw_root")
  echo "[align][c${ncore}] after=$after expected=$expected fails=$fails"
  if (( fails > 0 || after < expected )); then
    echo "[align][c${ncore}][FAIL] conversion incomplete logs=$log_dir" >&2
    return 1
  fi
  echo "[align][c${ncore}][OK]"
}

drop_raw_jsonl_for_core() {
  local raw_root=$1
  local ncore=$2
  local log_dir=$3
  local before after
  if [[ "$DROP_RAW_JSONL_AFTER_ALIGN" != "1" ]]; then
    return 0
  fi
  before=$(find "$raw_root" -type f \( -name '*.records.micro.jsonl' -o -name '*.labels.micro.jsonl' \) | wc -l | tr -d ' ')
  echo "[drop-raw][c${ncore}] deleting source JSONL files count=$before" | tee "$log_dir/drop_raw.log"
  find "$raw_root" -type f \( -name '*.records.micro.jsonl' -o -name '*.labels.micro.jsonl' \) -print -delete >> "$log_dir/drop_raw.log"
  after=$(find "$raw_root" -type f \( -name '*.records.micro.jsonl' -o -name '*.labels.micro.jsonl' \) | wc -l | tr -d ' ')
  if (( after != 0 )); then
    echo "[drop-raw][c${ncore}][FAIL] remaining source JSONL=$after" >&2
    return 1
  fi
  echo "[drop-raw][c${ncore}][OK] aligned parquet retained; source JSONL removed" | tee -a "$log_dir/drop_raw.log"
}

run_core() {
  local ncore=$1
  local ctag
  local raw_root
  local core_log_dir
  printf -v ctag "c%02d" "$ncore"
  raw_root="${DATA_PREFIX}_${ctag}"
  core_log_dir="$LOG_ROOT/$ctag"
  mkdir -p "$core_log_dir"

  echo
  echo "==================== core=$ncore start $(date '+%F %T') ===================="
  echo "[core][$ctag] raw_root=$raw_root logs=$core_log_dir"

  FF_ATOMIC=1 \
  PARALLEL="$COLLECT_PARALLEL" \
  NUM_CORES="$ncore" \
  OUT_BASE="$raw_root" \
  SEED="$SEED" \
  TARGET_PER_CORE="$TARGET_PER_CORE" \
  MIN_ACCEPT_PER_CORE="$MIN_ACCEPT_PER_CORE" \
  MAX_ACCEPT_PER_CORE="$MAX_ACCEPT_PER_CORE" \
  PROBE_SCALE="$PROBE_SCALE" \
  PROBE_STOP_REC="$PROBE_STOP_REC" \
  TIMEOUT_SECS="$TIMEOUT_SECS" \
  MAX_FINAL_ATTEMPTS="$MAX_FINAL_ATTEMPTS" \
  SCALE_MARGIN_PCT="$SCALE_MARGIN_PCT" \
  REUSE_PROBE_IF_SUFFICIENT="$REUSE_PROBE_IF_SUFFICIENT" \
  RUN_TO_COMPLETION="$RUN_TO_COMPLETION" \
  VALIDATE_WINDOWS=0 \
  bash scripts/collect_v27_workloads.sh "$MODE" \
    2>&1 | tee "$core_log_dir/collect.log"

  verify_raw_workloads "$raw_root" 2>&1 | tee "$core_log_dir/verify_raw.log"
  convert_aligned_for_core "$raw_root" "$ncore" "$core_log_dir/align" \
    2>&1 | tee "$core_log_dir/align.log"
  drop_raw_jsonl_for_core "$raw_root" "$ncore" "$core_log_dir"

  echo "[core][$ctag] done $(date '+%F %T')"
}

for ncore in $CORES_LIST; do
  while (( $(jobs -pr | wc -l) >= CORE_PARALLEL )); do
    wait -n
  done
  run_core "$ncore" &
done
while (( $(jobs -pr | wc -l) > 0 )); do
  wait -n
done

echo
echo "==================== all done $(date '+%F %T') ===================="
for ncore in $CORES_LIST; do
  printf -v ctag "c%02d" "$ncore"
  raw_root="${DATA_PREFIX}_${ctag}"
  expected=$(( ncore * ${#WORKLOADS[@]} ))
  aligned=$(count_aligned "$raw_root")
  printf "[summary] %-36s aligned=%s/%s\n" "$raw_root" "$aligned" "$expected"
done
echo "[summary] logs=$LOG_ROOT"
