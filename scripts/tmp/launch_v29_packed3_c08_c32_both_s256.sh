#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT=${ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
cd "$PROJECT_ROOT"

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
CKPT=${CKPT:-$PROJECT_ROOT/ckpt/tcsim_v29_packed3_100m_8gpu_60k/best.pt}
MANIFEST=${MANIFEST:-$PROJECT_ROOT/data/v29_global_time_dataset/manifest.json}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
SPLITS=${SPLITS:-seed0_inference,development_heldout}
TARGET_STRIDE=${TARGET_STRIDE:-256}
MAX_STEP_CYCLES=${MAX_STEP_CYCLES:-1024}
RUN_STAMP=$(date +%Y%m%d_%H%M%S)
RUN_TAG=v29_packed3_s${TARGET_STRIDE}_seed0_c08_c32_full_${RUN_STAMP}
OUT_DIR=${OUT:-$PROJECT_ROOT/logs/$RUN_TAG}
LAUNCH_LOG=${LAUNCH_LOG:-$PROJECT_ROOT/logs/tmp/$RUN_TAG.log}
PID_FILE=${PID_FILE:-$PROJECT_ROOT/scripts/tmp/$RUN_TAG.pid}

[[ -x "$PY" ]] || { echo "[v29-c08-c32-s256][ERROR] missing Python: $PY" >&2; exit 2; }
[[ -f "$CKPT" ]] || { echo "[v29-c08-c32-s256][ERROR] missing checkpoint: $CKPT" >&2; exit 2; }
[[ -f "$MANIFEST" ]] || { echo "[v29-c08-c32-s256][ERROR] missing manifest: $MANIFEST" >&2; exit 2; }

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
  TARGET_STRIDE="$TARGET_STRIDE" \
  MIN_STEP_CYCLES=4 \
  MAX_STEP_CYCLES="$MAX_STEP_CYCLES" \
  MAX_NO_PROGRESS_STEPS=64 \
  AMP_DTYPE=bf16 \
  SDPA_BACKEND=auto \
  PROGRESS_EVERY=200 \
  RESUME=1 \
  bash scripts/run_v29_eval_8gpu.sh \
  >"$LAUNCH_LOG" 2>&1 &

launcher_pid=$!
printf '%s\n' "$launcher_pid" > "$PID_FILE"

printf '[v29-c08-c32-s256] pid=%s\n' "$launcher_pid"
printf '[v29-c08-c32-s256] target_stride=%s max_step_cycles=%s\n' "$TARGET_STRIDE" "$MAX_STEP_CYCLES"
printf '[v29-c08-c32-s256] out=%s\n' "$OUT_DIR"
printf '[v29-c08-c32-s256] log=%s\n' "$LAUNCH_LOG"
printf '[v29-c08-c32-s256] monitor: tail -f %q\n' "$LAUNCH_LOG"
