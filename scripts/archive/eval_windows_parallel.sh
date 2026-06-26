#!/usr/bin/env bash
# Parallel GPU validation on prebuilt windows + ids_cache.
# Unlike scripts/eval_parallel.sh, this uses eval/eval.py and does not read raw
# gem5 traces.
set -euo pipefail

ROOT=/data00/yinhaolang/LLMSim
cd "$ROOT"

DATA=${DATA:-data/windows_v7_c08_tq/windows.jsonl}
CKPT=${CKPT:-ckpt/v7_c08_absmiss_ddp8}
MAXLEN=${MAXLEN:-32768}
BS=${BS:-1}
MAX_SAMPLES=${MAX_SAMPLES:-0}
NUM_WORKERS=${NUM_WORKERS:-0}
NO_CACHE=${NO_CACHE:-0}
PROGRESS_EVERY=${PROGRESS_EVERY:-30}
MODEL_PROGRESS_EVERY=${MODEL_PROGRESS_EVERY:-20}

DEFAULT_WORKLOADS=(
  W_ads_ctr W_ads_ranking_proxy W_branch_storm W_chase_dram
  W_compute_int W_false_sharing W_feed_ranking W_fp_compute_dense
  W_fp_lite W_graph_recall_proxy W_indirect W_int_div
  W_interest_graph_recall W_mlp_light W_phased_mix
  W_search_index_proxy W_stream
)
read -r -a WORKLOADS <<< "${WORKLOADS:-${DEFAULT_WORKLOADS[*]}}"
IFS=',' read -r -a GPUS <<< "${GPUS:-0,1,2,3,4,5,6,7}"

TAG=${TAG:-$(basename "$CKPT")_windows}
TS=$(date +%Y%m%d_%H%M%S)
LOGDIR=${LOGDIR:-logs/eval_windows_parallel_${TAG}_${TS}}
mkdir -p "$LOGDIR"

echo "[meta] DATA=$DATA"
echo "[meta] CKPT=$CKPT"
echo "[meta] GPUS=${GPUS[*]}"
echo "[meta] WORKLOADS=${WORKLOADS[*]}"
echo "[meta] MAX_SAMPLES=$MAX_SAMPLES NO_CACHE=$NO_CACHE MODEL_PROGRESS_EVERY=$MODEL_PROGRESS_EVERY"
echo "[meta] LOGDIR=$LOGDIR"

declare -A GPU_PID
declare -A GPU_WORKLOAD
FAILS=0

launch_one() {
  local w=$1
  local gpu=$2
  local log="$LOGDIR/${w}.log"
  echo "[launch] gpu=$gpu workload=$w -> $log"
  GPU="$gpu" DATA="$DATA" CKPT="$CKPT" MAXLEN="$MAXLEN" BS="$BS" \
    MAX_SAMPLES="$MAX_SAMPLES" NUM_WORKERS="$NUM_WORKERS" \
    PROGRESS_EVERY="$MODEL_PROGRESS_EVERY" \
    NO_CACHE="$NO_CACHE" WORKLOADS="$w" LOG="$log" \
    bash scripts/eval_windows_gpu.sh </dev/null > "$LOGDIR/${w}.driver.log" 2>&1 &
  GPU_PID[$gpu]=$!
  GPU_WORKLOAD[$gpu]=$w
}

QUEUE=("${WORKLOADS[@]}")
for g in "${GPUS[@]}"; do
  if [[ ${#QUEUE[@]} -eq 0 ]]; then break; fi
  w=${QUEUE[0]}; QUEUE=("${QUEUE[@]:1}")
  launch_one "$w" "$g"
done

progress_loop() {
  while true; do
    sleep "$PROGRESS_EVERY"
    echo
    echo "============ progress @ $(date +%H:%M:%S) ============"
    for f in "$LOGDIR"/W_*.log; do
      [[ -f "$f" ]] || continue
      w=$(basename "$f" .log)
      line=$(grep -E "^\[eval\]|^\[time\]|^\[data\]|num_core_windows|cpi_within" "$f" | tail -n 1 || true)
      [[ -n "$line" ]] || line=$(tail -n 1 "$f")
      printf "%-28s %s\n" "$w" "$line"
    done
  done
}

progress_loop &
PROG_PID=$!
trap 'kill $PROG_PID 2>/dev/null || true' EXIT

while true; do
  running=0
  for g in "${!GPU_PID[@]}"; do
    pid=${GPU_PID[$g]}
    if kill -0 "$pid" 2>/dev/null; then
      running=$((running + 1))
    else
      rc=0
      wait "$pid" 2>/dev/null || rc=$?
      if [[ $rc -ne 0 ]]; then
        echo "[error] gpu=$g workload=${GPU_WORKLOAD[$g]:-unknown} exit=$rc"
        FAILS=$((FAILS + 1))
      fi
      unset "GPU_PID[$g]"
      unset "GPU_WORKLOAD[$g]"
      if [[ ${#QUEUE[@]} -gt 0 ]]; then
        w=${QUEUE[0]}; QUEUE=("${QUEUE[@]:1}")
        launch_one "$w" "$g"
        running=$((running + 1))
      fi
    fi
  done
  if [[ $running -eq 0 && ${#QUEUE[@]} -eq 0 ]]; then
    break
  fi
  sleep 5
done

kill "$PROG_PID" 2>/dev/null || true
trap - EXIT

echo
echo "============ all done @ $(date +%H:%M:%S) ============"
echo "fails=$FAILS logs=$LOGDIR"

python3 - "$LOGDIR" <<'PY'
import glob, json, os, re, sys

logdir = sys.argv[1]
rows = []
for fp in sorted(glob.glob(os.path.join(logdir, "W_*.log"))):
    name = os.path.basename(fp)[:-4]
    text = open(fp).read()
    obj = {}
    for m in re.finditer(r"^\{", text, flags=re.M):
        try:
            cand = json.loads(text[m.start():])
        except Exception:
            continue
        if isinstance(cand, dict) and "num_core_windows" in cand:
            obj = cand
    timing = obj.get("timing_s", {})
    rows.append((
        name,
        obj.get("num_core_windows"),
        obj.get("mae_cpi_uop"),
        obj.get("mape_cpi_uop"),
        obj.get("cpi_within_10pct"),
        timing.get("dataset_preprocess_s"),
        timing.get("forward_eval_s"),
        timing.get("total_s"),
    ))

print("workload,core_windows,mae_cpi_uop,mape_cpi_uop,cpi_within_10pct,dataset_s,forward_s,total_s")
for r in rows:
    print(",".join("" if v is None else str(v) for v in r))
PY

exit "$FAILS"
