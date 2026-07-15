#!/usr/bin/env bash
# Collect only the 16 seed1 deployment workloads, align them, and add their
# packed chunks to the existing v28 manifest/cache.  Core counts are serial;
# workloads inside each core-count group run fully in parallel.
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
TSIM_ROOT=${TSIM_ROOT:-/data00/yinhaolang/TSim}
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
WORKERS=${WORKERS:-64}
COLLECT_PARALLEL=${COLLECT_PARALLEL:-16}
CONVERT_PARALLEL=${CONVERT_PARALLEL:-16}
AUDIT_REPORT=${AUDIT_REPORT:-$ROOT/data/v28_business_a1_sharedzipf_seed01_raw_audit.json}

cd "$ROOT"
env \
  TSIM_ROOT="$TSIM_ROOT" \
  PY="$PY" \
  SEEDS=1 \
  MODE=train \
  COLLECT_PARALLEL="$COLLECT_PARALLEL" \
  CONVERT_PARALLEL="$CONVERT_PARALLEL" \
  RUN_AUDIT=1 \
  AUDIT_OUT="$AUDIT_REPORT" \
  DATASET_TAG=v28_business_a1_sharedzipf \
  bash scripts/tmp/run_v28_business_serial_cores_collect.sh

"$PY" scripts/build_v28_dataset.py \
  --out "$ROOT/data/v28_business_a1_sharedzipf_dataset" \
  --config "$ROOT/configs/mvp_100m.yaml" \
  --input-format aligned \
  --audit-report "$AUDIT_REPORT" \
  --train-seeds 0 \
  --deployment-seeds 1 \
  --build \
  --allow-provisional \
  --workers "$WORKERS" \
  --skip-existing

"$PY" - "$ROOT/data/v28_business_a1_sharedzipf_dataset/manifest.json" <<'PY'
import json, sys
path = sys.argv[1]
manifest = json.load(open(path, "r", encoding="utf-8"))
count = len(manifest.get("splits", {}).get("deployment_inference", []))
if count != 80:
    raise SystemExit(f"expected 80 seed1 deployment traces, found {count}: {path}")
print(f"[seed1] deployment_inference={count} manifest={path}")
PY
