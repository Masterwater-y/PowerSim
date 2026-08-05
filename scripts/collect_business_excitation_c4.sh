#!/usr/bin/env bash
# One-click C4 business excitation collection and FastSim validation.
# The 12 functional traces are reusable; only 48 workload/uarch OoO labels are
# collected in the pilot. Q remains fixed by gem5-v28_1-time-epoch.cfg at 1024.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
FASTSIM_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
HOST_PYTHON=${FASTSIM_HOST_PYTHON:-/data00/yinhaolang/infer/.venv/bin/python}
MATRIX=$FASTSIM_ROOT/configs/business-excitation-c4.json
BIN_DIR=$FASTSIM_ROOT/workloads/business_excitation/bin
DATASET_OUT=${FASTSIM_BUSINESS_EXCITATION_OUT:-$FASTSIM_ROOT/tmp/business-excitation-c4}

if [[ ! -x "$HOST_PYTHON" ]]; then
  HOST_PYTHON=python3
fi

make -C "$FASTSIM_ROOT/workloads/business_excitation" -j "${FASTSIM_BUILD_JOBS:-12}"

TRACE_ARGS=(
  "$FASTSIM_ROOT/tools/collect_functional_traces.py"
  --matrix "$MATRIX"
  --bin-dir "$BIN_DIR"
  --out "$DATASET_OUT/traces"
  --cores 4
  --resume-orphans
)
if [[ -n "${FASTSIM_TRACE_JOBS:-}" ]]; then
  TRACE_ARGS+=(--jobs "$FASTSIM_TRACE_JOBS")
fi
if [[ "${FASTSIM_SKIP_TRACE:-0}" != "1" ]]; then
  "$HOST_PYTHON" "${TRACE_ARGS[@]}" "$@"
fi

for arg in "$@"; do
  if [[ "$arg" == "--dry-run" || "$arg" == "--list" ]]; then
    exit 0
  fi
done

if [[ "${FASTSIM_SKIP_LABELS:-0}" != "1" ]]; then
  LABEL_ARGS=(
    "$FASTSIM_ROOT/tools/collect_uarch_stats.py"
    --matrix "$MATRIX"
    --bin-dir "$BIN_DIR"
    --out "$DATASET_OUT/labels"
    --cores 4
    --expected-profiles-only
  )
  if [[ -n "${FASTSIM_UARCH_JOBS:-}" ]]; then
    LABEL_ARGS+=(--jobs "$FASTSIM_UARCH_JOBS")
  fi
  "$HOST_PYTHON" "${LABEL_ARGS[@]}"
fi

if [[ -d "$DATASET_OUT/traces" && -d "$DATASET_OUT/labels" ]]; then
  "$HOST_PYTHON" "$FASTSIM_ROOT/tools/link_uarch_dataset.py" --root "$DATASET_OUT"
fi

if [[ "${FASTSIM_SKIP_REPLAY:-0}" != "1" ]]; then
  REPLAY_ARGS=(
    "$FASTSIM_ROOT/tools/run_uarch_fastsim.py"
    --root "$DATASET_OUT"
    --matrix "$MATRIX"
    --config "$FASTSIM_ROOT/configs/gem5-v28_1-time-epoch.cfg"
    --fastsim "$FASTSIM_ROOT/build/fastsim"
  )
  if [[ -n "${FASTSIM_REPLAY_JOBS:-}" ]]; then
    REPLAY_ARGS+=(--jobs "$FASTSIM_REPLAY_JOBS")
  fi
  "$HOST_PYTHON" "${REPLAY_ARGS[@]}"
  if [[ "${FASTSIM_SKIP_ISOLATED_THROUGHPUT:-0}" != "1" ]]; then
    "$HOST_PYTHON" "${REPLAY_ARGS[@]}" --jobs 1 --force
  fi
  "$HOST_PYTHON" "$FASTSIM_ROOT/tools/evaluate_uarch_generalization.py" \
    --root "$DATASET_OUT"
fi

echo "business excitation complete: $DATASET_OUT"
