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
WINDOW_SIZES=${WINDOW_SIZES:-256,1024}
COLD_BRANCHES=${COLD_BRANCHES:-4096}
RUN_STAMP=${RUN_STAMP:-$(date +%Y%m%d_%H%M%S)}
OUT=${OUT:-$PROJECT_ROOT/logs/branch_replay_event_window_seed1_$RUN_STAMP}

args=(
  --manifest "$MANIFEST"
  --splits "$SPLITS"
  --core-counts "$CORE_COUNTS"
  --jobs "$JOBS"
  --window-sizes "$WINDOW_SIZES"
  --cold-branches "$COLD_BRANCHES"
  --out "$OUT"
)
[[ -n "${WORKLOADS:-}" ]] && args+=(--workloads "$WORKLOADS")
[[ -n "${ROLES:-}" ]] && args+=(--roles "$ROLES")
[[ "${RESUME:-0}" == "1" ]] && args+=(--resume)
[[ "${MAX_TRACES:-0}" != "0" ]] && args+=(--max-traces "$MAX_TRACES")

echo "[branch-event-window-audit] CPU-only seed1 jobs=$JOBS cores=$CORE_COUNTS windows=$WINDOW_SIZES"
echo "[branch-event-window-audit] out=$OUT"

exec env \
  PYTHONUNBUFFERED=1 \
  OMP_NUM_THREADS=1 \
  OPENBLAS_NUM_THREADS=1 \
  MKL_NUM_THREADS=1 \
  "$PYTHON_BIN" scripts/audit_branch_replay_event_windows.py "${args[@]}"
