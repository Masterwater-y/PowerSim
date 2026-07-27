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
WINDOW_PARALLEL_MODE=${WINDOW_PARALLEL_MODE:-serial}
WINDOW_PARALLEL_DEVICES=${WINDOW_PARALLEL_DEVICES:-}
WINDOW_PARALLEL_SHIFT=${WINDOW_PARALLEL_SHIFT:-64}
WINDOW_CONTEXT_BACKEND=${WINDOW_CONTEXT_BACKEND:-process}
TRACE_LOG_DIR=${TRACE_LOG_DIR:-$OUT/trace_logs}
STATE_DIR=${STATE_DIR:-$OUT/.worker_state}

[[ -f "$CKPT" ]] || { echo "[v29-eval][ERROR] missing checkpoint: $CKPT" >&2; exit 2; }
[[ -f "$MANIFEST" ]] || { echo "[v29-eval][ERROR] missing manifest: $MANIFEST" >&2; exit 2; }
mkdir -p "$OUT" "$TRACE_LOG_DIR" "$STATE_DIR"
IFS=',' read -r -a gpu_array <<< "$GPUS"
num_shards=${#gpu_array[@]}
(( num_shards > 0 )) || { echo "[v29-eval][ERROR] empty GPUS" >&2; exit 2; }

pids=()
resume_args=()
if [[ "${RESUME:-0}" == "1" ]]; then
  resume_args=(--resume)
fi
oracle_drift_args=()
if [[ "${ORACLE_DRIFT_DIAGNOSTICS:-0}" == "1" ]]; then
  oracle_drift_args=(--oracle-drift-diagnostics)
fi
workload_args=()
if [[ -n "${WORKLOADS:-}" ]]; then
  workload_args=(--workloads "$WORKLOADS")
fi
for shard in "${!gpu_array[@]}"; do
  gpu=${gpu_array[$shard]}
  worker_report="$STATE_DIR/worker_$shard.json"
  echo "[v29-eval] worker=$shard/$num_shards gpu=$gpu trace_logs=$TRACE_LOG_DIR"
  CUDA_VISIBLE_DEVICES="$gpu" "$PY" scripts/infer_v29.py \
    --ckpt "$CKPT" \
    --manifest "$MANIFEST" \
    --splits "$SPLITS" \
    --out "$OUT" \
    --worker-report "$worker_report" \
    --trace-log-dir "$TRACE_LOG_DIR" \
    --mode "$MODE" \
    --window-parallel-mode "$WINDOW_PARALLEL_MODE" \
    --window-parallel-devices "$WINDOW_PARALLEL_DEVICES" \
    --window-parallel-shift "$WINDOW_PARALLEL_SHIFT" \
    --window-context-backend "$WINDOW_CONTEXT_BACKEND" \
    --device cuda \
    --amp-dtype "${AMP_DTYPE:-bf16}" \
    --sdpa-backend "${SDPA_BACKEND:-auto}" \
    --core-counts "${CORE_COUNTS:-4,8,16,32}" \
    "${workload_args[@]}" \
    --max-oracle-samples "${MAX_ORACLE_SAMPLES:-0}" \
    --max-free-steps "${MAX_FREE_STEPS:-0}" \
    --target-stride "${TARGET_STRIDE:-32}" \
    --min-step-cycles "${MIN_STEP_CYCLES:-4}" \
    --max-step-cycles "${MAX_STEP_CYCLES:-1024}" \
    --max-no-progress-steps "${MAX_NO_PROGRESS_STEPS:-64}" \
    --num-shards "$num_shards" \
    --shard-index "$shard" \
    --progress-every "${PROGRESS_EVERY:-200}" \
    "${oracle_drift_args[@]}" \
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
for index in "${!pids[@]}"; do
  if wait "${pids[$index]}"; then
    continue
  else
    status=$?
    echo "[v29-eval][ERROR] worker $index exit_status=$status; inspect the main launch log and $TRACE_LOG_DIR" >&2
    failed=1
  fi
done
(( failed == 0 )) || exit 2

inputs=()
for shard in "${!gpu_array[@]}"; do
  inputs+=("$STATE_DIR/worker_$shard.json")
done
"$PY" scripts/merge_v29_reports.py --inputs "${inputs[@]}" --out "$OUT"
echo "[v29-eval] report=$OUT/report.txt trace_logs=$TRACE_LOG_DIR"
