#!/usr/bin/env bash
# v7 数据集采集 driver：17 workload × {1c, 4c, 8c} × seed=0 = 51 trace。
# 每个 (workload, ncore) 调一次 collect_parallel_500k.sh，输出独立 OUT_BASE：
#   data/raw_v7_seedA/c01/W_<name>
#   data/raw_v7_seedA/c04/W_<name>
#   data/raw_v7_seedA/c08/W_<name>
# 同步实时打印总进度到 stdout + logs/collect_v7_progress.log。

set +e
set -uo pipefail

ROOT=/data00/yinhaolang/LLMSim
LOGDIR=$ROOT/logs
mkdir -p "$LOGDIR"

PARALLEL=${PARALLEL:-2}              # 同时跑几个 gem5 进程
SEED=${SEED:-0}
TARGET_PER_CORE=${TARGET_PER_CORE:-500000}
MIN_ACCEPT_PER_CORE=${MIN_ACCEPT_PER_CORE:-450000}
TIMEOUT_SECS=${TIMEOUT_SECS:-7200}
PROGRESS_TICK=${PROGRESS_TICK:-30}   # 每多少秒打一次总览

WORKLOADS=(
  ads_ctr ads_ranking_proxy branch_storm chase_dram compute_int
  false_sharing feed_ranking fp_compute_dense fp_lite graph_recall_proxy
  indirect int_div interest_graph_recall mlp_light phased_mix
  search_index_proxy stream
)
NCORES_LIST=(1 4 8)

OUT_ROOT=$ROOT/data/raw_v7_seedA
mkdir -p "$OUT_ROOT"

STATE_DIR=$ROOT/data/raw_v7_seedA/_progress
mkdir -p "$STATE_DIR"
rm -f "$STATE_DIR"/task_*.state

# 任务清单
TASKS=()
for wl in "${WORKLOADS[@]}"; do
  for nc in "${NCORES_LIST[@]}"; do
    TASKS+=("$wl|$nc")
  done
done

TOTAL=${#TASKS[@]}
echo "[v7] total tasks=$TOTAL workloads=${#WORKLOADS[@]} ncores=${NCORES_LIST[*]} PARALLEL=$PARALLEL SEED=$SEED"
echo "[v7] OUT_ROOT=$OUT_ROOT"
echo "[v7] log per-task -> $LOGDIR/collect_v7_<wl>_c<nc>.log"

run_one() {
  local wl=$1
  local nc=$2
  local cdir
  printf -v cdir "c%02d" "$nc"
  local out_base="$OUT_ROOT/$cdir"
  local state="$STATE_DIR/task_${wl}_${cdir}.state"
  local log="$LOGDIR/collect_v7_${wl}_${cdir}.log"
  echo "running" > "$state"
  mkdir -p "$out_base"
  # PROBE_SCALE 按 (8/nc) 反比缩放：1c→8, 4c→2, 8c→1，
  # 保证每核 probe records ~ 同一量级，避免 1c probe 不够命中目标。
  local probe_scale=$(( 8 / nc ))
  (( probe_scale < 1 )) && probe_scale=1
  # collect_parallel_500k.sh 内部 PARALLEL=1，因为外层 driver 控并发
  OUT_BASE="$out_base" \
  NUM_CORES="$nc" \
  PARALLEL=1 \
  SEED="$SEED" \
  TARGET_PER_CORE="$TARGET_PER_CORE" \
  MIN_ACCEPT_PER_CORE="$MIN_ACCEPT_PER_CORE" \
  TIMEOUT_SECS="$TIMEOUT_SECS" \
  VALIDATE_WINDOWS=0 \
  PROBE_SCALE="$probe_scale" \
  PROBE_STOP_REC=$(( TARGET_PER_CORE * 110 / 100 )) \
  bash "$ROOT/scripts/collect_parallel_500k.sh" "$wl" \
      > "$log" 2>&1
  rc=$?
  if [[ $rc -eq 0 ]]; then
    echo "done" > "$state"
  else
    echo "fail rc=$rc" > "$state"
  fi
  return $rc
}

# 启动并行池
launch_progress_monitor() {
  (
    while true; do
      sleep "$PROGRESS_TICK"
      local done_n=0 run_n=0 fail_n=0 pend_n=0
      for f in "$STATE_DIR"/task_*.state; do
        [[ -f "$f" ]] || continue
        s=$(cat "$f")
        case "$s" in
          done) done_n=$((done_n+1));;
          running) run_n=$((run_n+1));;
          fail*) fail_n=$((fail_n+1));;
        esac
      done
      pend_n=$(( TOTAL - done_n - run_n - fail_n ))
      printf '[%s] progress done=%d run=%d fail=%d pend=%d / total=%d\n' \
             "$(date '+%F %T')" "$done_n" "$run_n" "$fail_n" "$pend_n" "$TOTAL"
    done
  ) &
  MON_PID=$!
}

cleanup() {
  kill "$MON_PID" >/dev/null 2>&1 || true
  jobs -pr | xargs -r kill >/dev/null 2>&1 || true
}
trap cleanup INT TERM EXIT

launch_progress_monitor

overall_rc=0
for task in "${TASKS[@]}"; do
  while (( $(jobs -pr | wc -l) >= PARALLEL )); do
    if ! wait -n; then overall_rc=1; fi
  done
  IFS='|' read -r wl nc <<<"$task"
  run_one "$wl" "$nc" &
done

while (( $(jobs -pr | wc -l) > 0 )); do
  if ! wait -n; then overall_rc=1; fi
done

# 最终汇总
done_n=0; fail_n=0
for f in "$STATE_DIR"/task_*.state; do
  s=$(cat "$f")
  case "$s" in done) done_n=$((done_n+1));; fail*) fail_n=$((fail_n+1));; esac
done

echo
echo "[v7] FINAL: done=$done_n fail=$fail_n total=$TOTAL"
if (( fail_n > 0 )); then
  echo "[v7] failed tasks:"
  for f in "$STATE_DIR"/task_*.state; do
    s=$(cat "$f")
    [[ "$s" == fail* ]] && echo "  - $(basename "$f" .state): $s"
  done
fi
exit "$overall_rc"
