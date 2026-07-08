#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TSim}
cd "$ROOT"

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
SRC=${SRC:-data/windows_v26_clean14_tail_local_all/windows.jsonl}
OUT_DIR=${OUT_DIR:-data/windows_v26_clean14_false_sharing_only}
MAX_LEN=${MAX_LEN:-32768}
JOBS=${JOBS:-$(nproc)}
LINES_PER_SHARD=${LINES_PER_SHARD:-512}
OUT_JSONL="$OUT_DIR/windows.jsonl"
CACHE_OUT=${CACHE_OUT:-$OUT_DIR/windows.maxlen${MAX_LEN}.tensor_cache}

mkdir -p "$OUT_DIR"

"$PY" - "$SRC" "$OUT_JSONL" <<'PY'
import json
import sys

src, out = sys.argv[1:3]
n = 0
with open(src) as f, open(out, "w") as g:
    for line in f:
        if not line.lstrip().startswith("{"):
            continue
        if '"workload":"W_false_sharing"' not in line and '"workload": "W_false_sharing"' not in line:
            continue
        obj = json.loads(line)
        if obj.get("workload") != "W_false_sharing":
            continue
        g.write(line if line.endswith("\n") else line + "\n")
        n += 1
print(f"[filter] workload=W_false_sharing windows={n} out={out}", flush=True)
if n == 0:
    raise SystemExit("[filter] no false-sharing windows found")
PY

"$PY" scripts/prepare_dataset_cache.py \
  --data "$OUT_JSONL" \
  --cache-out "$CACHE_OUT" \
  --max-len "$MAX_LEN" \
  --label-keys cpi_uop,branch_miss,l1d_ld_miss,l1d_st_miss,l2_ld_miss,l2_st_miss,llc_miss,dtlb_miss \
  --jobs "$JOBS" \
  --lines-per-shard "$LINES_PER_SHARD"

echo "[ready] data=$OUT_JSONL"
echo "[ready] cache=$CACHE_OUT"
