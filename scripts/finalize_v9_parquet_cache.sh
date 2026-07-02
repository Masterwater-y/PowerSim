#!/usr/bin/env bash
set -euo pipefail

cd /data00/yinhaolang/LLMSim

PY=/data00/yinhaolang/infer/.venv/bin/python
MAX_LEN=32768
JOBS="${JOBS:-17}"

C16_DIR=data/windows_v9_tq_train600_c16
ALL_DIR=data/windows_v9_tq_train600_all
ALL_CACHE_LOG=logs/cache_v9_600_parquet_all.log

echo "[start] $(date '+%F %T') jobs=${JOBS}"

for d in \
  data/windows_v9_tq_train600_c01 \
  data/windows_v9_tq_train600_c04 \
  data/windows_v9_tq_train600_c08 \
  "$C16_DIR"; do
  if [[ ! -d "$d" ]]; then
    echo "[error] missing directory: $d" >&2
    exit 1
  fi
done

for part in \
  data/windows_v9_tq_train600_c01/windows.jsonl \
  data/windows_v9_tq_train600_c04/windows.jsonl \
  data/windows_v9_tq_train600_c08/windows.jsonl; do
  if [[ ! -s "$part" ]]; then
    echo "[error] missing or empty windows file: $part" >&2
    exit 1
  fi
done

if [[ ! -d "$C16_DIR/.shards" ]]; then
  echo "[error] missing c16 shard directory: $C16_DIR/.shards" >&2
  exit 1
fi

shard_count="$(find "$C16_DIR/.shards" -maxdepth 1 -type f -name '*.jsonl' | wc -l)"
if [[ "$shard_count" != "17" ]]; then
  echo "[error] expected 17 c16 shards, got $shard_count" >&2
  find "$C16_DIR/.shards" -maxdepth 1 -type f -name '*.jsonl' -printf '%f\n' | sort >&2
  exit 1
fi

echo "[c16] rebuild windows.jsonl from 17 shards"
rm -rf \
  "$C16_DIR/windows.maxlen${MAX_LEN}.ids_cache" \
  "$C16_DIR/windows.maxlen${MAX_LEN}.tensor_cache"
tmp_c16="$C16_DIR/windows.jsonl.tmp"
find "$C16_DIR/.shards" -maxdepth 1 -type f -name '*.jsonl' \
  | sort \
  | xargs cat > "$tmp_c16"
mv "$tmp_c16" "$C16_DIR/windows.jsonl"

c16_n="$(wc -l < "$C16_DIR/windows.jsonl")"
echo "[c16] windows=$c16_n"
if [[ "$c16_n" != "10046" ]]; then
  echo "[warn] expected c16 windows=10046, got $c16_n" >&2
fi

echo "[c16] build cache"
"$PY" scripts/prepare_dataset_cache.py \
  --data "$C16_DIR/windows.jsonl" \
  --max-len "$MAX_LEN" \
  --format tensor \
  --jobs "$JOBS"

echo "[all] merge c01/c04/c08/c16"
rm -rf "$ALL_DIR" "$ALL_CACHE_LOG"
mkdir -p "$ALL_DIR"
cat \
  data/windows_v9_tq_train600_c01/windows.jsonl \
  data/windows_v9_tq_train600_c04/windows.jsonl \
  data/windows_v9_tq_train600_c08/windows.jsonl \
  "$C16_DIR/windows.jsonl" \
  > "$ALL_DIR/windows.jsonl"

all_n="$(wc -l < "$ALL_DIR/windows.jsonl")"
echo "[all] windows=$all_n"
if [[ "$all_n" != "40568" ]]; then
  echo "[warn] expected all windows=40568, got $all_n" >&2
fi

echo "[all] build cache -> $ALL_CACHE_LOG"
"$PY" scripts/prepare_dataset_cache.py \
  --data "$ALL_DIR/windows.jsonl" \
  --max-len "$MAX_LEN" \
  --format tensor \
  --jobs "$JOBS" \
  > "$ALL_CACHE_LOG" 2>&1

if [[ ! -s "$ALL_DIR/windows.maxlen${MAX_LEN}.tensor_cache/manifest.pt" ]]; then
  echo "[error] missing all cache manifest" >&2
  tail -80 "$ALL_CACHE_LOG" >&2 || true
  exit 1
fi

echo "[done] $(date '+%F %T')"
echo "[done] c16 windows=$c16_n"
echo "[done] all windows=$all_n"
echo "[done] all cache=$ALL_DIR/windows.maxlen${MAX_LEN}.tensor_cache"
