#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
cd "$ROOT"
mkdir -p logs

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
MANIFEST=${MANIFEST:-data/v29_global_time_dataset/manifest.json}
CONFIG=${CONFIG:-configs/v29_query_kv16_scratch_60k.yaml}
OUT=${OUT:-ckpt/tcsim_v29_query_kv16_scratch_100m_8gpu_60k}
STEPS=${STEPS:-60000}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
NPROC=${NPROC:-8}
RESUME_CKPT=${RESUME_CKPT:-}

[[ -x "$PY" ]] || { echo "[query-kv16][ERROR] missing Python: $PY" >&2; exit 2; }
[[ -f "$MANIFEST" ]] || { echo "[query-kv16][ERROR] missing manifest: $MANIFEST" >&2; exit 2; }
[[ -f "$CONFIG" ]] || { echo "[query-kv16][ERROR] missing config: $CONFIG" >&2; exit 2; }
if [[ ! "$STEPS" =~ ^[0-9]+$ ]] || (( STEPS < 1 || STEPS > 60000 )); then
  echo "[query-kv16][ERROR] STEPS must be in [1,60000], got $STEPS" >&2
  exit 2
fi
"$PY" scripts/audit_v29_query_kv_train_cache.py --manifest "$MANIFEST"
if [[ -z "$RESUME_CKPT" && -e "$OUT/last.pt" ]]; then
  echo "[query-kv16][ERROR] refusing to overwrite existing run: $OUT" >&2
  exit 2
fi
if [[ -n "$RESUME_CKPT" && ! -f "$RESUME_CKPT" ]]; then
  echo "[query-kv16][ERROR] missing resume checkpoint: $RESUME_CKPT" >&2
  exit 2
fi

STAMP=$(date +%Y%m%d_%H%M%S)
LOG=${LOG:-logs/v29_query_kv16_scratch_60k_${STAMP}.nohup.log}
PID_FILE=${PID_FILE:-${LOG}.pid}

nohup env \
  ROOT="$ROOT" PY="$PY" MANIFEST="$MANIFEST" CONFIG="$CONFIG" \
  OUT="$OUT" STEPS="$STEPS" GPUS="$GPUS" NPROC="$NPROC" \
  RESUME_CKPT="$RESUME_CKPT" \
  bash scripts/run_v29_query_kv16_scratch_60k_ddp8.sh >"$LOG" 2>&1 &

PID=$!
printf '%s\n' "$PID" >"$PID_FILE"
echo "[query-kv16] pid=$PID"
echo "[query-kv16] cache=$MANIFEST"
echo "[query-kv16] output=$OUT"
echo "[query-kv16] log=$LOG"
echo "[query-kv16] milestone=$OUT/step_30000.pt"
echo "[query-kv16] follow: tail -f $LOG"
