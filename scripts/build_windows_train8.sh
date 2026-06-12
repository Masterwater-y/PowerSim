#!/usr/bin/env bash
set -euo pipefail

PY=/data00/yinhaolang/infer/.venv/bin/python
ROOT=/data00/yinhaolang/LLMSim
cd "$ROOT"

RAW_MERGED=${RAW_MERGED:-$ROOT/data/raw_train8}
WINDOW_OUT=${WINDOW_OUT:-$ROOT/data/windows_train8_w512}

WINDOW=${WINDOW:-512}
STRIDE=${STRIDE:-256}
JOBS=${JOBS:-8}
MAXLEN=${MAXLEN:-32768}

RELINK_RAW=${RELINK_RAW:-1}
REBUILD_WINDOWS=${REBUILD_WINDOWS:-1}
PREPARE_CACHE=${PREPARE_CACHE:-1}

WORKLOADS=(
  W_branch_storm
  W_chase_dram
  W_compute_int
  W_false_sharing
  W_indirect
  W_int_div
  W_phased_mix
  W_stream
)

echo "[prep] RAW_MERGED=$RAW_MERGED"
echo "[prep] WINDOW_OUT=$WINDOW_OUT"
echo "[prep] WINDOW=$WINDOW STRIDE=$STRIDE JOBS=$JOBS MAXLEN=$MAXLEN"
echo "[prep] RELINK_RAW=$RELINK_RAW REBUILD_WINDOWS=$REBUILD_WINDOWS PREPARE_CACHE=$PREPARE_CACHE"

mkdir -p "$RAW_MERGED" "$WINDOW_OUT" logs

if [[ "$RELINK_RAW" == "1" ]]; then
  rm -rf "$RAW_MERGED"
  mkdir -p "$RAW_MERGED"

  ln -sfn "$ROOT/data/raw_fix3_8c_500k/W_branch_storm"  "$RAW_MERGED/W_branch_storm"
  ln -sfn "$ROOT/data/raw_8w_8c_500k/W_chase_dram"      "$RAW_MERGED/W_chase_dram"
  ln -sfn "$ROOT/data/raw_8w_8c_500k/W_compute_int"     "$RAW_MERGED/W_compute_int"
  ln -sfn "$ROOT/data/raw_8w_8c_500k/W_false_sharing"   "$RAW_MERGED/W_false_sharing"
  ln -sfn "$ROOT/data/raw_8w_8c_500k/W_indirect"        "$RAW_MERGED/W_indirect"
  ln -sfn "$ROOT/data/raw_fix3_8c_500k/W_int_div"       "$RAW_MERGED/W_int_div"
  ln -sfn "$ROOT/data/raw_fix3_8c_500k/W_phased_mix"    "$RAW_MERGED/W_phased_mix"
  ln -sfn "$ROOT/data/raw_8w_8c_500k/W_stream"          "$RAW_MERGED/W_stream"
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

if [[ "$PREPARE_CACHE" == "1" ]]; then
  "$PY" scripts/prepare_dataset_cache.py \
    --data "$WINDOW_OUT/windows.jsonl" \
    --max-len "$MAXLEN" \
    --jobs "$JOBS"
else
  echo "[prep] skip dataset cache build"
fi

"$PY" - <<'PYEOF' "$WINDOW_OUT/windows.jsonl"
import json
import sys
from pathlib import Path

jsonl = Path(sys.argv[1])
if not jsonl.exists():
    raise SystemExit(f"[sanity] missing dataset: {jsonl}")

total = 0
per_workload = {}
for line in jsonl.open():
    s = line.strip()
    if not s.startswith("{"):
        continue
    rec = json.loads(s)
    total += 1
    wname = rec.get("workload", "unknown")
    per_workload[wname] = per_workload.get(wname, 0) + 1

if total == 0:
    raise SystemExit("[sanity] windows.jsonl is empty")

print(f"[sanity] total_samples={total}")
for wname in sorted(per_workload):
    print(f"[sanity] {wname}: samples={per_workload[wname]}")
PYEOF

echo "[done] windows dataset ready: $WINDOW_OUT/windows.jsonl"
