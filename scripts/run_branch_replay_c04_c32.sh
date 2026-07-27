#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT=${ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
cd "$PROJECT_ROOT"

if [[ -n "${PY:-}" ]]; then
  PYTHON_BIN=$PY
elif [[ -x /data00/yinhaolang/infer/.venv/bin/python ]]; then
  PYTHON_BIN=/data00/yinhaolang/infer/.venv/bin/python
else
  PYTHON_BIN=python3
fi

MANIFEST=${MANIFEST:-$PROJECT_ROOT/data/v29_global_time_dataset/manifest.json}
SPLITS=${SPLITS:-deployment_inference}
CORE_COUNTS=${CORE_COUNTS:-4,8,16,32}
JOBS=${JOBS:-64}
RUN_STAMP=${RUN_STAMP:-$(date +%Y%m%d_%H%M%S)}
OUT=${OUT:-$PROJECT_ROOT/logs/branch_replay_c04_c32_$RUN_STAMP}

[[ -f "$MANIFEST" ]] || {
  echo "[branch-replay-c04-c32][ERROR] missing manifest: $MANIFEST" >&2
  exit 2
}

args=(
  --manifest "$MANIFEST"
  --splits "$SPLITS"
  --core-counts "$CORE_COUNTS"
  --jobs "$JOBS"
  --out "$OUT"
)
[[ -n "${WORKLOADS:-}" ]] && args+=(--workloads "$WORKLOADS")
[[ -n "${ROLES:-}" ]] && args+=(--roles "$ROLES")
[[ -n "${SEEDS:-}" ]] && args+=(--seeds "$SEEDS")
[[ "${RESUME:-0}" == "1" ]] && args+=(--resume)
[[ "${MAX_TRACES:-0}" != "0" ]] && args+=(--max-traces "$MAX_TRACES")

echo "[branch-replay-c04-c32] CPU-only jobs=$JOBS splits=$SPLITS cores=$CORE_COUNTS"
echo "[branch-replay-c04-c32] out=$OUT"

exec env \
  PYTHONUNBUFFERED=1 \
  OMP_NUM_THREADS=1 \
  OPENBLAS_NUM_THREADS=1 \
  MKL_NUM_THREADS=1 \
  "$PYTHON_BIN" scripts/run_branch_replay_suite.py "${args[@]}"
