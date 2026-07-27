#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
cd "$ROOT"

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
BASE_MANIFEST=${BASE_MANIFEST:-data/v29_global_time_dataset/manifest.json}
OUT=${OUT:-data/v29_long_history_dataset}
SPLITS=${SPLITS:-train,validation}
WORKERS=${WORKERS:-16}

exec "$PY" scripts/build_v29_long_history_cache.py \
  --manifest "$BASE_MANIFEST" \
  --out "$OUT" \
  --splits "$SPLITS" \
  --workers "$WORKERS" \
  "$@"
