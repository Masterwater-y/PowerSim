#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
cd "$ROOT"

WINDOW_OUT=${WINDOW_OUT:-$ROOT/data/windows_v7_c08_tq}
DATA=${DATA:-$WINDOW_OUT/windows.jsonl}
MAXLEN=${MAXLEN:-32768}
BUILD_JOBS=${BUILD_JOBS:-8}
REBUILD_WINDOWS=${REBUILD_WINDOWS:-0}

SMOKE_GPU=${SMOKE_GPU:-0}
SMOKE_STEPS=${SMOKE_STEPS:-2}
SMOKE_OUT=${SMOKE_OUT:-$ROOT/ckpt/v7_c08_absmiss_smoke}

TRAIN_OUT=${TRAIN_OUT:-$ROOT/ckpt/v7_c08_absmiss_ddp8}
STEPS=${STEPS:-3000}
BS=${BS:-1}
GRAD_ACCUM=${GRAD_ACCUM:-2}
LOG_EVERY=${LOG_EVERY:-20}
EVAL_EVERY=${EVAL_EVERY:-200}
EVAL_BATCHES=${EVAL_BATCHES:-20}
VAL_FRAC=${VAL_FRAC:-0.10}
NUM_WORKERS=${NUM_WORKERS:-4}
MASTER_PORT=${MASTER_PORT:-29588}

mkdir -p logs "$SMOKE_OUT" "$TRAIN_OUT"

echo "[1/4] build windows/cache if needed"
if [[ "$REBUILD_WINDOWS" == "1" || ! -f "$DATA" ]]; then
  PREPARE_CACHE=1 \
  WINDOW_OUT="$WINDOW_OUT" \
  MAXLEN="$MAXLEN" \
  BUILD_JOBS="$BUILD_JOBS" \
    bash scripts/collect_v7_c08_parallel.sh build
else
  echo "[build] reuse $DATA"
  "$PY" scripts/prepare_dataset_cache.py \
    --data "$DATA" \
    --max-len "$MAXLEN" \
    --jobs "$BUILD_JOBS"
fi

echo "[2/4] schema smoke"
"$PY" - "$DATA" <<'PYEOF'
import json
import sys
expected = [
    "cpi_uop", "branch_miss", "l1d_ld_miss", "l1d_st_miss",
    "l1i_miss", "llc_miss", "dtlb_miss", "mshr_avg",
]
n = 0
for line in open(sys.argv[1]):
    if not line.startswith("{"):
        continue
    rec = json.loads(line)
    if rec.get("label_keys") != expected:
        raise SystemExit(f"bad label_keys: {rec.get('label_keys')}")
    if len(rec.get("label", [])) != rec.get("n_core"):
        raise SystemExit("label/core mismatch")
    n += 1
if n == 0:
    raise SystemExit("empty windows dataset")
print(f"[schema] ok samples={n}")
PYEOF

echo "[3/4] single-GPU train smoke"
CUDA_VISIBLE_DEVICES="$SMOKE_GPU" \
HF_HUB_OFFLINE=1 TOKENIZERS_PARALLELISM=false \
  "$PY" train/train_lora.py \
    --data "$DATA" \
    --out "$SMOKE_OUT" \
    --steps "$SMOKE_STEPS" \
    --bs 1 \
    --grad-accum 1 \
    --max-len "$MAXLEN" \
    --log-every 1 \
    --eval-every "$SMOKE_STEPS" \
    --eval-batches 1 \
    --val-frac 0.10 \
    --num-workers 0

echo "[4/4] launch 8-GPU training"
DATA="$DATA" \
OUT="$TRAIN_OUT" \
NPROC=8 \
STEPS="$STEPS" \
BS="$BS" \
GRAD_ACCUM="$GRAD_ACCUM" \
MAXLEN="$MAXLEN" \
LOG_EVERY="$LOG_EVERY" \
EVAL_EVERY="$EVAL_EVERY" \
EVAL_BATCHES="$EVAL_BATCHES" \
VAL_FRAC="$VAL_FRAC" \
NUM_WORKERS="$NUM_WORKERS" \
MASTER_PORT="$MASTER_PORT" \
  bash scripts/launch_ddp8.sh
