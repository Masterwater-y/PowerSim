#!/usr/bin/env bash
# Shard seed1 deployment inference across local GPUs and merge trace reports.
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
CKPT=${CKPT:-$ROOT/ckpt/tcsim_v281_a2_100m_8gpu_30k/best.pt}
INFER_CKPT=${INFER_CKPT:-${CKPT%.pt}.infer.pt}
MANIFEST=${MANIFEST:-$ROOT/data/v28_1_business_a2_sharedzipf_dataset/manifest.json}
SPLIT=${SPLIT:-deployment_inference}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
AMP_DTYPE=${AMP_DTYPE:-bf16}
SDPA_BACKEND=${SDPA_BACKEND:-auto}
STATIC_CACHE_ENTRIES=${STATIC_CACHE_ENTRIES:-256}
MAX_TRACES=${MAX_TRACES:-0}
MAX_CHUNKS_PER_CORE=${MAX_CHUNKS_PER_CORE:-0}
CORE_COUNTS=${CORE_COUNTS:-}
PROGRESS_EVERY_STEPS=${PROGRESS_EVERY_STEPS:-200}
RESUME=${RESUME:-1}
OUT_ROOT=${OUT_ROOT:-$ROOT/logs/v281_a2_seed1_c04_c08_c16_c32_full}
TRACE_LOG_DIR=${TRACE_LOG_DIR:-$OUT_ROOT/trace_logs}
STATE_DIR=${STATE_DIR:-$OUT_ROOT/.worker_state}

cd "$ROOT"
mkdir -p "$OUT_ROOT" "$TRACE_LOG_DIR" "$STATE_DIR"
IFS=',' read -r -a GPU_ARRAY <<< "$GPUS"
N=${#GPU_ARRAY[@]}
if (( N == 0 )); then
  echo "GPUS is empty" >&2
  exit 2
fi

# The training checkpoint also contains Adam states and is about 3x larger.
# Export once before all GPU shards load it concurrently.
if [[ ! -f "$INFER_CKPT" || "$CKPT" -nt "$INFER_CKPT" ]]; then
  "$PY" scripts/export_inference_checkpoint.py --input "$CKPT" --out "$INFER_CKPT"
fi

pids=()
resume_args=()
if [[ "$RESUME" == "1" ]]; then
  resume_args=(--resume)
fi
for ((i=0; i<N; i++)); do
  gpu=${GPU_ARRAY[$i]}
  out="$STATE_DIR/worker_${i}.json"
  echo "[launch] worker=$i/$N gpu=$gpu trace_logs=$TRACE_LOG_DIR"
  CUDA_VISIBLE_DEVICES="$gpu" "$PY" scripts/infer_deployment.py \
    --ckpt "$INFER_CKPT" \
    --manifest "$MANIFEST" \
    --split "$SPLIT" \
    --out "$out" \
    --device cuda \
    --amp-dtype "$AMP_DTYPE" \
    --sdpa-backend "$SDPA_BACKEND" \
    --static-cache-entries "$STATIC_CACHE_ENTRIES" \
    --max-traces "$MAX_TRACES" \
    --max-chunks-per-core "$MAX_CHUNKS_PER_CORE" \
    --core-counts "$CORE_COUNTS" \
    --progress-every-steps "$PROGRESS_EVERY_STEPS" \
    --trace-log-dir "$TRACE_LOG_DIR" \
    --num-shards "$N" \
    --shard-index "$i" \
    "${resume_args[@]}" \
    &
  pids+=("$!")
done

on_signal() {
  for child in "${pids[@]}"; do
    kill "$child" 2>/dev/null || true
  done
  exit 130
}
trap on_signal INT TERM

failed=0
for pid in "${pids[@]}"; do
  wait "$pid" || failed=$((failed + 1))
done
if (( failed != 0 )); then
  echo "[FAIL] $failed worker(s) failed; inspect the main launcher log and $TRACE_LOG_DIR" >&2
  exit 1
fi

"$PY" scripts/merge_deployment_reports.py \
  --inputs "$STATE_DIR"/worker_*.json \
  --out "$OUT_ROOT/report.json"
echo "[complete] report=$OUT_ROOT/report.json trace_logs=$TRACE_LOG_DIR"
