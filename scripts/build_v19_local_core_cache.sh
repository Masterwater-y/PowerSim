#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
JOBS=${JOBS:-16}
MAX_LEN=${MAX_LEN:-32768}
LINES_PER_SHARD=${LINES_PER_SHARD:-512}

# MODE:
#   c08       build c08 only, useful for smoke validation
#   all       build merged c01/c04/c08/c16 train cache
#   both      build c08 first, then all
MODE=${MODE:-both}

DATA_C08=${DATA_C08:-data/windows_v17_bc_split_heads_nophase_c08/windows.jsonl}
OUT_C08=${OUT_C08:-data/windows_v17_bc_split_heads_nophase_c08/windows.maxlen${MAX_LEN}.local_core.tensor_cache}

DATA_ALL=${DATA_ALL:-data/windows_v17_bc_split_heads_nophase_all/windows.jsonl}
OUT_ALL=${OUT_ALL:-data/windows_v17_bc_split_heads_nophase_all/windows.maxlen${MAX_LEN}.local_core.tensor_cache}

LOG_DIR=${LOG_DIR:-logs}
TS=${TS:-$(date +%Y%m%d_%H%M%S)}

cd "$ROOT"
mkdir -p "$LOG_DIR"

build_one() {
  local name="$1"
  local data="$2"
  local out="$3"
  local log="$LOG_DIR/build_v19_local_core_cache_${name}_${TS}.log"

  if [[ ! -f "$data" ]]; then
    echo "[cache][error] missing data: $data" >&2
    return 1
  fi

  echo "[cache][$name] data=$data"
  echo "[cache][$name] out=$out"
  echo "[cache][$name] jobs=$JOBS max_len=$MAX_LEN lines_per_shard=$LINES_PER_SHARD"
  echo "[cache][$name] log=$log"

  "$PY" scripts/prepare_dataset_cache.py \
    --data "$data" \
    --max-len "$MAX_LEN" \
    --format tensor \
    --input-mode local_core \
    --jobs "$JOBS" \
    --lines-per-shard "$LINES_PER_SHARD" \
    --cache-out "$out" \
    2>&1 | tee "$log"

  if [[ ! -f "$out/manifest.pt" ]]; then
    echo "[cache][$name][error] manifest missing: $out/manifest.pt" >&2
    return 1
  fi
  echo "[cache][$name] ready: $out"
}

case "$MODE" in
  c08)
    build_one c08 "$DATA_C08" "$OUT_C08"
    ;;
  all)
    build_one all "$DATA_ALL" "$OUT_ALL"
    ;;
  both)
    build_one c08 "$DATA_C08" "$OUT_C08"
    build_one all "$DATA_ALL" "$OUT_ALL"
    ;;
  *)
    echo "[cache][error] unknown MODE=$MODE, expected c08|all|both" >&2
    exit 2
    ;;
esac

echo "[cache] done mode=$MODE"
