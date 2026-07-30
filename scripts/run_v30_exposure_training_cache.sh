#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
cd "$ROOT"

PYTHON=${PYTHON:-/data00/yinhaolang/infer/.venv/bin/python}
WORKERS=${WORKERS:-128}
SPLITS=${SPLITS:-train,validation}
CORE_COUNTS=${CORE_COUNTS:-1,4,8,16,32}
BASE_MANIFEST=${BASE_MANIFEST:-data/v29_global_time_dataset/manifest.json}
GSS_ROOT=${GSS_ROOT:-data/v30_gss_commit_sidecars}
GSS_MANIFEST=${GSS_MANIFEST:-data/v30_gss_commit_dataset/manifest.json}
EXPOSURE_ROOT=${EXPOSURE_ROOT:-data/v30_exposure_v1_sidecars}
FINAL_MANIFEST=${FINAL_MANIFEST:-data/v30_exposure_v1_dataset/manifest.json}

[[ -x "$PYTHON" ]] || { echo "[v30-cache][ERROR] python not executable: $PYTHON" >&2; exit 2; }
[[ -f "$BASE_MANIFEST" ]] || { echo "[v30-cache][ERROR] missing manifest: $BASE_MANIFEST" >&2; exit 2; }
(( WORKERS > 0 )) || { echo "[v30-cache][ERROR] WORKERS must be positive" >&2; exit 2; }

echo "[v30-cache] phase=1/2 commit-clock GSS workers=$WORKERS splits=$SPLITS"
"$PYTHON" scripts/build_v30_gss_sidecar.py \
  --manifest "$BASE_MANIFEST" \
  --splits "$SPLITS" \
  --core-counts "$CORE_COUNTS" \
  --clock commit \
  --workers "$WORKERS" \
  --out-root "$GSS_ROOT" \
  --write-manifest "$GSS_MANIFEST"

echo "[v30-cache] phase=2/2 functional Exposure-v1 workers=$WORKERS splits=$SPLITS"
"$PYTHON" scripts/build_v30_exposure_sidecar.py \
  --manifest "$GSS_MANIFEST" \
  --splits "$SPLITS" \
  --core-counts "$CORE_COUNTS" \
  --workers "$WORKERS" \
  --out-root "$EXPOSURE_ROOT" \
  --write-manifest "$FINAL_MANIFEST"

echo "[v30-cache] complete"
echo "[v30-cache] training_manifest=$FINAL_MANIFEST"
echo "[v30-cache] contracts=commit-clock-GSS+functional-Exposure-v1"
