#!/usr/bin/env bash
# Collect the v7 8-core dataset for the 17 active workloads.
#
# Default contract:
#   - 17 workloads, NUM_CORES=8, SEED=0
#   - PARALLEL=17 gem5 jobs
#   - accept each final trace only when every core has 500k-1M records
#   - build mode uses the current TQ training-window scheme

set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
cd "$ROOT"

ACTION=${1:-collect}

OUT_BASE=${OUT_BASE:-$ROOT/data/raw_v7_seedA_c08}
WINDOW_OUT=${WINDOW_OUT:-$ROOT/data/windows_v7_c08_tq}
LOG_DIR=${LOG_DIR:-$ROOT/logs}

NUM_CORES=${NUM_CORES:-8}
PARALLEL=${PARALLEL:-17}
SEED=${SEED:-0}

TARGET_PER_CORE=${TARGET_PER_CORE:-700000}
MIN_ACCEPT_PER_CORE=${MIN_ACCEPT_PER_CORE:-500000}
MAX_ACCEPT_PER_CORE=${MAX_ACCEPT_PER_CORE:-1000000}
PROBE_SCALE=${PROBE_SCALE:-1}
PROBE_STOP_REC=${PROBE_STOP_REC:-700000}
PROGRESS_INTERVAL=${PROGRESS_INTERVAL:-5}
REUSE_PROBE_IF_SUFFICIENT=${REUSE_PROBE_IF_SUFFICIENT:-1}
MAX_FINAL_ATTEMPTS=${MAX_FINAL_ATTEMPTS:-5}
TIMEOUT_SECS=${TIMEOUT_SECS:-7200}
VALIDATE_WINDOWS=${VALIDATE_WINDOWS:-0}
REC_LAB_DELTA_MAX=${REC_LAB_DELTA_MAX:-64}
MAX_ACCEPT_SLACK_PCT=${MAX_ACCEPT_SLACK_PCT:-2}

MAXLEN=${MAXLEN:-32768}
TQ_TARGET_WINDOWS=${TQ_TARGET_WINDOWS:-1200}
BUILD_JOBS=${BUILD_JOBS:-8}
PREPARE_CACHE=${PREPARE_CACHE:-0}

