#!/usr/bin/env bash
# 并行 dump 多个 workload 的逐窗诊断 JSONL。
# 每张 GPU 一个进程，跑完一个再继续队列里的下一个。
#
# 用法：
#   CKPT=ckpt/v5_tq32k_schemeA_2000 \
#   WORKLOADS="W_chase_dram W_interest_graph_recall" \
#   GPUS="1,2" \
#   bash scripts/dump_parallel.sh
#
# 主要参数（环境变量）：
#   CKPT            必填，待评估的 ckpt 目录
#   RAW             默认 data/raw_eval11_8c
#   WORKLOADS       默认 11 个 workload；可覆盖成子集
#   GPUS            默认 0,1,2,3,4,5,6,7
#   DUMP_DIR        默认 logs/dump_parallel_<tag>_<ts>/dumps
#   LOGDIR          默认 logs/dump_parallel_<tag>_<ts>
#   CLEAN           默认 1；启动前删除目标 workload 的旧 dump
#   DT_TARGET       默认 8000
#   DT_MAX          默认 12000
#   MAX_LEN         默认 32768
#   MAX_WINDOWS     默认 0（全量）
#   PROGRESS_EVERY  默认 30s
set -euo pipefail

ROOT=/data00/yinhaolang/LLMSim
cd "$ROOT"

CKPT=${CKPT:?"need CKPT=ckpt/xxx"}
RAW=${RAW:-data/raw_eval11_8c}
DT_TARGET=${DT_TARGET:-8000}
DT_MAX=${DT_MAX:-12000}
MAX_LEN=${MAX_LEN:-32768}
MAX_WINDOWS=${MAX_WINDOWS:-0}
PROGRESS_EVERY=${PROGRESS_EVERY:-30}
CLEAN=${CLEAN:-1}
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}

DEFAULT_WORKLOADS=(
  W_branch_storm W_chase_dram W_compute_int W_false_sharing
  W_indirect W_int_div W_phased_mix W_stream
  W_ads_ctr W_feed_ranking W_interest_graph_recall
)
read -r -a WORKLOADS <<< "${WORKLOADS:-${DEFAULT_WORKLOADS[*]}}"
IFS=',' read -r -a GPUS <<< "${GPUS:-0,1,2,3,4,5,6,7}"

TAG=${TAG:-$(basename "$CKPT")}
TS=$(date +%Y%m%d_%H%M%S)
LOGDIR=${LOGDIR:-logs/dump_parallel_${TAG}_${TS}}
DUMP_DIR=${DUMP_DIR:-$LOGDIR/dumps}
mkdir -p "$LOGDIR" "$DUMP_DIR"

echo "[meta] CKPT=$CKPT"
echo "[meta] RAW=$RAW"
echo "[meta] GPUS=${GPUS[*]}"
echo "[meta] WORKLOADS=${WORKLOADS[*]}"
echo "[meta] MAX_WINDOWS=$MAX_WINDOWS"
echo "[meta] CLEAN=$CLEAN"
echo "[meta] LOGDIR=$LOGDIR"
echo "[meta] DUMP_DIR=$DUMP_DIR"
echo

cleanup_one() {
  local W=$1
  rm -f \
    "$DUMP_DIR/${W}.windows.jsonl" \
    "$DUMP_DIR/${W}.windows.jsonl."* \
    "$LOGDIR/${W}.log"
}

if [[ "$CLEAN" == "1" ]]; then
  echo "[clean] removing old dumps/logs for target workloads"
  for W in "${WORKLOADS[@]}"; do
    cleanup_one "$W"
  done
  echo
fi

declare -A GPU_PID
declare -A GPU_WORKLOAD
FAILS=0

launch_one() {
  local W=$1
  local GPU=$2
  local LOG="$LOGDIR/${W}.log"
  echo "[launch] gpu=$GPU workload=$W dump=$DUMP_DIR/${W}.windows.jsonl -> $LOG"
  HF_HUB_OFFLINE=1 CUDA_VISIBLE_DEVICES="$GPU" \
    "$PY" eval/eval_quota_cycles.py \
      --raw-root "$RAW" \
      --workload "$W" \
      --ckpt "$CKPT" \
      --dt-target "$DT_TARGET" --dt-max "$DT_MAX" \
      --max-len "$MAX_LEN" \
      --max-windows "$MAX_WINDOWS" \
      --device cuda:0 \
      --dump-window-jsonl-dir "$DUMP_DIR" \
      </dev/null > "$LOG" 2>&1 &
  GPU_PID[$GPU]=$!
  GPU_WORKLOAD[$GPU]=$W
}

