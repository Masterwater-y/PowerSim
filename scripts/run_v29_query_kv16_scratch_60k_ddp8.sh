#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
cd "$ROOT"

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
MANIFEST=${MANIFEST:-data/v29_global_time_dataset/manifest.json}
CONFIG=${CONFIG:-configs/v29_query_kv16_scratch_60k.yaml}
OUT=${OUT:-ckpt/tcsim_v29_query_kv16_scratch_100m_8gpu_60k}
STEPS=${STEPS:-60000}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
NPROC=${NPROC:-8}

[[ -x "$PY" ]] || { echo "[query-kv16][ERROR] missing Python: $PY" >&2; exit 2; }
[[ -f "$MANIFEST" ]] || { echo "[query-kv16][ERROR] missing manifest: $MANIFEST" >&2; exit 2; }
[[ -f "$CONFIG" ]] || { echo "[query-kv16][ERROR] missing config: $CONFIG" >&2; exit 2; }
if [[ ! "$STEPS" =~ ^[0-9]+$ ]] || (( STEPS < 1 || STEPS > 60000 )); then
  echo "[query-kv16][ERROR] STEPS must be in [1,60000], got $STEPS" >&2
  exit 2
fi
if [[ -z "${RESUME_CKPT:-}" && -e "$OUT/last.pt" ]]; then
  echo "[query-kv16][ERROR] refusing to overwrite existing run: $OUT" >&2
  echo "[query-kv16][ERROR] set RESUME_CKPT=$OUT/last.pt or choose another OUT" >&2
  exit 2
fi
if [[ -n "${RESUME_CKPT:-}" && ! -f "$RESUME_CKPT" ]]; then
  echo "[query-kv16][ERROR] missing resume checkpoint: $RESUME_CKPT" >&2
  exit 2
fi

"$PY" scripts/audit_v29_query_kv_train_cache.py --manifest "$MANIFEST"
echo "[query-kv16] cache=$MANIFEST backend=query_preserving_kv layers=4,8 anchors=8+8"
echo "[query-kv16] target=$STEPS milestone=30000 output=$OUT"

RESUME_CKPT="${RESUME_CKPT:-}" \
INIT_CHECKPOINT= \
MANIFEST="$MANIFEST" CONFIG="$CONFIG" OUT="$OUT" STEPS="$STEPS" \
GPUS="$GPUS" NPROC="$NPROC" PROFILE_ATTENTION=0 \
  bash scripts/run_v29_ddp8.sh
