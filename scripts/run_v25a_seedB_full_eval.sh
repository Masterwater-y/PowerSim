#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TSim}
cd "$ROOT"

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
CKPT=${CKPT:-ckpt/v25a_tiny_transformer_8l320_8gpu_8000}
CORES=${CORES:-"04 08 16"}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
MAX_LEN=${MAX_LEN:-32768}
TRAIN_MAX_LEN=${TRAIN_MAX_LEN:-32768}
MAX_WINDOWS=${MAX_WINDOWS:-0}
LOAD_MAX_ROWS_PER_CORE=${LOAD_MAX_ROWS_PER_CORE:-0}
QUERY_PLACEMENT=${QUERY_PLACEMENT:-tail_local}
PLANNER_STATE_SOURCE=${PLANNER_STATE_SOURCE:-label}
DEVICE=${DEVICE:-cuda}
DT_TARGET=${DT_TARGET:-8000}
DT_MAX=${DT_MAX:-12000}
PROGRESS_EVERY=${PROGRESS_EVERY:-60}
TS=${TS:-$(date +%Y%m%d_%H%M%S)}
OUT_ROOT=${OUT_ROOT:-logs/v25a_seedB_full_${TS}}

WORKLOADS=${WORKLOADS:-"W_ads_ctr W_ads_ranking_proxy W_branch_storm W_chase_dram W_compute_int W_false_sharing W_feed_ranking W_fp_compute_dense W_fp_lite W_graph_recall_proxy W_indirect W_int_div W_interest_graph_recall W_mlp_light W_phased_mix W_search_index_proxy W_stream"}

IFS=',' read -r -a GPU_ARR <<< "$GPUS"
read -r -a CORE_ARR <<< "$CORES"
read -r -a WORKLOAD_ARR <<< "$WORKLOADS"

if [[ ! -x "$PY" ]]; then
  echo "[v25a-eval][error] python not executable: $PY" >&2
  exit 2
fi
if [[ ! -f "$CKPT/head_best.pt" || ! -d "$CKPT/lora_best" ]]; then
  echo "[v25a-eval][error] checkpoint missing head_best.pt or lora_best: $CKPT" >&2
  exit 2
