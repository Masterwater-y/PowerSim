#!/usr/bin/env bash
# Resumable v27.0 cold15 CPI + branch-miss packed-cache build.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
PYTHON=${PYTHON:-/data00/yinhaolang/infer/.venv/bin/python}
WORKERS=${WORKERS:-64}

exec "$PYTHON" "$ROOT/scripts/build_v27_dataset.py" \
  --raw-root-glob '/data00/yinhaolang/TSim/data/raw_v27_0_cold16_seed*_c*' \
  --out "$ROOT/data/v27_0_cold15_cpi_brm_v3" \
  --input-format aligned \
  --audit-report "$ROOT/data/v27_0_cold15_raw_audit.json" \
  --build \
  --workers "$WORKERS" \
  --skip-existing
