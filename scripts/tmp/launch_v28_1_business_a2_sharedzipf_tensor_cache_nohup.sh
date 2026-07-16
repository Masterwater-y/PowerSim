#!/usr/bin/env bash
# Build the v28.1 A2 packed tensor cache in a new output directory.
set -euo pipefail

ROOT=/data00/yinhaolang/TCSim
PYTHON=${PYTHON:-/data00/yinhaolang/infer/.venv/bin/python}
WORKERS=${WORKERS:-64}

cd "$ROOT"
mkdir -p logs/tmp
stamp=$(date +%Y%m%d_%H%M%S)
log="$ROOT/logs/tmp/v28_1_business_a2_sharedzipf_tensor_cache_${stamp}.log"

nohup "$PYTHON" "$ROOT/scripts/build_v28_dataset.py" \
  --out "$ROOT/data/v28_1_business_a2_sharedzipf_dataset" \
  --input-format aligned \
  --audit-report "$ROOT/data/v28_1_business_a2_sharedzipf_seed0_raw_audit.json" \
  --build \
  --workers "$WORKERS" \
  --skip-existing \
  >"$log" 2>&1 &

pid=$!
echo "pid=$pid"
echo "cache=$ROOT/data/v28_1_business_a2_sharedzipf_dataset"
echo "log=$log"
echo "watch: tail -f $log"