WORKLOADS=(
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

usage() {
  cat <<EOF
Usage:
  bash scripts/collect_v7_c08_parallel.sh collect   # run 8c collection
  bash scripts/collect_v7_c08_parallel.sh status    # show progress
  bash scripts/collect_v7_c08_parallel.sh verify    # check per-core record counts
  bash scripts/collect_v7_c08_parallel.sh build     # build windows after verify

Common overrides:
  CLEAN=1                         remove OUT_BASE before collect
  PARALLEL=17                     concurrent gem5 jobs
  OUT_BASE=$OUT_BASE
  WINDOW_OUT=$WINDOW_OUT
  MIN_ACCEPT_PER_CORE=$MIN_ACCEPT_PER_CORE
  MAX_ACCEPT_PER_CORE=$MAX_ACCEPT_PER_CORE
EOF
}

print_config() {
  echo "[config] ROOT=$ROOT"
  echo "[config] OUT_BASE=$OUT_BASE"
  echo "[config] WINDOW_OUT=$WINDOW_OUT"
  echo "[config] NUM_CORES=$NUM_CORES PARALLEL=$PARALLEL SEED=$SEED"
  echo "[config] TARGET_PER_CORE=$TARGET_PER_CORE MIN_ACCEPT_PER_CORE=$MIN_ACCEPT_PER_CORE MAX_ACCEPT_PER_CORE=$MAX_ACCEPT_PER_CORE"
  echo "[config] REC_LAB_DELTA_MAX=$REC_LAB_DELTA_MAX MAX_ACCEPT_SLACK_PCT=$MAX_ACCEPT_SLACK_PCT"
  echo "[config] PROBE_SCALE=$PROBE_SCALE PROBE_STOP_REC=$PROBE_STOP_REC PROGRESS_INTERVAL=$PROGRESS_INTERVAL"
  echo "[config] TIMEOUT_SECS=$TIMEOUT_SECS MAX_FINAL_ATTEMPTS=$MAX_FINAL_ATTEMPTS"
  echo "[config] workloads=${#WORKLOADS[@]}"
}

check_bins() {
  local missing=0
  local wl
  for wl in "${WORKLOADS[@]}"; do
    if [[ ! -x "$ROOT/workloads/bin/$wl" ]]; then
      echo "[error] missing executable: $ROOT/workloads/bin/$wl" >&2
      missing=1
    fi
  done
  if (( missing != 0 )); then
    echo "[hint] build workloads first: make -C $ROOT/workloads" >&2
    exit 1
  fi
}

prefixed_workloads() {
  local wl
  for wl in "${WORKLOADS[@]}"; do
    echo "W_$wl"
  done
}

run_collect() {
  check_bins
  mkdir -p "$LOG_DIR"
  if [[ "${CLEAN:-0}" == "1" ]]; then
    echo "[collect] CLEAN=1, removing $OUT_BASE"
    rm -rf "$OUT_BASE"
  fi
  mkdir -p "$OUT_BASE"
  print_config
  echo "[collect] start: $(date '+%F %T')"
  echo "[collect] log tip: redirect this script to $LOG_DIR/collect_v7_c08.log when running with nohup"

  OUT_BASE="$OUT_BASE" \
  NUM_CORES="$NUM_CORES" \
  PARALLEL="$PARALLEL" \
  SEED="$SEED" \
  TARGET_PER_CORE="$TARGET_PER_CORE" \
  MIN_ACCEPT_PER_CORE="$MIN_ACCEPT_PER_CORE" \
  MAX_ACCEPT_PER_CORE="$MAX_ACCEPT_PER_CORE" \
  PROBE_SCALE="$PROBE_SCALE" \
  PROBE_STOP_REC="$PROBE_STOP_REC" \
  PROGRESS_INTERVAL="$PROGRESS_INTERVAL" \
  REUSE_PROBE_IF_SUFFICIENT="$REUSE_PROBE_IF_SUFFICIENT" \
  MAX_FINAL_ATTEMPTS="$MAX_FINAL_ATTEMPTS" \
  TIMEOUT_SECS="$TIMEOUT_SECS" \
  VALIDATE_WINDOWS="$VALIDATE_WINDOWS" \
  bash "$ROOT/scripts/collect_parallel_500k.sh" "${WORKLOADS[@]}"
}

show_status() {
  local done_n
  done_n=$(find "$OUT_BASE" -maxdepth 1 -type d -name 'W_*' 2>/dev/null | wc -l | tr -d ' ')
  echo "[status] OUT_BASE=$OUT_BASE"
  echo "[status] completed W_ dirs: $done_n/${#WORKLOADS[@]}"
  find "$OUT_BASE" -maxdepth 1 -type d -name 'W_*' 2>/dev/null | sort | sed 's#^.*/#  #'
  echo
  echo "[status] running collect/gem5 processes:"
  ps -eo pid,etime,cmd | grep -E '[g]em5\.opt|[c]ollect_parallel_500k|[c]ollect_v7_c08_parallel' || true
  echo
  echo "[status] recent debug/final dirs:"
  find "$OUT_BASE" -maxdepth 1 -type d \( -name '_debug_*' -o -name '_tmp_*' -o -name 'probe_*' \) 2>/dev/null | sort | tail -30 | sed 's#^.*/#  #'
}

verify_counts() {
  local fail=0
  local wl d counts stats min_rec max_rec max_abs_delta ncores max_allowed
  max_allowed=$(( MAX_ACCEPT_PER_CORE * (100 + MAX_ACCEPT_SLACK_PCT) / 100 ))
  for wl in "${WORKLOADS[@]}"; do
    d="$OUT_BASE/W_$wl"
    counts="$d/counts.txt"
    if [[ ! -f "$counts" ]]; then
      echo "[verify][fail] W_$wl missing counts.txt"
      fail=1
      continue
    fi
    stats=$(awk '
      BEGIN { seen=0; min=0; max=0; maxdiff=0 }
      /core[0-9]+:/ {
        rec=-1; lab=-1
        for (i=1; i<=NF; i++) {
          if ($i ~ /^rec=/) { sub(/^rec=/, "", $i); rec=$i + 0 }
          if ($i ~ /^lab=/) { sub(/^lab=/, "", $i); lab=$i + 0 }
        }
        diff=rec-lab
        if (diff < 0) { diff=-diff }
        if (diff > maxdiff) { maxdiff=diff }
        if (seen == 0 || rec < min) { min=rec }
        if (seen == 0 || rec > max) { max=rec }
        seen++
      }
      END {
        if (seen == 0) { exit 2 }
        printf "%d %d %d %d\n", min, max, maxdiff, seen
      }' "$counts") || {
        echo "[verify][fail] W_$wl cannot parse counts.txt"
        fail=1
        continue
      }
    read -r min_rec max_rec max_abs_delta ncores <<<"$stats"
    if (( ncores != NUM_CORES || max_abs_delta > REC_LAB_DELTA_MAX || min_rec < MIN_ACCEPT_PER_CORE || max_rec > max_allowed )); then
      echo "[verify][fail] W_$wl cores=$ncores min=$min_rec max=$max_rec max_abs_delta=$max_abs_delta max_allowed=$max_allowed"
      fail=1
    else
      echo "[verify][ok]   W_$wl cores=$ncores min=$min_rec max=$max_rec max_abs_delta=$max_abs_delta"
    fi
  done

  if (( fail != 0 )); then
    echo "[verify] failed"
    return 1
  fi
  echo "[verify] all workloads passed"
}

run_build() {
  verify_counts
  mkdir -p "$WINDOW_OUT"
  rm -rf "$WINDOW_OUT"
  mkdir -p "$WINDOW_OUT"

  local -a wnames
  mapfile -t wnames < <(prefixed_workloads)

  print_config
  echo "[build] start: $(date '+%F %T')"
  "$PY" "$ROOT/data/build_windows.py" \
    --raw "$OUT_BASE" \
    --out "$WINDOW_OUT" \
    --jobs "$BUILD_JOBS" \
    --tq-max-len "$MAXLEN" \
    --tq-target-windows "$TQ_TARGET_WINDOWS" \
    --workloads "${wnames[@]}"

  "$PY" - "$WINDOW_OUT/windows.jsonl" <<'PYEOF'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists():
    raise SystemExit(f"[schema][fail] missing {path}")

n = 0
per = {}
lo = None
hi = None
expected_label_keys = [
    "cpi_uop",
    "branch_miss",
    "l1d_ld_miss",
    "l1d_st_miss",
    "l1i_miss",
    "llc_miss",
    "dtlb_miss",
    "mshr_avg",
]
required_fields = {"uops_per_core", "cpi_macro_per_core"}
with path.open() as f:
    for line in f:
        if not line.startswith("{"):
            continue
        rec = json.loads(line)
        n += 1
        per[rec.get("workload", "unknown")] = per.get(rec.get("workload", "unknown"), 0) + 1
        label_keys = rec.get("label_keys") or []
        if label_keys != expected_label_keys:
            raise SystemExit(f"[schema][fail] bad label_keys={label_keys}")
        missing = required_fields - set(rec)
        if missing:
            raise SystemExit(f"[schema][fail] missing fields={sorted(missing)}")
        labels = rec.get("label", rec.get("labels"))
        if not labels or not labels[0]:
            raise SystemExit("[schema][fail] empty label matrix")
        v = float(labels[0][0])
        if not (0.05 <= v <= 100.0):
            raise SystemExit(f"[schema][fail] cpi_uop out of range: {v}")
        lo = v if lo is None else min(lo, v)
        hi = v if hi is None else max(hi, v)

if n == 0:
    raise SystemExit("[schema][fail] empty windows.jsonl")

print(f"[schema][ok] total={n} cpi_uop_range=[{lo:.4f},{hi:.4f}]")
for name in sorted(per):
    print(f"[schema][ok] {name}: samples={per[name]}")
PYEOF

  if [[ "$PREPARE_CACHE" == "1" ]]; then
    "$PY" "$ROOT/scripts/prepare_dataset_cache.py" \
      --data "$WINDOW_OUT/windows.jsonl" \
      --max-len "$MAXLEN" \
      --jobs "$BUILD_JOBS"
  fi

  echo "[build] done: $WINDOW_OUT/windows.jsonl"
}

case "$ACTION" in
  collect)
    run_collect
    ;;
  status)
    show_status
    ;;
  verify)
    verify_counts
    ;;
  build)
    run_build
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
