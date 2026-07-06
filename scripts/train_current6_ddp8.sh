#!/usr/bin/env bash
set -euo pipefail

PY=/data00/yinhaolang/infer/.venv/bin/python
ROOT=/data00/yinhaolang/LLMSim
cd "$ROOT"

RAW_MERGED=${RAW_MERGED:-$ROOT/data/raw_train6}
WINDOW_OUT=${WINDOW_OUT:-$ROOT/data/windows_train6_w512}
TRAIN_OUT=${TRAIN_OUT:-$ROOT/ckpt/train6_w512_ddp8}

WINDOW=${WINDOW:-512}
STRIDE=${STRIDE:-256}
JOBS=${JOBS:-8}
MAXLEN=${MAXLEN:-32768}
RELINK_RAW=${RELINK_RAW:-1}
REBUILD_WINDOWS=${REBUILD_WINDOWS:-0}
PREPARE_CACHE=${PREPARE_CACHE:-1}

NPROC=${NPROC:-8}
STEPS=${STEPS:-2000}
BS=${BS:-1}
LOG_EVERY=${LOG_EVERY:-20}
EVAL_EVERY=${EVAL_EVERY:-50}
EVAL_BATCHES=${EVAL_BATCHES:-0}
VAL_FRAC=${VAL_FRAC:-0.15}
MASTER_PORT=${MASTER_PORT:-29578}

WORKLOADS=(
  W_branch_storm
  W_chase_dram
  W_compute_int
  W_false_sharing
  W_indirect
  W_int_div
)

echo "[prep] RAW_MERGED=$RAW_MERGED"
echo "[prep] WINDOW_OUT=$WINDOW_OUT"
echo "[prep] TRAIN_OUT=$TRAIN_OUT"
echo "[prep] WINDOW=$WINDOW STRIDE=$STRIDE JOBS=$JOBS MAXLEN=$MAXLEN"
echo "[prep] RELINK_RAW=$RELINK_RAW REBUILD_WINDOWS=$REBUILD_WINDOWS PREPARE_CACHE=$PREPARE_CACHE"
echo "[prep] NPROC=$NPROC STEPS=$STEPS BS=$BS MASTER_PORT=$MASTER_PORT"

mkdir -p "$RAW_MERGED" "$WINDOW_OUT" logs

if [[ "$RELINK_RAW" == "1" ]]; then
  rm -rf "$RAW_MERGED"
  mkdir -p "$RAW_MERGED"
  ln -sfn "$ROOT/data/raw_fix3_8c_500k/W_branch_storm"   "$RAW_MERGED/W_branch_storm"
  ln -sfn "$ROOT/data/raw_8w_8c_500k/W_chase_dram"       "$RAW_MERGED/W_chase_dram"
  ln -sfn "$ROOT/data/raw_8w_8c_500k/W_compute_int"      "$RAW_MERGED/W_compute_int"
  ln -sfn "$ROOT/data/raw_8w_8c_500k/W_false_sharing"    "$RAW_MERGED/W_false_sharing"
  ln -sfn "$ROOT/data/raw_8w_8c_500k/W_indirect"         "$RAW_MERGED/W_indirect"
  ln -sfn "$ROOT/data/raw_fix3_8c_500k/W_int_div"        "$RAW_MERGED/W_int_div"
fi

echo "[prep] linked workloads:"
ls -1 "$RAW_MERGED"

if [[ "$REBUILD_WINDOWS" == "1" || ! -f "$WINDOW_OUT/windows.jsonl" ]]; then
  rm -rf "$WINDOW_OUT"
  mkdir -p "$WINDOW_OUT"
  "$PY" data/build_windows.py \
    --raw "$RAW_MERGED" \
    --out "$WINDOW_OUT" \
    --window "$WINDOW" \
    --stride "$STRIDE" \
    --jobs "$JOBS" \
    --workloads "${WORKLOADS[@]}"
else
  echo "[prep] reuse existing windows: $WINDOW_OUT/windows.jsonl"
fi

"$PY" - <<'PYEOF' "$WINDOW_OUT/windows.jsonl" "$MAXLEN"
import json
import sys
from pathlib import Path

jsonl = Path(sys.argv[1])
max_len = int(sys.argv[2])
if not jsonl.exists():
    raise SystemExit(f"[sanity] missing dataset: {jsonl}")

total = 0
max_tokens = 0
for line in jsonl.open():
    s = line.strip()
    if not s.startswith("{"):
        continue
    rec = json.loads(s)
    total += 1
    max_tokens = max(max_tokens, len(rec["tokens"]))

if total == 0:
    raise SystemExit("[sanity] windows.jsonl is empty")
if max_tokens > max_len:
    raise SystemExit(
        f"[sanity] max_tokens={max_tokens} exceeds max_len={max_len}; "
        "reduce WINDOW or increase MAXLEN"
    )

print(f"[sanity] total_samples={total} max_tokens={max_tokens} max_len={max_len}")
PYEOF

if [[ "$PREPARE_CACHE" == "1" ]]; then
  "$PY" scripts/prepare_dataset_cache.py \
    --data "$WINDOW_OUT/windows.jsonl" \
    --max-len "$MAXLEN" \
    --jobs "$JOBS"
else
  echo "[prep] skip dataset cache build"
fi

env \
  DATA="$WINDOW_OUT/windows.jsonl" \
  OUT="$TRAIN_OUT" \
  NPROC="$NPROC" \
  STEPS="$STEPS" \
  BS="$BS" \
  MAXLEN="$MAXLEN" \
  LOG_EVERY="$LOG_EVERY" \
  EVAL_EVERY="$EVAL_EVERY" \
  EVAL_BATCHES="$EVAL_BATCHES" \
  VAL_FRAC="$VAL_FRAC" \
  MASTER_PORT="$MASTER_PORT" \
  bash "$ROOT/scripts/launch_ddp8.sh"
