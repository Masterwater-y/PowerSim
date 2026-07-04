#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
cd "$ROOT"

TS=${TS:-$(date +%Y%m%d_%H%M%S)}
CKPT_ROOT=${CKPT_ROOT:-ckpt/v18_fastslow_adapter_scratch_8gpu_20000}
CKPT=${CKPT:-}
RAW=${RAW:-data/raw_trace_pool/activecore_eval/c08_seedB_infer17}
MAX_LEN=${MAX_LEN:-32768}
QUERY_PLACEMENT=${QUERY_PLACEMENT:-tail_local}
RUN_ROOT=${RUN_ROOT:-logs/v18_c08_diagnostics_${TS}}

# Eval job. By default this is a smoke c8 sweep on GPUs 2-7 so it can run
# alongside the two alignment diagnostics on GPUs 0/1.
RUN_EVAL=${RUN_EVAL:-1}
EVAL_MODE=${EVAL_MODE:-smoke}       # smoke | full | none
EVAL_SMOKE_WINDOWS=${EVAL_SMOKE_WINDOWS:-50}
EVAL_GPUS=${EVAL_GPUS:-2,3,4,5,6,7}
PROGRESS_EVERY=${PROGRESS_EVERY:-30}

# Alignment jobs.
RUN_ALIGNMENT=${RUN_ALIGNMENT:-1}
ALIGN_WORKLOADS=${ALIGN_WORKLOADS:-W_ads_ranking_proxy}
PLANNER_SOURCES=${PLANNER_SOURCES:-"pred label"}
ALIGN_GPUS=${ALIGN_GPUS:-0,1}
ALIGN_MAX_WINDOWS=${ALIGN_MAX_WINDOWS:-0}

if [[ -z "$CKPT" ]]; then
  CKPT=$(find "$CKPT_ROOT" -maxdepth 1 -type d -name 'step_*' | sort -V | tail -n 1)
fi

if [[ -z "$CKPT" || ! -d "$CKPT" ]]; then
  echo "[diag][error] checkpoint not found: ${CKPT:-<empty>}" >&2
  exit 1
fi
if [[ ! -f "$CKPT/head_best.pt" || ! -d "$CKPT/lora_best" ]]; then
  echo "[diag][error] checkpoint missing head_best.pt or lora_best: $CKPT" >&2
  exit 1
fi
if [[ ! -d "$RAW" ]]; then
  echo "[diag][error] raw eval root not found: $RAW" >&2
  exit 1
fi

mkdir -p "$RUN_ROOT"

echo "[diag] root=$ROOT"
echo "[diag] ckpt=$CKPT"
echo "[diag] raw=$RAW"
echo "[diag] max_len=$MAX_LEN query_placement=$QUERY_PLACEMENT"
echo "[diag] run_root=$RUN_ROOT"
echo "[diag] run_eval=$RUN_EVAL eval_mode=$EVAL_MODE eval_gpus=$EVAL_GPUS"
echo "[diag] run_alignment=$RUN_ALIGNMENT align_workloads=$ALIGN_WORKLOADS planner_sources=$PLANNER_SOURCES align_gpus=$ALIGN_GPUS"
echo

declare -a PIDS=()
declare -a NAMES=()
declare -a LOGS=()

launch_bg() {
  local name="$1"
  local log="$2"
  shift 2
  echo "[diag] launch $name -> $log"
  (
    "$@"
  ) > "$log" 2>&1 &
  PIDS+=("$!")
  NAMES+=("$name")
  LOGS+=("$log")
}

