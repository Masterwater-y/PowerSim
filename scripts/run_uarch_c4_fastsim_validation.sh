#!/usr/bin/env bash
# Replay all C4 traces under every uarch and evaluate CPI/PMU generalization.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
FASTSIM_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
HOST_PYTHON=${FASTSIM_HOST_PYTHON:-/data00/yinhaolang/infer/.venv/bin/python}
DATASET_ROOT=${FASTSIM_UARCH_DATASET_OUT:-$FASTSIM_ROOT/tmp/uarch-c4-first-batch}
MATRIX=${FASTSIM_UARCH_MATRIX:-$FASTSIM_ROOT/configs/uarch-first-batch.json}

if [[ ! -x "$HOST_PYTHON" ]]; then
  HOST_PYTHON=python3
fi

ARGS=(
  --root "$DATASET_ROOT"
  --matrix "$MATRIX"
  --config "$FASTSIM_ROOT/configs/gem5-v28_1-time-epoch.cfg"
  --fastsim "$FASTSIM_ROOT/build/fastsim"
)
if [[ -n "${FASTSIM_REPLAY_JOBS:-}" ]]; then
  ARGS+=(--jobs "$FASTSIM_REPLAY_JOBS")
fi
if [[ -n "${FASTSIM_CASE_TIMEOUT:-}" ]]; then
  ARGS+=(--timeout "$FASTSIM_CASE_TIMEOUT")
fi

"$HOST_PYTHON" "$FASTSIM_ROOT/tools/run_uarch_fastsim.py" "${ARGS[@]}" "$@"

for arg in "$@"; do
  if [[ "$arg" == "--dry-run" ]]; then
    exit 0
  fi
done

# Per-case throughput is a wall-clock metric and is depressed by a many-process
# batch. Re-run serially so the >=5M UOP/s gate has an isolated meaning. The
# functional results are deterministic and are validated again on replacement.
if [[ "${FASTSIM_SKIP_ISOLATED_THROUGHPUT:-0}" != "1" ]]; then
  "$HOST_PYTHON" "$FASTSIM_ROOT/tools/run_uarch_fastsim.py" \
    "${ARGS[@]}" --jobs 1 --force "$@"
fi

"$HOST_PYTHON" "$FASTSIM_ROOT/tools/evaluate_uarch_generalization.py" \
  --root "$DATASET_ROOT"
