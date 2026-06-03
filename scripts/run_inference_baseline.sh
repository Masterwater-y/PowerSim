#!/usr/bin/env bash
# Run one no-ROI detailed baseline for deploy-side inference validation.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$(cd "$REPO/.." && pwd)"

GEM5="${GEM5:-$ROOT/gem5/build/X86_MESI_Three_Level/gem5.opt}"
CFG="$REPO/configs/run_mt_mvp.py"
WL="$REPO/workloads"
OUT_BASE="${1:-$REPO/tmp/baseline_w11_no_roi_$(date +%Y%m%d_%H%M%S)}"
NUM_CORES="${NUM_CORES:-4}"
WORKLOAD="${WORKLOAD:-W11_stream_mix}"
GCC11_LIB="${GCC11_LIB:-/opt/gcc-11/lib64}"
PY38_LIB="${PY38_LIB:-/root/.pyenv/versions/3.8.0/lib}"

if [[ -d "$GCC11_LIB" ]]; then
  export LD_LIBRARY_PATH="$GCC11_LIB:${LD_LIBRARY_PATH:-}"
fi
if [[ -d "$PY38_LIB" ]]; then
  export LD_LIBRARY_PATH="$PY38_LIB:${LD_LIBRARY_PATH:-}"
fi

declare -A WL_BIN
declare -A WL_ARGS
WL_BIN[W11_stream_mix]="$WL/mt_stream_mix/mt_stream_mix"
WL_ARGS[W11_stream_mix]="${WL_ARGS_W11:-4 47 256 1 11}"

if [[ -z "${WL_BIN[$WORKLOAD]:-}" ]]; then
  echo "[baseline][FATAL] unsupported workload: $WORKLOAD" >&2
  exit 2
fi

OUT="$OUT_BASE/runs/$WORKLOAD"
mkdir -p "$OUT_BASE/runs"
rm -rf "$OUT"
mkdir -p "$OUT"

echo "[baseline] workload=$WORKLOAD"
echo "[baseline] out=$OUT"
echo "[baseline] ROI is disabled intentionally; this stats.txt is the CPI baseline."

"$GEM5" --outdir="$OUT" "$CFG" \
  --cmd "${WL_BIN[$WORKLOAD]}" \
  --workload-args ${WL_ARGS[$WORKLOAD]} \
  --num-cores "$NUM_CORES" \
  > "$OUT_BASE/$WORKLOAD.gem5.log" 2>&1

echo "[baseline] done: $OUT"
echo "[baseline] verify macro count with:"
echo "  python3 $REPO/tmp/ad_hoc_tools/macro_count_check.py $OUT"