if [[ "$RUN_EVAL" == "1" && "$EVAL_MODE" != "none" ]]; then
  case "$EVAL_MODE" in
    smoke)
      EVAL_MAX_WINDOWS=${EVAL_MAX_WINDOWS:-$EVAL_SMOKE_WINDOWS}
      ;;
    full)
      EVAL_MAX_WINDOWS=${EVAL_MAX_WINDOWS:-0}
      ;;
    *)
      echo "[diag][error] EVAL_MODE must be smoke|full|none, got: $EVAL_MODE" >&2
      exit 2
      ;;
  esac
  step_name=$(basename "$CKPT")
  eval_tag=${EVAL_TAG:-v18_fastslow_${step_name}_c08_seedB_${EVAL_MODE}${EVAL_MAX_WINDOWS}_ctx${MAX_LEN}}
  launch_bg \
    "c08_eval_${EVAL_MODE}" \
    "$RUN_ROOT/c08_eval_${EVAL_MODE}.log" \
    env CKPT="$CKPT" RAW="$RAW" TAG="$eval_tag" GPUS="$EVAL_GPUS" \
      MAX_LEN="$MAX_LEN" MAX_WINDOWS="$EVAL_MAX_WINDOWS" \
      QUERY_PLACEMENT="$QUERY_PLACEMENT" PROGRESS_EVERY="$PROGRESS_EVERY" \
      bash scripts/eval_parallel.sh
fi

if [[ "$RUN_ALIGNMENT" == "1" ]]; then
  IFS=',' read -r -a ALIGN_GPU_ARR <<< "$ALIGN_GPUS"
  read -r -a WORKLOAD_ARR <<< "$ALIGN_WORKLOADS"
  read -r -a SOURCE_ARR <<< "$PLANNER_SOURCES"
  if [[ ${#ALIGN_GPU_ARR[@]} -eq 0 ]]; then
    echo "[diag][error] ALIGN_GPUS is empty" >&2
    exit 2
  fi
  job_i=0
  for workload in "${WORKLOAD_ARR[@]}"; do
    for source in "${SOURCE_ARR[@]}"; do
      gpu="${ALIGN_GPU_ARR[$((job_i % ${#ALIGN_GPU_ARR[@]}))]}"
      name="align_${workload}_${source}"
      outdir="$RUN_ROOT/$name"
      tag="v18_${name}"
      launch_bg \
        "$name" \
        "$RUN_ROOT/${name}.log" \
        env CKPT="$CKPT" RAW="$RAW" WORKLOAD="$workload" GPU="$gpu" \
          MAX_LEN="$MAX_LEN" MAX_WINDOWS="$ALIGN_MAX_WINDOWS" \
          QUERY_PLACEMENT="$QUERY_PLACEMENT" PLANNER_STATE_SOURCE="$source" \
          TAG="$tag" OUTDIR="$outdir" \
          bash scripts/run_v16_ads_oracle_cut_eval.sh
      job_i=$((job_i + 1))
    done
  done
fi

echo
echo "[diag] launched ${#PIDS[@]} jobs"

status=0
for i in "${!PIDS[@]}"; do
  pid="${PIDS[$i]}"
  name="${NAMES[$i]}"
  log="${LOGS[$i]}"
  if wait "$pid"; then
    echo "[diag] done $name"
  else
    rc=$?
    echo "[diag][error] failed $name rc=$rc log=$log" >&2
    tail -80 "$log" >&2 || true
    status=$rc
  fi
done

echo
echo "[diag] summary"
echo "  run_root: $RUN_ROOT"
for i in "${!NAMES[@]}"; do
  echo "  ${NAMES[$i]} log: ${LOGS[$i]}"
done

if [[ "$RUN_ALIGNMENT" == "1" ]]; then
  echo
  echo "[diag] alignment summaries"
  for analysis in "$RUN_ROOT"/align_*/alignment_analysis.txt; do
    [[ -s "$analysis" ]] || continue
    echo "===== $analysis ====="
    cat "$analysis"
    echo
  done
fi

if [[ "$RUN_EVAL" == "1" && "$EVAL_MODE" != "none" ]]; then
  echo "[diag] eval tail"
  tail -120 "$RUN_ROOT/c08_eval_${EVAL_MODE}.log" || true
fi

exit "$status"
