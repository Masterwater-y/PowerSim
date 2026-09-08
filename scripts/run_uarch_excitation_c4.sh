#!/usr/bin/env bash
# One command for build -> functional traces -> matching gem5 labels ->
# FastSim replay -> generalization report.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
FASTSIM_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
DATASET_OUT=${FASTSIM_UARCH_EXCITATION_OUT:-$FASTSIM_ROOT/tmp/uarch-c4-excitation-first-batch}
MATRIX=$FASTSIM_ROOT/configs/workloads/uarch_excitation.json

"$FASTSIM_ROOT/scripts/collect_uarch_excitation_c4.sh" "$@"

for arg in "$@"; do
  if [[ "$arg" == "--dry-run" || "$arg" == "--list" ]]; then
    exit 0
  fi
done

FASTSIM_UARCH_DATASET_OUT="$DATASET_OUT" \
FASTSIM_UARCH_MATRIX="$MATRIX" \
  "$FASTSIM_ROOT/scripts/run_uarch_c4_fastsim_validation.sh"
