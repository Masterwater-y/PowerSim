#!/usr/bin/env bash
# Build threshold-crossing workloads, collect reusable C4 functional traces,
# then collect only the matching gem5 O3 labels instead of a full Cartesian
# workload/profile product.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
FASTSIM_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
HOST_PYTHON=${FASTSIM_HOST_PYTHON:-/data00/yinhaolang/infer/.venv/bin/python}
MATRIX=$FASTSIM_ROOT/configs/workloads/uarch_excitation.json
BIN_DIR=$FASTSIM_ROOT/workloads/uarch_excitation/bin/gem5
DATASET_OUT=${FASTSIM_UARCH_EXCITATION_OUT:-$FASTSIM_ROOT/tmp/uarch-c4-excitation-first-batch}

if [[ ! -x "$HOST_PYTHON" ]]; then
  HOST_PYTHON=python3
fi

make -C "$FASTSIM_ROOT/workloads/uarch_excitation" -j "${FASTSIM_BUILD_JOBS:-12}"

TRACE_ARGS=(
  "$FASTSIM_ROOT/tools/collect_functional_traces.py"
  --matrix "$MATRIX"
  --bin-dir "$BIN_DIR"
  --out "$DATASET_OUT/traces"
  --cores 4
)
if [[ -n "${FASTSIM_TRACE_JOBS:-}" ]]; then
  TRACE_ARGS+=(--jobs "$FASTSIM_TRACE_JOBS")
fi
if [[ "${FASTSIM_SKIP_TRACE:-0}" != "1" ]]; then
  "$HOST_PYTHON" "${TRACE_ARGS[@]}" "$@"
fi

collect_family() {
  local domain=$1
  shift
  local args=(
    "$FASTSIM_ROOT/tools/collect_uarch_stats.py"
    --matrix "$MATRIX"
    --bin-dir "$BIN_DIR"
    --out "$DATASET_OUT/labels"
    --cores 4
    --uarch baseline
  )
  for profile in "$@"; do args+=(--uarch "$profile"); done
  args+=(--workload "uarch_${domain}*")
  if [[ -n "${FASTSIM_UARCH_JOBS:-}" ]]; then
    args+=(--jobs "$FASTSIM_UARCH_JOBS")
  fi
  "$HOST_PYTHON" "${args[@]}"
}

for arg in "$@"; do
  if [[ "$arg" == "--dry-run" || "$arg" == "--list" ]]; then
    exit 0
  fi
done

if [[ "${FASTSIM_SKIP_LABELS:-0}" != "1" ]]; then
  collect_family rob rob96 rob256
  collect_family iq iq32 iq96
  collect_family dtlb dtlb32 dtlb128
  collect_family l1 l1d16k4 l1d64k8
  collect_family l2 l2_512k8 l2_2m8
fi

if [[ -d "$DATASET_OUT/traces" && -d "$DATASET_OUT/labels" ]]; then
  "$HOST_PYTHON" "$FASTSIM_ROOT/tools/link_uarch_dataset.py" --root "$DATASET_OUT"
fi
