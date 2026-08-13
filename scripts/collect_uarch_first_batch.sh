#!/usr/bin/env bash
# One-command entrypoint for the first FastSim gem5-SE uarch sweep.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
FASTSIM_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
HOST_PYTHON=${FASTSIM_HOST_PYTHON:-/data00/yinhaolang/infer/.venv/bin/python}

if [[ ! -x "$HOST_PYTHON" ]]; then
  HOST_PYTHON=python3
fi

ARGS=(
  "$FASTSIM_ROOT/tools/collect_uarch_stats.py"
  --matrix "$FASTSIM_ROOT/configs/workloads/uarch_first.json"
  --out "${FASTSIM_UARCH_OUT:-$FASTSIM_ROOT/tmp/uarch-se-first-batch}"
)

if [[ -n "${FASTSIM_UARCH_JOBS:-}" ]]; then
  ARGS+=(--jobs "$FASTSIM_UARCH_JOBS")
fi
if [[ -n "${FASTSIM_CORE_COUNTS:-}" ]]; then
  ARGS+=(--cores "$FASTSIM_CORE_COUNTS")
fi
if [[ -n "${FASTSIM_CASE_TIMEOUT:-}" ]]; then
  ARGS+=(--timeout "$FASTSIM_CASE_TIMEOUT")
fi

exec "$HOST_PYTHON" "${ARGS[@]}" "$@"
