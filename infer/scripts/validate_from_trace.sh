#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON:-$(command -v python3.11 || command -v python3)}"
REFSIM="$ROOT/mesi_ref_sim/build/mesi_ref_sim"
GCC11_LIB="${GCC11_LIB:-/opt/gcc-11/lib64}"

if [[ -d "$GCC11_LIB" ]]; then
  export LD_LIBRARY_PATH="$GCC11_LIB:${LD_LIBRARY_PATH:-}"
fi

TRACE_DIR=""
UARCH_PROFILE=""
CKPT=""
OUT_DIR=""
STATS=""
WORKLOAD="holdout"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --trace-dir) TRACE_DIR="$2"; shift 2 ;;
    --uarch-profile) UARCH_PROFILE="$2"; shift 2 ;;
    --ckpt) CKPT="$2"; shift 2 ;;
    --stats) STATS="$2"; shift 2 ;;
    --out-dir) OUT_DIR="$2"; shift 2 ;;
    --workload) WORKLOAD="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$TRACE_DIR" || -z "$UARCH_PROFILE" || -z "$CKPT" || -z "$STATS" ]]; then
  echo "usage: $0 --trace-dir <tao_trace_dir> --uarch-profile <uarch_profile.json> --stats <stats.txt> --ckpt <ckpt.pt> [--out-dir dir] [--workload name]" >&2
  exit 2
fi

test -d "$TRACE_DIR"
test -f "$UARCH_PROFILE"
test -f "$CKPT"
test -f "$STATS"

OUT_DIR="${OUT_DIR:-$ROOT/out_$(date +%Y%m%d_%H%M%S)}"
mkdir -p "$OUT_DIR"

MEM_EVENTS="$OUT_DIR/all_mem_events.merged.jsonl"
PRED_JSONL="$OUT_DIR/pred.jsonl"
INFER_IN="$OUT_DIR/infer_in.jsonl"
MODEL_PRED="$OUT_DIR/model_pred.jsonl"
CPI_JSON="$OUT_DIR/cpi_report.json"

echo "[1/6] derive mem_events ..."
"$PYTHON_BIN" "$ROOT/tools/derive_mem_events.py" \
  --detailed-dir "$TRACE_DIR" \
  --out "$MEM_EVENTS"

echo "[2/6] ref_sim replay ..."
"$REFSIM" "$UARCH_PROFILE" "$MEM_EVENTS" "$PRED_JSONL" \
  2> "$OUT_DIR/refsim.log"

echo "[3/6] bit-exact check ..."
"$PYTHON_BIN" "$ROOT/mesi_ref_sim/scripts/compare_oracle.py" \
  "$MEM_EVENTS" "$PRED_JSONL" > "$OUT_DIR/compare.log" 2>&1 || true
"$PYTHON_BIN" "$ROOT/mesi_ref_sim/scripts/compare_ifetch.py" \
  "$MEM_EVENTS" "$PRED_JSONL" > "$OUT_DIR/compare.ifetch.log" 2>&1 || true

echo "[4/6] build inference input ..."
"$PYTHON_BIN" "$ROOT/tools/build_inference_input.py" \
  --detailed-dir "$TRACE_DIR" \
  --mem-events-jsonl "$MEM_EVENTS" \
  --pred-jsonl "$PRED_JSONL" \
  --workload "$WORKLOAD" \
  --out "$INFER_IN" > "$OUT_DIR/build_infer.log" 2>&1

echo "[5/6] model inference ..."
"$PYTHON_BIN" "$ROOT/ml/infer.py" \
  --ckpt "$CKPT" \
  --input-jsonl "$INFER_IN" \
  --out-jsonl "$MODEL_PRED" 2> "$OUT_DIR/infer.log"

echo "[6/6] report ..."
"$PYTHON_BIN" "$ROOT/tools/compare_pred_vs_truth.py" \
  --pred-jsonl "$MODEL_PRED" \
  --detailed-dir "$TRACE_DIR" > "$OUT_DIR/compare_pred_vs_truth.log" 2>&1
"$PYTHON_BIN" "$ROOT/tools/synthesize_cpi.py" \
  --pred-jsonl "$MODEL_PRED" \
  --input-jsonl "$INFER_IN" \
  --gem5-stats "$STATS" \
  --require-inst-match \
  --out-json "$CPI_JSON" > "$OUT_DIR/cpi_report.log" 2>&1
"$PYTHON_BIN" "$ROOT/mesi_ref_sim/scripts/pmu_report.py" \
  "$MEM_EVENTS" "$PRED_JSONL" "$STATS" \
  --uarch-profile "$UARCH_PROFILE" \
  --model-pred-jsonl "$MODEL_PRED" > "$OUT_DIR/pmu.log" 2>&1 || true

echo "[done] out=$OUT_DIR"
