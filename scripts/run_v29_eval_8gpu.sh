#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
cd "$ROOT"
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
CKPT=${CKPT:-ckpt/tcsim_v29_global_time_100m_8gpu_30000/best.pt}
MANIFEST=${MANIFEST:-data/v29_global_time_dataset/manifest.json}
OUT=${OUT:-logs/v29_eval_$(date +%Y%m%d_%H%M%S)}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
SPLITS=${SPLITS:-seed0_inference,development_heldout}
MODE=${MODE:-both}

[[ -f "$CKPT" ]] || { echo "[v29-eval][ERROR] missing checkpoint: $CKPT" >&2; exit 2; }
[[ -f "$MANIFEST" ]] || { echo "[v29-eval][ERROR] missing manifest: $MANIFEST" >&2; exit 2; }
mkdir -p "$OUT" "$OUT/logs"
IFS=',' read -r -a gpu_array <<< "$GPUS"
num_shards=${#gpu_array[@]}
(( num_shards > 0 )) || { echo "[v29-eval][ERROR] empty GPUS" >&2; exit 2; }

pids=()
for shard in "${!gpu_array[@]}"; do
  gpu=${gpu_array[$shard]}
  shard_out="$OUT/shard_$shard"
  mkdir -p "$shard_out"
  echo "[v29-eval] shard=$shard/$num_shards gpu=$gpu out=$shard_out"
  CUDA_VISIBLE_DEVICES="$gpu" "$PY" scripts/infer_v29.py \
    --ckpt "$CKPT" \
    --manifest "$MANIFEST" \
    --splits "$SPLITS" \
    --out "$shard_out" \
    --mode "$MODE" \
    --device cuda \
    --amp-dtype "${AMP_DTYPE:-bf16}" \
    --sdpa-backend "${SDPA_BACKEND:-auto}" \
    --core-counts "${CORE_COUNTS:-4,8,16,32}" \
    --max-oracle-samples "${MAX_ORACLE_SAMPLES:-0}" \
    --max-free-steps "${MAX_FREE_STEPS:-0}" \
    --target-stride "${TARGET_STRIDE:-32}" \
    --min-step-cycles "${MIN_STEP_CYCLES:-4}" \
    --max-step-cycles "${MAX_STEP_CYCLES:-1024}" \
    --max-no-progress-steps "${MAX_NO_PROGRESS_STEPS:-64}" \
    --num-shards "$num_shards" \
    --shard-index "$shard" \
    --progress-every "${PROGRESS_EVERY:-100}" \
    ${RESUME:+--resume} \
    > "$OUT/logs/shard_$shard.log" 2>&1 &
  pids+=("$!")
done

failed=0
for index in "${!pids[@]}"; do
  if ! wait "${pids[$index]}"; then
    echo "[v29-eval][ERROR] shard $index failed; inspect $OUT/logs/shard_$index.log" >&2
    failed=1
  fi
done
(( failed == 0 )) || exit 2

inputs=()
for shard in "${!gpu_array[@]}"; do
  inputs+=("$OUT/shard_$shard/report.json")
done
"$PY" scripts/merge_v29_reports.py --inputs "${inputs[@]}" --out "$OUT"
echo "[v29-eval] report=$OUT/report.txt"