fi
if [[ ${#GPU_ARR[@]} -eq 0 ]]; then
  echo "[v25a-eval][error] GPUS is empty" >&2
  exit 2
fi

for C in "${CORE_ARR[@]}"; do
  RAW="data/raw_trace_pool/activecore_eval/c${C}_seedB_infer17"
  if [[ ! -d "$RAW" ]]; then
    echo "[v25a-eval][error] raw root not found: $RAW" >&2
    exit 2
  fi
done

mkdir -p "$OUT_ROOT"
cat > "$OUT_ROOT/config.txt" <<EOF
root=$ROOT
python=$PY
ckpt=$CKPT
cores=$CORES
gpus=$GPUS
max_len=$MAX_LEN
train_max_len=$TRAIN_MAX_LEN
max_windows=$MAX_WINDOWS
load_max_rows_per_core=$LOAD_MAX_ROWS_PER_CORE
query_placement=$QUERY_PLACEMENT
planner_state_source=$PLANNER_STATE_SOURCE
device=$DEVICE
dt_target=$DT_TARGET
dt_max=$DT_MAX
workloads=$WORKLOADS
timestamp=$TS
EOF

echo "[v25a-eval] start $(date '+%F %T')"
echo "[v25a-eval] out_root=$OUT_ROOT"
echo "[v25a-eval] ckpt=$CKPT"
echo "[v25a-eval] cores=$CORES workloads=${#WORKLOAD_ARR[@]} total_tasks=$((${#CORE_ARR[@]} * ${#WORKLOAD_ARR[@]}))"

declare -a TASK_CORES=()
declare -a TASK_WORKLOADS=()
for C in "${CORE_ARR[@]}"; do
  mkdir -p "$OUT_ROOT/c${C}/run_logs"
  for W in "${WORKLOAD_ARR[@]}"; do
    TASK_CORES+=("$C")
    TASK_WORKLOADS+=("$W")
  done
done

declare -A GPU_PID=()
declare -A GPU_CORE=()
declare -A GPU_WORKLOAD=()
declare -A GPU_LOG=()
NEXT_TASK=0
FAILS=0
DONE=0
PROGRESS_PID=""

cleanup_children() {
  if [[ -n "${PROGRESS_PID:-}" ]]; then
    kill "$PROGRESS_PID" 2>/dev/null || true
  fi
  for G in "${!GPU_PID[@]}"; do
    kill "${GPU_PID[$G]}" 2>/dev/null || true
  done
}

on_signal() {
  trap - EXIT INT TERM
  cleanup_children
  echo "[v25a-eval] interrupted; cleaned child processes" >&2
  exit 130
}

trap on_signal INT TERM
trap 'rc=$?; if [[ $rc -ne 0 ]]; then cleanup_children; fi' EXIT

launch_task() {
  local gpu="$1"
  local idx="$2"
  local C="${TASK_CORES[$idx]}"
  local W="${TASK_WORKLOADS[$idx]}"
  local RAW="data/raw_trace_pool/activecore_eval/c${C}_seedB_infer17"
  local LOG="$OUT_ROOT/c${C}/run_logs/${W}.log"
  local -a device_args=()
  if [[ -n "$DEVICE" ]]; then
    device_args=(--device "$DEVICE")
  fi

  echo "[v25a-eval] launch gpu=$gpu c${C} workload=$W log=$LOG"
  HF_HUB_OFFLINE=1 CUDA_VISIBLE_DEVICES="$gpu" "$PY" eval/eval_quota_cycles.py \
    --raw-root "$RAW" \
    --workload "$W" \
    --ckpt "$CKPT" \
    --dt-target "$DT_TARGET" \
    --dt-max "$DT_MAX" \
    --max-len "$MAX_LEN" \
    --train-max-len "$TRAIN_MAX_LEN" \
    --max-windows "$MAX_WINDOWS" \
    --load-max-rows-per-core "$LOAD_MAX_ROWS_PER_CORE" \
    --query-placement "$QUERY_PLACEMENT" \
    --planner-state-source "$PLANNER_STATE_SOURCE" \
    "${device_args[@]}" \
    > "$LOG" 2>&1 &

  GPU_PID[$gpu]=$!
  GPU_CORE[$gpu]="$C"
  GPU_WORKLOAD[$gpu]="$W"
  GPU_LOG[$gpu]="$LOG"
}

progress_loop() {
  while true; do
    sleep "$PROGRESS_EVERY" || true
    echo
    echo "============ v25a-eval progress @ $(date '+%F %T') done=$DONE/${#TASK_CORES[@]} running=${#GPU_PID[@]} ============"
    for G in "${GPU_ARR[@]}"; do
      local pid="${GPU_PID[$G]:-}"
      [[ -n "$pid" ]] || continue
      local C="${GPU_CORE[$G]}"
      local W="${GPU_WORKLOAD[$G]}"
      local LOG="${GPU_LOG[$G]}"
      local LINE=""
      LINE=$(grep -E "^\\s*\\[$W\\] " "$LOG" 2>/dev/null | tail -n 1 || true)
      if [[ -z "$LINE" ]]; then
        LINE=$(tail -n 1 "$LOG" 2>/dev/null || true)
      fi
      printf "gpu=%-2s c%-2s %-26s %s\n" "$G" "$C" "$W" "$LINE"
    done
  done
}

if [[ "$PROGRESS_EVERY" != "0" ]]; then
  progress_loop &
  PROGRESS_PID=$!
fi

for G in "${GPU_ARR[@]}"; do
  if [[ "$NEXT_TASK" -ge "${#TASK_CORES[@]}" ]]; then
    break
  fi
  launch_task "$G" "$NEXT_TASK"
  NEXT_TASK=$((NEXT_TASK + 1))
done

while true; do
  RUNNING=0
  for G in "${GPU_ARR[@]}"; do
    pid="${GPU_PID[$G]:-}"
    [[ -n "$pid" ]] || continue
    if kill -0 "$pid" 2>/dev/null; then
      RUNNING=$((RUNNING + 1))
      continue
    fi

    rc=0
    wait "$pid" 2>/dev/null || rc=$?
    C="${GPU_CORE[$G]}"
    W="${GPU_WORKLOAD[$G]}"
    LOG="${GPU_LOG[$G]}"
    if [[ "$rc" -eq 0 ]]; then
      DONE=$((DONE + 1))
      echo "[v25a-eval] done c${C}/$W ($DONE/${#TASK_CORES[@]})"
    else
      FAILS=$((FAILS + 1))
      DONE=$((DONE + 1))
      echo "[v25a-eval][FAIL] c${C}/$W rc=$rc log=$LOG" >&2
      tail -n 30 "$LOG" >&2 || true
    fi
    unset "GPU_PID[$G]" "GPU_CORE[$G]" "GPU_WORKLOAD[$G]" "GPU_LOG[$G]"

    if [[ "$NEXT_TASK" -lt "${#TASK_CORES[@]}" ]]; then
      launch_task "$G" "$NEXT_TASK"
      NEXT_TASK=$((NEXT_TASK + 1))
      RUNNING=$((RUNNING + 1))
    fi
  done
  if [[ "$RUNNING" -eq 0 && "$NEXT_TASK" -ge "${#TASK_CORES[@]}" ]]; then
    break
  fi
  sleep 5
done

cleanup_children
trap - EXIT INT TERM
echo "[v25a-eval] complete done=$DONE fails=$FAILS out_root=$OUT_ROOT"
if [[ "$FAILS" -ne 0 ]]; then
  exit 1
fi
