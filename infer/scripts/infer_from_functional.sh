#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON:-$(command -v python3.11 || command -v python3)}"

TRACE_DIR=""
FUNCTIONAL_DIR=""
LABELS_DIR=""
UARCH_PROFILE=""
STATS=""
CKPT=""
OUT_DIR=""
MODE="label"
FORMAT="parquet"
REF_SIM_BACKEND="${REF_SIM_BACKEND:-coordinator}"
QUANTUM_CYCLES="${QUANTUM_CYCLES:-256}"
K_MAX="${K_MAX:-32}"
MODEL_BATCH_SIZE="${MODEL_BATCH_SIZE:-8}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --trace-dir) TRACE_DIR="$2"; shift 2 ;;
    --functional-dir) FUNCTIONAL_DIR="$2"; shift 2 ;;
    --labels-dir) LABELS_DIR="$2"; shift 2 ;;
    --uarch-profile) UARCH_PROFILE="$2"; shift 2 ;;
    --stats) STATS="$2"; shift 2 ;;
    --ckpt) CKPT="$2"; shift 2 ;;
    --out-dir) OUT_DIR="$2"; shift 2 ;;
    --mode) MODE="$2"; shift 2 ;; # label | mock | ckpt
    --format) FORMAT="$2"; shift 2 ;; # parquet | jsonl
    --ref-sim-backend) REF_SIM_BACKEND="$2"; shift 2 ;;
    --quantum-cycles) QUANTUM_CYCLES="$2"; shift 2 ;;
    --k-max) K_MAX="$2"; shift 2 ;;
    --model-batch-size) MODEL_BATCH_SIZE="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$UARCH_PROFILE" ]]; then
  echo "usage: $0 --uarch-profile <uarch_profile.json> [--trace-dir tao_trace] [--functional-dir dir] [--labels-dir dir] [--stats stats.txt] [--ckpt ckpt.pt] [--mode label|mock|ckpt] [--format parquet|jsonl] [--out-dir dir] [--ref-sim-backend coordinator|legacy|timing-functional] [--quantum-cycles n] [--k-max n] [--model-batch-size n]" >&2
  exit 2
fi

OUT_DIR="${OUT_DIR:-$ROOT/out_infer_$(date +%Y%m%d_%H%M%S)}"
FUNCTIONAL_DIR="${FUNCTIONAL_DIR:-$OUT_DIR/functional}"
LABELS_DIR="${LABELS_DIR:-$OUT_DIR/labels}"
mkdir -p "$OUT_DIR"

if [[ ! -d "$FUNCTIONAL_DIR" || -z "$(find "$FUNCTIONAL_DIR" -name 'functional.core*.*' -print -quit 2>/dev/null)" ]]; then
  if [[ -z "$TRACE_DIR" ]]; then
    echo "[infer][FATAL] --trace-dir is required when functional traces do not already exist" >&2
    exit 2
  fi
  EXTRACT_ARGS=(
    --trace-dir "$TRACE_DIR"
    --out-dir "$FUNCTIONAL_DIR"
    --format "$FORMAT"
  )
  if [[ "$MODE" == "label" ]]; then
    EXTRACT_ARGS+=(--labels-trace-dir "$TRACE_DIR" --labels-out-dir "$LABELS_DIR")
  fi
  "$PYTHON_BIN" "$ROOT/functional_trace/extract_from_records.py" "${EXTRACT_ARGS[@]}"
fi

DRIVER_ARGS=(
  --functional-dir "$FUNCTIONAL_DIR"
  --uarch-profile "$UARCH_PROFILE"
  --ref-sim-module-dir "$ROOT/mesi_ref_sim/build"
  --ref-sim-backend "$REF_SIM_BACKEND"
  --out-jsonl "$OUT_DIR/infer.jsonl"
  --report-json "$OUT_DIR/report.json"
  --quantum-cycles "$QUANTUM_CYCLES"
  --k-max "$K_MAX"
  --model-batch-size "$MODEL_BATCH_SIZE"
)
if [[ -n "$STATS" ]]; then
  DRIVER_ARGS+=(--stats "$STATS")
fi
case "$MODE" in
  label)
    if [[ ! -d "$LABELS_DIR" || -z "$(find "$LABELS_DIR" -name 'labels.core*.*' -print -quit 2>/dev/null)" ]]; then
      echo "[infer][FATAL] label mode requires labels.core* files; provide --labels-dir or --trace-dir" >&2
      exit 2
    fi
    DRIVER_ARGS+=(--label-driven --labels-dir "$LABELS_DIR")
    ;;
  mock)
    DRIVER_ARGS+=(--mock-model)
    ;;
  ckpt)
    if [[ -z "$CKPT" ]]; then
      echo "[infer][FATAL] ckpt mode requires --ckpt" >&2
      exit 2
    fi
    DRIVER_ARGS+=(--ckpt "$CKPT")
    ;;
  *) echo "[infer][FATAL] unknown mode: $MODE" >&2; exit 2 ;;
esac

"$PYTHON_BIN" "$ROOT/driver/inference_driver.py" "${DRIVER_ARGS[@]}"
echo "[infer] outputs under $OUT_DIR"
