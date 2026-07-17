#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT=${ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
cd "$PROJECT_ROOT"

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
CKPT=${CKPT:-$PROJECT_ROOT/ckpt/tcsim_v29_packed3_100m_8gpu_60k/best.pt}
MANIFEST=${MANIFEST:-$PROJECT_ROOT/data/v29_global_time_dataset/manifest.json}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
SPLITS=${SPLITS:-seed0_inference,development_heldout,deployment_inference}
CORE_COUNTS=${CORE_COUNTS:-4,8,16,32}
TARGET_STRIDE=${TARGET_STRIDE:-256}
MAX_STEP_CYCLES=${MAX_STEP_CYCLES:-1024}
RUN_TAG=${RUN_TAG:-v29_packed3_free_s256_seed0_seed1_c04_c08_c16_c32_full}
OUT_DIR=${OUT:-$PROJECT_ROOT/logs/$RUN_TAG}
LAUNCH_LOG=${LAUNCH_LOG:-$OUT_DIR/launcher.log}
PID_FILE=${PID_FILE:-$PROJECT_ROOT/scripts/tmp/$RUN_TAG.pid}

[[ -x "$PY" ]] || { echo "[v29-full-free][ERROR] missing Python: $PY" >&2; exit 2; }
[[ -f "$CKPT" ]] || { echo "[v29-full-free][ERROR] missing checkpoint: $CKPT" >&2; exit 2; }
[[ -f "$MANIFEST" ]] || { echo "[v29-full-free][ERROR] missing manifest: $MANIFEST" >&2; exit 2; }

if [[ -f "$PID_FILE" ]]; then
  existing_pid=$(<"$PID_FILE")
  if [[ "$existing_pid" =~ ^[0-9]+$ ]] && kill -0 "$existing_pid" 2>/dev/null; then
    echo "[v29-full-free][ERROR] evaluation is already running: pid=$existing_pid" >&2
    echo "[v29-full-free] log=$LAUNCH_LOG" >&2
    exit 2
  fi
fi

mkdir -p "$OUT_DIR" "$PROJECT_ROOT/scripts/tmp"
printf '\n[%s] launch mode=free splits=%s cores=%s stride=%s max_step=%s\n' \
  "$(date '+%Y-%m-%d %H:%M:%S')" "$SPLITS" "$CORE_COUNTS" \
  "$TARGET_STRIDE" "$MAX_STEP_CYCLES" >>"$LAUNCH_LOG"

nohup env \
  ROOT="$PROJECT_ROOT" \
  PY="$PY" \
  CKPT="$CKPT" \
  MANIFEST="$MANIFEST" \
  OUT="$OUT_DIR" \
  GPUS="$GPUS" \
  SPLITS="$SPLITS" \
  MODE=free \
  CORE_COUNTS="$CORE_COUNTS" \
  MAX_ORACLE_SAMPLES=0 \
  MAX_FREE_STEPS=0 \
  TARGET_STRIDE="$TARGET_STRIDE" \
  MIN_STEP_CYCLES=4 \
  MAX_STEP_CYCLES="$MAX_STEP_CYCLES" \
  MAX_NO_PROGRESS_STEPS=64 \
  AMP_DTYPE=bf16 \
  SDPA_BACKEND=auto \
  PROGRESS_EVERY=100 \
  ORACLE_DRIFT_DIAGNOSTICS=0 \
  RESUME=1 \
  bash scripts/run_v29_eval_8gpu.sh \
  >>"$LAUNCH_LOG" 2>&1 &

launcher_pid=$!
printf '%s\n' "$launcher_pid" >"$PID_FILE"

printf '[v29-full-free] pid=%s\n' "$launcher_pid"
printf '[v29-full-free] traces=184 (seed0=92 seed1=92) cores=%s\n' "$CORE_COUNTS"
printf '[v29-full-free] mode=free oracle=off drift=off target_stride=%s max_step_cycles=%s\n' \
  "$TARGET_STRIDE" "$MAX_STEP_CYCLES"
printf '[v29-full-free] out=%s\n' "$OUT_DIR"
printf '[v29-full-free] log=%s\n' "$LAUNCH_LOG"
printf '[v29-full-free] report=%s/report.txt\n' "$OUT_DIR"
printf '[v29-full-free] monitor: tail -f %q\n' "$LAUNCH_LOG"
