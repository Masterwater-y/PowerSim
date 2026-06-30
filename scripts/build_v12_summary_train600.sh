#!/usr/bin/env bash
set -euo pipefail

ROOT=/data00/yinhaolang/LLMSim
cd "$ROOT"

export TMPDIR="${TMPDIR:-$ROOT/tmp}"
mkdir -p "$TMPDIR" logs

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
BASE_MODEL=${BASE_MODEL:-Qwen/Qwen3-0.6B-Base}
MAX_LEN=${MAX_LEN:-32768}
CAP=${CAP:-600}
MIN_UOPS=${MIN_UOPS:-256}
TSTART_SOURCE=${TSTART_SOURCE:-teacher_forced}
JOBS=${JOBS:-17}
CLEAN=${CLEAN:-0}

RAW_C01=data/raw_trace_pool/activecore_train/c01_seedA
RAW_C04=data/raw_trace_pool/activecore_train/c04_seedA
RAW_C08=data/raw_trace_pool/activecore_train/c08_seedA
RAW_C16=data/raw_trace_pool/activecore_train/c16_seedA

OUT_C01=data/windows_v12_summary_tq_train600_c01
OUT_C04=data/windows_v12_summary_tq_train600_c04
OUT_C08=data/windows_v12_summary_tq_train600_c08
OUT_C16=data/windows_v12_summary_tq_train600_c16
COMB=data/windows_v12_summary_tq_train600_seedA_c01_c04_c08_c16

if [[ "$CLEAN" == "1" ]]; then
  rm -f data/windows.jsonl data/dedup_report.json
  rm -rf data/.shards
  rm -rf "$OUT_C01" "$OUT_C04" "$OUT_C08" "$OUT_C16" "$COMB"
fi

run_build() {
  local tag="$1"
  local raw="$2"
  local out="$3"

  echo "[build][$tag] raw=$raw out=$out jobs=$JOBS"
  "$PY" data/build_windows.py \
    --raw "$raw" \
    --out "$out" \
    --tq-max-len "$MAX_LEN" \
    --tq-target-windows 1200 \
    --tq-min-uops-per-core "$MIN_UOPS" \
    --tstart-source "$TSTART_SOURCE" \
    --per-workload-cap "$CAP" \
    --per-workload-cap-seed 0 \
    --dedup-threshold 0.05 \
    --dedup-jobs "$JOBS" \
    --jobs "$JOBS" \
    --no-cache \
    > "logs/build_v12_summary_tq_train600_${tag}.log" 2>&1
}

pids=()
run_build c01 "$RAW_C01" "$OUT_C01" & pids+=("$!")
run_build c04 "$RAW_C04" "$OUT_C04" & pids+=("$!")
run_build c08 "$RAW_C08" "$OUT_C08" & pids+=("$!")
run_build c16 "$RAW_C16" "$OUT_C16" & pids+=("$!")

for p in "${pids[@]}"; do
  wait "$p"
done

mkdir -p "$COMB"
: > "$COMB/windows.jsonl"

cat \
  "$OUT_C01/windows.jsonl" \
  "$OUT_C04/windows.jsonl" \
  "$OUT_C08/windows.jsonl" \
  "$OUT_C16/windows.jsonl" \
  >> "$COMB/windows.jsonl"

"$PY" scripts/prepare_dataset_cache.py \
  --data "$COMB/windows.jsonl" \
  --base-model "$BASE_MODEL" \
  --max-len "$MAX_LEN" \
  --format tensor \
  --jobs "$JOBS" \
  --lines-per-shard 512

echo "[done] $COMB/windows.jsonl"