QUEUE=("${WORKLOADS[@]}")

for G in "${GPUS[@]}"; do
  if [[ ${#QUEUE[@]} -eq 0 ]]; then
    break
  fi
  W=${QUEUE[0]}
  QUEUE=("${QUEUE[@]:1}")
  launch_one "$W" "$G"
done

progress_loop() {
  while true; do
    sleep "$PROGRESS_EVERY"
    echo
    echo "============ progress @ $(date +%H:%M:%S) ============"
    for W in "${WORKLOADS[@]}"; do
      local_dump="$DUMP_DIR/${W}.windows.jsonl"
      local_log="$LOGDIR/${W}.log"
      if [[ -f "$local_dump" ]]; then
        DUMP_ROWS=$(wc -l < "$local_dump" 2>/dev/null || echo 0)
      else
        DUMP_ROWS=0
      fi
      if [[ -f "$local_log" ]]; then
        LINE=$(grep -E "^\s*\[$W\] " "$local_log" 2>/dev/null | tail -n 1)
        if [[ -z "$LINE" ]]; then
          LINE=$(tail -n 1 "$local_log" 2>/dev/null || true)
        fi
      else
        LINE="(not started)"
      fi
      printf "%-28s dump_rows=%-8s %s\n" "$W" "$DUMP_ROWS" "${LINE:-}"
    done
  done
}

progress_loop &
PROG_PID=$!
trap 'kill $PROG_PID 2>/dev/null || true' EXIT

while true; do
  RUNNING=0
  for G in "${!GPU_PID[@]}"; do
    PID=${GPU_PID[$G]}
    if kill -0 "$PID" 2>/dev/null; then
      RUNNING=$((RUNNING + 1))
    else
      RC=0
      wait "$PID" 2>/dev/null || RC=$?
      if [[ $RC -ne 0 ]]; then
        echo "[error] gpu=$G workload=${GPU_WORKLOAD[$G]:-unknown} exit=$RC log=$LOGDIR/${GPU_WORKLOAD[$G]:-unknown}.log"
        FAILS=$((FAILS + 1))
      else
        WDONE=${GPU_WORKLOAD[$G]}
        if [[ -f "$DUMP_DIR/${WDONE}.windows.jsonl" ]]; then
          echo "[done] gpu=$G workload=$WDONE dump_rows=$(wc -l < "$DUMP_DIR/${WDONE}.windows.jsonl")"
        else
          echo "[done] gpu=$G workload=$WDONE dump_rows=0 (dump file missing)"
        fi
      fi
      unset "GPU_PID[$G]"
      unset "GPU_WORKLOAD[$G]"
      if [[ ${#QUEUE[@]} -gt 0 ]]; then
        W=${QUEUE[0]}
        QUEUE=("${QUEUE[@]:1}")
        launch_one "$W" "$G"
        RUNNING=$((RUNNING + 1))
      fi
    fi
  done
  if [[ $RUNNING -eq 0 && ${#QUEUE[@]} -eq 0 ]]; then
    break
  fi
  sleep 5
done

kill $PROG_PID 2>/dev/null || true
echo
echo "============ all done @ $(date +%H:%M:%S) ============"
echo "logs in $LOGDIR"
echo "dumps in $DUMP_DIR"

for W in "${WORKLOADS[@]}"; do
  if [[ -f "$DUMP_DIR/${W}.windows.jsonl" ]]; then
    echo "[summary] $W dump_rows=$(wc -l < "$DUMP_DIR/${W}.windows.jsonl")"
  else
    echo "[summary] $W dump_rows=0 missing"
  fi
done

if (( FAILS > 0 )); then
  echo "[final] failed=$FAILS"
  exit 1
fi
echo "[final] all done"
