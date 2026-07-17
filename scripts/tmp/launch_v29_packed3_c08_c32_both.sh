#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT=${ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
cd "$PROJECT_ROOT"

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
CKPT=${CKPT:-$PROJECT_ROOT/ckpt/tcsim_v29_packed3_100m_8gpu_60k/best.pt}
MANIFEST=${MANIFEST:-$PROJECT_ROOT/data/v29_global_time_dataset/manifest.json}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
SPLITS=${SPLITS:-seed0_inference,development_heldout}
RUN_STAMP=$(date +%Y%m%d_%H%M%S)
OUT_DIR=${OUT:-$PROJECT_ROOT/logs/v29_packed3_seed0_c08_c32_full_$RUN_STAMP}
LAUNCH_LOG=${LAUNCH_LOG:-$PROJECT_ROOT/logs/tmp/v29_packed3_seed0_c08_c32_full_$RUN_STAMP.log}
PID_FILE=${PID_FILE:-$PROJECT_ROOT/scripts/tmp/v29_packed3_seed0_c08_c32_full_$RUN_STAMP.pid}

[[ -x "$PY" ]] || { echo "[v29-c08-c32][ERROR] missing Python: $PY" >&2; exit 2; }
[[ -f "$CKPT" ]] || { echo "[v29-c08-c32][ERROR] missing checkpoint: $CKPT" >&2; exit 2; }
[[ -f "$MANIFEST" ]] || { echo "[v29-c08-c32][ERROR] missing manifest: $MANIFEST" >&2; exit 2; }

mkdir -p "$OUT_DIR" "$PROJECT_ROOT/logs/tmp" "$PROJECT_ROOT/scripts/tmp"

nohup env \
  ROOT="$PROJECT_ROOT" \
  PY="$PY" \
  CKPT="$CKPT" \
  MANIFEST="$MANIFEST" \
  OUT="$OUT_DIR" \
  GPUS="$GPUS" \
  SPLITS="$SPLITS" \
  MODE=both \
  CORE_COUNTS=8,32 \
  MAX_ORACLE_SAMPLES=0 \
  MAX_FREE_STEPS=0 \
  TARGET_STRIDE=32 \
  MIN_STEP_CYCLES=4 \
  MAX_STEP_CYCLES=1024 \
  MAX_NO_PROGRESS_STEPS=64 \
  AMP_DTYPE=bf16 \
  SDPA_BACKEND=auto \
  PROGRESS_EVERY=200 \
  RESUME=1 \
  bash scripts/run_v29_eval_8gpu.sh \
  >"$LAUNCH_LOG" 2>&1 &

launcher_pid=$!
printf '%s\n' "$launcher_pid" > "$PID_FILE"

printf '[v29-c08-c32] pid=%s\n' "$launcher_pid"
printf '[v29-c08-c32] out=%s\n' "$OUT_DIR"
printf '[v29-c08-c32] log=%s\n' "$LAUNCH_LOG"
printf '[v29-c08-c32] monitor: tail -f %q\n' "$LAUNCH_LOG"
