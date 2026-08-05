#!/usr/bin/env bash
# One command: reusable C4 functional traces + all-uarch O3 CPI/PMU labels.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
FASTSIM_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
HOST_PYTHON=${FASTSIM_HOST_PYTHON:-/data00/yinhaolang/infer/.venv/bin/python}
DATASET_OUT=${FASTSIM_UARCH_DATASET_OUT:-$FASTSIM_ROOT/tmp/uarch-c4-first-batch}

if [[ ! -x "$HOST_PYTHON" ]]; then
  HOST_PYTHON=python3
fi

TRACE_ARGS=(
  "$FASTSIM_ROOT/tools/collect_functional_traces.py"
  --matrix "$FASTSIM_ROOT/configs/uarch-first-batch.json"
  --out "$DATASET_OUT/traces"
  --cores 4
)
LABEL_ARGS=(
  "$FASTSIM_ROOT/tools/collect_uarch_stats.py"
  --matrix "$FASTSIM_ROOT/configs/uarch-first-batch.json"
  --out "$DATASET_OUT/labels"
  --cores 4
)

if [[ -n "${FASTSIM_TRACE_JOBS:-}" ]]; then
  TRACE_ARGS+=(--jobs "$FASTSIM_TRACE_JOBS")
fi
if [[ -n "${FASTSIM_UARCH_JOBS:-}" ]]; then
  LABEL_ARGS+=(--jobs "$FASTSIM_UARCH_JOBS")
fi
if [[ -n "${FASTSIM_CASE_TIMEOUT:-}" ]]; then
  TRACE_ARGS+=(--timeout "$FASTSIM_CASE_TIMEOUT")
  LABEL_ARGS+=(--timeout "$FASTSIM_CASE_TIMEOUT")
fi

if [[ "${FASTSIM_SKIP_TRACE:-0}" != "1" ]]; then
  "$HOST_PYTHON" "${TRACE_ARGS[@]}" "$@"
fi
if [[ "${FASTSIM_SKIP_LABELS:-0}" != "1" ]]; then
  "$HOST_PYTHON" "${LABEL_ARGS[@]}" "$@"
fi

for arg in "$@"; do
  if [[ "$arg" == "--dry-run" || "$arg" == "--list" ]]; then
    exit 0
  fi
done

if [[ -d "$DATASET_OUT/traces" && -d "$DATASET_OUT/labels" ]]; then
  "$HOST_PYTHON" "$FASTSIM_ROOT/tools/link_uarch_dataset.py" \
    --root "$DATASET_OUT"
fi
