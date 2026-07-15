#!/usr/bin/env bash
# Resume only the remaining v27.0-cold16 ranking-mix cube after seed0/c01.
# The delegated launcher collects seed0(all 16 train + 2 heldout), then
# seed1(train 16), with core-count groups serial and workloads parallel.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
RUNNER="$ROOT/scripts/tmp/run_v27_0_cold16_serial_cores_collect.sh"

if pgrep -f '[r]un_v27_0_cold16_serial_cores_collect\.sh' >/dev/null; then
  echo "[resume] a v27.0-cold16 collection is already running; do not start a duplicate." >&2
  exit 1
fi

exec env \
  CORES_LIST="${CORES_LIST:-4 8 16 32}" \
  COLLECT_PARALLEL="${COLLECT_PARALLEL:-18}" \
  CONVERT_PARALLEL="${CONVERT_PARALLEL:-18}" \
  "$RUNNER"
