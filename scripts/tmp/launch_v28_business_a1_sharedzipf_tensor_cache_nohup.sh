#!/usr/bin/env bash
# Build the v28 A1 shared-Zipf packed tensor cache in the background.
set -euo pipefail

ROOT=/data00/yinhaolang/TCSim
PYTHON=${PYTHON:-/data00/yinhaolang/infer/.venv/bin/python}
WORKERS=${WORKERS:-64}

cd "$ROOT"
mkdir -p logs/tmp
stamp=$(date +%Y%m%d_%H%M%S)
log="$ROOT/logs/tmp/v28_business_a1_sharedzipf_tensor_cache_${stamp}.log"

nohup "$PYTHON" "$ROOT/scripts/build_v28_dataset.py" \
  --out "$ROOT/data/v28_business_a1_sharedzipf_dataset" \
  --input-format aligned \
  --audit-report "$ROOT/data/v28_business_a1_sharedzipf_seed0_raw_audit.json" \
  --build \
  --allow-provisional \
  --workers "$WORKERS" \
  --skip-existing \
  >"$log" 2>&1 &

pid=$!
echo "pid=$pid"
echo "log=$log"
echo "watch: tail -f $log"
