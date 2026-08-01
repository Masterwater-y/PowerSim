#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
cd "$ROOT"
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
CKPT=${CKPT:-ckpt/tcsim_v29_packed3_100m_8gpu_60k/best.pt}
MANIFEST=${MANIFEST:-data/v29_global_time_dataset/manifest.json}
TCSIM_CONTEXT_BACKEND=${TCSIM_CONTEXT_BACKEND:-native}
TCSIM_GSS_BACKEND=${TCSIM_GSS_BACKEND:-native}
OUT=${OUT:-logs/v29_eval_$(date +%Y%m%d_%H%M%S)}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
SPLITS=${SPLITS:-seed0_inference,development_heldout}
MODE=${MODE:-both}
WINDOW_PARALLEL_MODE=${WINDOW_PARALLEL_MODE:-serial}
WINDOW_PARALLEL_DEVICES=${WINDOW_PARALLEL_DEVICES:-}
WINDOW_PARALLEL_SHIFT=${WINDOW_PARALLEL_SHIFT:-64}
WINDOW_CONTEXT_BACKEND=${WINDOW_CONTEXT_BACKEND:-process}
CROSS_ATTENTION_BACKEND=${CROSS_ATTENTION_BACKEND:-}
QRKV_PROJECTION_BACKEND=${QRKV_PROJECTION_BACKEND:-}
GSS_PMU_ONLY=${GSS_PMU_ONLY:-0}
TRACE_LOG_DIR=${TRACE_LOG_DIR:-$OUT/trace_logs}
STATE_DIR=${STATE_DIR:-$OUT/.worker_state}

[[ -f "$CKPT" ]] || { echo "[v29-eval][ERROR] missing checkpoint: $CKPT" >&2; exit 2; }
[[ -f "$MANIFEST" ]] || { echo "[v29-eval][ERROR] missing manifest: $MANIFEST" >&2; exit 2; }
if [[ "$TCSIM_CONTEXT_BACKEND" == "native" ]]; then
  if ! "$PY" -c 'import os; import tcsim.v29._context_native as m; raise SystemExit(os.path.getmtime(m.__file__) < os.path.getmtime("tcsim/v29/native_context.cpp"))' >/dev/null 2>&1; then
    echo "[v29-eval] building native context hot path"
    "$PY" scripts/build_v29_context_native.py
  fi
elif [[ "$TCSIM_CONTEXT_BACKEND" != "auto" && "$TCSIM_CONTEXT_BACKEND" != "python" ]]; then
  echo "[v29-eval][ERROR] TCSIM_CONTEXT_BACKEND must be native, auto, or python" >&2
  exit 2
fi
export TCSIM_CONTEXT_BACKEND
if [[ "$GSS_PMU_ONLY" == "1" ]]; then
  if ! "$PY" -c 'import os; import tcsim.v30._gss_native as m; raise SystemExit(os.path.getmtime(m.__file__) < os.path.getmtime("tcsim/v30/native_gss.cpp"))' >/dev/null 2>&1; then
    echo "[v29-eval] building native GSS PMU hot path"
    "$PY" scripts/build_v30_gss_native.py
  fi
  export TCSIM_GSS_BACKEND
elif [[ "$GSS_PMU_ONLY" != "0" ]]; then
  echo "[v29-eval][ERROR] GSS_PMU_ONLY must be 0 or 1" >&2
  exit 2
fi
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
ready_clock_compat_args=()
if [[ "${ALLOW_READY_CLOCK_GSS_COMPAT:-0}" == "1" ]]; then
  ready_clock_compat_args=(--allow-ready-clock-gss-compat)
fi
gss_ablation_args=()
if [[ -n "${GSS_ABLATION_MODE:-}" ]]; then
  gss_ablation_args=(--gss-ablation-mode "$GSS_ABLATION_MODE")
fi
gss_pmu_args=()
if [[ "$GSS_PMU_ONLY" == "1" ]]; then
  gss_pmu_args=(--gss-pmu-only)
fi
cross_attention_args=()
if [[ -n "$CROSS_ATTENTION_BACKEND" ]]; then
  cross_attention_args=(--cross-attention-backend "$CROSS_ATTENTION_BACKEND")
fi
qrkv_projection_args=()
if [[ -n "$QRKV_PROJECTION_BACKEND" ]]; then
  qrkv_projection_args=(--qrkv-projection-backend "$QRKV_PROJECTION_BACKEND")
fi
workload_args=()
if [[ -n "${WORKLOADS:-}" ]]; then
  workload_args=(--workloads "$WORKLOADS")
fi
seed_args=()
if [[ -n "${SEEDS:-}" ]]; then
  seed_args=(--seeds "$SEEDS")
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
    "${cross_attention_args[@]}" \
    "${qrkv_projection_args[@]}" \
    --device cuda \
    --amp-dtype "${AMP_DTYPE:-bf16}" \
    --sdpa-backend "${SDPA_BACKEND:-auto}" \
    --branch-event-scale "${BRANCH_EVENT_SCALE:-1.0}" \
    --branch-history-scale "${BRANCH_HISTORY_SCALE:-1.0}" \
    --core-counts "${CORE_COUNTS:-4,8,16,32}" \
    "${workload_args[@]}" \
    "${seed_args[@]}" \
    --max-oracle-samples "${MAX_ORACLE_SAMPLES:-0}" \
    --max-free-steps "${MAX_FREE_STEPS:-0}" \
    --max-traces "${MAX_TRACES:-0}" \
    --target-stride "${TARGET_STRIDE:-32}" \
    --min-step-cycles "${MIN_STEP_CYCLES:-4}" \
    --max-step-cycles "${MAX_STEP_CYCLES:-1024}" \
    --max-no-progress-steps "${MAX_NO_PROGRESS_STEPS:-64}" \
    --max-core-stall-steps "${MAX_CORE_STALL_STEPS:-256}" \
    --num-shards "$num_shards" \
    --shard-index "$shard" \
    --progress-every "${PROGRESS_EVERY:-200}" \
    "${oracle_drift_args[@]}" \
    "${ready_clock_compat_args[@]}" \
    "${gss_ablation_args[@]}" \
    "${gss_pmu_args[@]}" \
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
