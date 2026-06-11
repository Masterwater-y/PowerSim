#!/usr/bin/env bash
# W11-W15 并行采集流水线（gem5 → refsim → sample_stable → pack → dedup）。
# 每个 workload 在独立子进程内串行跑完 5 个 phase；主进程是 watchdog，
# 周期性打印各 workload 当前 phase / 经过时间 / records 行数 / pid / 状态。
#
# 用法:
#   bash scripts/run_w11_w15_parallel_collect.sh [OUT_BASE]
#
# Smoke 模式（强烈建议先跑一次确认并行调度可行）:
#   SMOKE=1 bash scripts/run_w11_w15_parallel_collect.sh
#   - 仅跑 W11 + W12 两个 workload（验证并行 watchdog 表格）
#   - workload args 缩小到 ~50K records
#   - calib target = 10K，pack/dedup 很快
#   - 总耗时预计 < 5 min
#
# 全量模式:
#   bash scripts/run_w11_w15_parallel_collect.sh
#   - 5 个 workload 全并行
#   - 与 run_w11_w15_10m_experiment.sh 同样的 args（每 workload ~10M dedup）
#
# 关键环境变量:
#   PARALLEL                  最大并发 workload 数（默认 5；smoke=2）
#   WORKLOADS                 空格分隔覆盖默认列表
#   MIN_DEDUP_PER_WORKLOAD    全量模式 dedup 后下限（默认 10000000；smoke 10000）
#   FINAL_SAMPLE              是否在所有 workload 完成后跑 balanced final（默认 0；smoke 不跑）
#   FINAL_TARGET              balanced final 总行数（默认 50000000）
#   HEARTBEAT_SEC             watchdog 心跳（默认 15s）
#   MIN_FREE_GB               磁盘保护阈值（默认 300；smoke=20）
#   CTX_LEN / CTX_WARMUP_SKIP / HEAD_SKIP / TAIL_SKIP / NUM_CORES
#
# 监控:
#   watch -n 5 "column -t '$OUT_BASE/dashboard.tsv'"
#   tail -f '$OUT_BASE/logs/progress.log'
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$(cd "$REPO/.." && pwd)"

GEM5="${GEM5:-$ROOT/gem5/build/X86_MESI_Three_Level/gem5.opt}"
CFG="$REPO/configs/run_mt_mvp.py"
REFSIM="$REPO/mesi_ref_sim/build/mesi_ref_sim"
COMPARE="$REPO/mesi_ref_sim/scripts/compare_oracle.py"
COMPARE_I="$REPO/mesi_ref_sim/scripts/compare_ifetch.py"
PMU="$REPO/mesi_ref_sim/scripts/pmu_report.py"
WL="$REPO/workloads"
PY="${PYTHON:-$(command -v python3.11 || command -v python3)}"
PY_BINDIR="$(cd "$(dirname "$PY")" && pwd)"
PY_LIBDIR="$("$PY" - <<'PY'
import sysconfig
print(sysconfig.get_config_var("LIBDIR") or "")
PY
)"

SMOKE="${SMOKE:-0}"
if [[ "$SMOKE" == "1" ]]; then
  : "${OUT_BASE:=$REPO/tmp/smoke_parallel_$(date +%Y%m%d_%H%M%S)}"
  : "${PARALLEL:=2}"
  : "${WORKLOADS:=W11_stream_mix W12_stencil2d}"
  : "${MIN_DEDUP_PER_WORKLOAD:=8000}"
  : "${CALIB_SAMPLE_TARGET:=10000}"
  : "${MIN_FREE_GB:=20}"
  : "${HEARTBEAT_SEC:=5}"
  : "${FINAL_SAMPLE:=0}"
else
  : "${OUT_BASE:=${1:-$REPO/tmp/exp_w11_w15_parallel_$(date +%Y%m%d_%H%M%S)}}"
  : "${PARALLEL:=5}"
  : "${WORKLOADS:=W11_stream_mix W12_stencil2d W13_graph_walk W14_branch_state W15_indirect}"
  : "${MIN_DEDUP_PER_WORKLOAD:=10000000}"
  : "${MIN_FREE_GB:=300}"
  : "${HEARTBEAT_SEC:=15}"
  : "${FINAL_SAMPLE:=1}"
fi
: "${DEDUP_SAFETY:=1.05}"
: "${FINAL_TARGET:=50000000}"
: "${CTX_LEN:=128}"
: "${CTX_WARMUP_SKIP:=128}"
: "${HEAD_SKIP:=0.05}"
: "${TAIL_SKIP:=0.05}"
: "${NUM_CORES:=4}"
: "${FEATURE_GENERATOR:=oracle}"
: "${TAO_CPU_SIM_ROOT:=${TAO_ROOT}}"

# 允许 $1 仅在非 SMOKE 模式下覆盖 OUT_BASE
if [[ "$SMOKE" != "1" && $# -ge 1 ]]; then
  OUT_BASE="$1"
fi

pick_libstdcpp_dir() {
  local cand
  local match
  for cand in \
    "${LIBSTDCXX_DIR:-}" \
    "${CONDA_PREFIX:-}/lib" \
    "/root/miniconda3/envs/yinhaolang/lib" \
    "/root/miniconda3/lib" \
    "/usr/lib/x86_64-linux-gnu"
  do
    [[ -n "$cand" && -f "$cand/libstdc++.so.6" ]] || continue
    match="$(strings "$cand/libstdc++.so.6" 2>/dev/null | grep 'GLIBCXX_3\.4\.30' | tail -n 1 || true)"
    if [[ -n "$match" ]]; then
      echo "$cand"
      return 0
    fi
  done
  return 1
}

prepend_ld_path() {
  local dir="$1"
  [[ -n "$dir" && -d "$dir" ]] || return 0
  case ":${LD_LIBRARY_PATH:-}:" in
    *":$dir:"*) ;;
    *) export LD_LIBRARY_PATH="$dir${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" ;;
  esac
}

sanitize_ld_path() {
  local raw="${LD_LIBRARY_PATH:-}" out="" item
  local old_ifs="$IFS"
  IFS=':'
  for item in $raw; do
    [[ -n "$item" ]] || continue
    case "$item" in
      /opt/gcc-11*|/opt/gcc-11.5.0/*|/root/.pyenv/versions/3.8.0/*)
        continue
        ;;
    esac
    if [[ -z "$out" ]]; then
      out="$item"
    else
      out="$out:$item"
    fi
  done
  IFS="$old_ifs"
  export LD_LIBRARY_PATH="$out"
}

sanitize_ld_path
LIBSTDCXX_DIR="$(pick_libstdcpp_dir || true)"
prepend_ld_path "$LIBSTDCXX_DIR"
prepend_ld_path "$PY_LIBDIR"
export PATH="$PY_BINDIR:$PATH"

LOG_DIR="$OUT_BASE/logs"
RUN_DIR="$OUT_BASE/runs"
# V10 方案 B：sample 阶段直接出 flat parquet（每 workload 一个 .parquet）。
# JSONL_DIR 变量名沿用，但其下放的是 .parquet 文件。
JSONL_DIR="$OUT_BASE/jsonl_sampled_stable"
PACK_DIR="$OUT_BASE/packed_by_workload"
DEDUP_DIR="$OUT_BASE/dedup_by_workload"
FINAL_DIR="$OUT_BASE/final_balanced_${FINAL_TARGET}_pq"
STATE_DIR="$OUT_BASE/state"
PROGRESS="$LOG_DIR/progress.log"
DASHBOARD="$OUT_BASE/dashboard.tsv"
STATUS="$OUT_BASE/status.tsv"
mkdir -p "$LOG_DIR" "$RUN_DIR" "$JSONL_DIR" "$PACK_DIR" "$DEDUP_DIR" "$STATE_DIR"
: > "$PROGRESS"
: > "$STATUS"
: > "$DASHBOARD"

log() {
  local msg="$*"
  echo "[$(date '+%F %T')] $msg" >> "$PROGRESS"
  echo "[$(date '+%F %T')] $msg"
}

free_gb() {
  df -BG "$REPO" | awk 'NR==2 {gsub("G","",$4); print $4}'
}

# ========================================================== workload args
# 全量模式：与 run_w11_w15_10m_experiment.sh 一致
# Smoke 模式：args 缩小，ROI 期 µop ~50K，sample target=10K，total ~3 min/workload
declare -A WL_BIN
declare -A WL_ARGS
WL_BIN[W11_stream_mix]="$WL/mt_stream_mix/mt_stream_mix"
WL_BIN[W12_stencil2d]="$WL/mt_stencil2d/mt_stencil2d"
WL_BIN[W13_graph_walk]="$WL/mt_graph_walk/mt_graph_walk"
WL_BIN[W14_branch_state]="$WL/mt_branch_state_machine/mt_branch_state_machine"
WL_BIN[W15_indirect]="$WL/mt_indirect_dispatch/mt_indirect_dispatch"

if [[ "$SMOKE" == "1" ]]; then
  WL_ARGS[W11_stream_mix]="4 1 64 1 11"
  WL_ARGS[W12_stencil2d]="4 1 64 1 12"
  WL_ARGS[W13_graph_walk]="4 1 64 1 13"
  WL_ARGS[W14_branch_state]="4 1 32 1 14"
  WL_ARGS[W15_indirect]="4 1 64 1 15"
else
  WL_ARGS[W11_stream_mix]="4 47 256 1 11"
  WL_ARGS[W12_stencil2d]="4 11 256 1 12"
  WL_ARGS[W13_graph_walk]="4 1 640 1 13"
  WL_ARGS[W14_branch_state]="4 11 64 1 14"
  WL_ARGS[W15_indirect]="4 11 256 1 15"
fi

# ========================================================== dedup keep rate（与串行脚本一致；smoke 用宽松值）
keep_rate_for() {
  case "$1" in
    W11_stream_mix)   echo 0.999988 ;;
    W12_stencil2d)    echo 0.94 ;;
    W13_graph_walk)   echo 0.999998 ;;
    W14_branch_state) echo 1.0 ;;
    W15_indirect)     echo 0.999988 ;;
    *) echo 0.95 ;;
  esac
}

pack_target_for() {
  local keep
  keep="$(keep_rate_for "$1")"
  if [[ "$SMOKE" == "1" ]]; then
    echo "${CALIB_SAMPLE_TARGET:-10000}"
    return
  fi
  "$PY" - <<PY
import math
keep=float("$keep")
target=int("$MIN_DEDUP_PER_WORKLOAD")
safety=float("$DEDUP_SAFETY")
print(math.ceil(target * safety / keep))
PY
}

# ========================================================== state helper
# 状态文件 $STATE_DIR/<W>.log 每行: TS\tEVENT\tPHASE[\trc=N]
# EVENT 取值: start | end | fail
write_state() {
  local w="$1" event="$2" phase="$3" extra="${4:-}"
  printf '%s\t%s\t%s\t%s\n' "$(date +%s)" "$event" "$phase" "$extra" \
    >> "$STATE_DIR/$w.log"
}

# ========================================================== single workload pipeline (子进程)
# 该函数在 fork 后的子 shell 内执行；任何阶段失败 -> exit 非 0，
# 主进程通过 wait 捕获并标记 FAIL。
run_pipeline() {
  local name="$1"
  local bin="${WL_BIN[$name]}"
  local args=( ${WL_ARGS[$name]} )
  local out="$RUN_DIR/$name"
  # V10 方案 B：sample 阶段直出 parquet（仍放在 JSONL_DIR 下，文件后缀 .parquet）
  local sample_pq="$JSONL_DIR/$name.parquet"
  local pack="$PACK_DIR/$name"
  local dedup="$DEDUP_DIR/$name"
  local pack_target keep_rate
  pack_target="$(pack_target_for "$name")"
  keep_rate="$(keep_rate_for "$name")"

  rm -rf "$out" "$pack" "$dedup" "$sample_pq"
  mkdir -p "$out"

  echo -e "$(date '+%F %T')\t$name\tRUNNING\tgem5\targs=${args[*]}\tpack_target=$pack_target" >> "$STATUS"

  # ---- gem5
  write_state "$name" start "gem5"
  if "$GEM5" --outdir="$out" "$CFG" \
       --cmd "$bin" --workload-args "${args[@]}" \
       --num-cores "$NUM_CORES" --require-roi \
       > "$LOG_DIR/$name.gem5.log" 2>&1 ; then
    write_state "$name" end "gem5"
  else
    local rc=$?
    write_state "$name" fail "gem5" "rc=$rc"
    return 10
  fi

  # ---- merge mem_events
  write_state "$name" start "merge_events"
  cat "$out/tao_trace/"*.mem_events.jsonl 2>/dev/null | "$PY" -c "
import json,sys
rows=[]
for ln in sys.stdin:
    s=ln.strip()
    if not s.startswith('{'): continue
    try: rows.append(json.loads(s))
    except Exception: pass
rows.sort(key=lambda r:(r.get('commit_tick',0), r.get('seq',0)))
for r in rows: print(json.dumps(r, separators=(',',':')))
" > "$out/mem_events.merged.jsonl" 2>> "$LOG_DIR/$name.merge.log" || {
    local rc=$?
    write_state "$name" fail "merge_events" "rc=$rc"
    return 11
  }
  write_state "$name" end "merge_events"

  # ---- refsim
  write_state "$name" start "refsim"
  if "$REFSIM" "$out/uarch_profile.json" \
       "$out/mem_events.merged.jsonl" "$out/pred.jsonl" \
       > "$LOG_DIR/$name.refsim.log" 2>&1 ; then
    write_state "$name" end "refsim"
  else
    local rc=$?
    write_state "$name" fail "refsim" "rc=$rc"
    return 12
  fi
  "$PY" "$COMPARE"   "$out/mem_events.merged.jsonl" "$out/pred.jsonl" \
    > "$out/compare.log" 2>&1 || true
  "$PY" "$COMPARE_I" "$out/mem_events.merged.jsonl" "$out/pred.jsonl" \
    > "$out/compare.ifetch.log" 2>&1 || true
  "$PY" "$PMU"       "$out/mem_events.merged.jsonl" "$out/pred.jsonl" \
    --uarch-profile "$out/uarch_profile.json" \
    > "$out/pmu.log" 2>&1 || true

  # ---- sample_stable
  write_state "$name" start "sample_stable"
  if "$PY" "$REPO/tools/sample_steady_balanced.py" \
       --run "$name=$out" --target "$pack_target" --out "$sample_pq" \
       --head-skip "$HEAD_SKIP" --tail-skip "$TAIL_SKIP" \
       --context-warmup-skip "$CTX_WARMUP_SKIP" \
       --feature-generator "$FEATURE_GENERATOR" \
       --tao-cpu-sim-root "$TAO_CPU_SIM_ROOT" \
       > "$LOG_DIR/$name.sample.log" 2>&1 ; then
    :
  else
    local rc=$?
    write_state "$name" fail "sample_stable" "rc=$rc"
    return 13
  fi
  # V10 方案 B：sample 直出 parquet，行数从 parquet metadata 读
  local samples
  samples="$("$PY" -c "import pyarrow.parquet as pq; print(pq.read_metadata('$sample_pq').num_rows)" 2>/dev/null || echo 0)"
  write_state "$name" end "sample_stable" "rows=$samples"
  if (( samples < pack_target )); then
    log "[FAIL] $name stable_sampled=$samples < pack_target=$pack_target"
    write_state "$name" fail "sample_stable" "samples=$samples"
    return 14
  fi

  # ---- pack
  write_state "$name" start "pack"
  if "$PY" "$REPO/tools/pack_to_parquet.py" \
       --from-parquet "$sample_pq" --out-dir "$pack" \
       --uarch-profile "$out/uarch_profile.json" \
       > "$LOG_DIR/$name.pack.log" 2>&1 ; then
    write_state "$name" end "pack"
  else
    local rc=$?
    write_state "$name" fail "pack" "rc=$rc"
    return 15
  fi

  # ---- dedup
  write_state "$name" start "dedup"
  if "$PY" "$REPO/tools/dedup_context.py" \
       --in-dir "$pack" --out-dir "$dedup" --context-len "$CTX_LEN" \
       --latency-bins 16 \
       > "$LOG_DIR/$name.dedup.log" 2>&1 ; then
    :
  else
    local rc=$?
    write_state "$name" fail "dedup" "rc=$rc"
    return 16
  fi
  local dedup_rows
  dedup_rows="$($PY - <<PY
import json
m=json.load(open('$dedup/meta.json'))
summary=m.get('dedup_summary', {})
if '$name' in summary:
    print(summary['$name'].get('out', 0))
else:
    print(m.get('dedup_output_rows', m.get('total_rows', m.get('n_total', 0))))
PY
)"
  write_state "$name" end "dedup" "rows=$dedup_rows"
  echo -e "$(date '+%F %T')\t$name\tDONE\tdedup\tinput=$samples\toutput=$dedup_rows" >> "$STATUS"
  if (( dedup_rows < MIN_DEDUP_PER_WORKLOAD )); then
    log "[FAIL] $name dedup_rows=$dedup_rows < MIN_DEDUP_PER_WORKLOAD=$MIN_DEDUP_PER_WORKLOAD"
    write_state "$name" fail "dedup_lowrows" "rows=$dedup_rows"
    return 17
  fi

  write_state "$name" end "ALL"
  return 0
}

# ========================================================== watchdog
# 主进程后台循环：扫描每个 workload 的 state log + records 行数，
# 渲染 dashboard.tsv（列：workload / phase / status / elapsed_s /
# records / labels / out_size / pid / rc）。
render_dashboard() {
  local now="$(date +%s)"
  {
    printf 'WORKLOAD\tPHASE\tSTATUS\tELAPSED_S\tRECORDS\tLABELS\tOUT_SIZE\tPID\tRC\n'
    for w in $WORKLOADS; do
      local pid="${WL_PID[$w]:-}"
      local state_file="$STATE_DIR/$w.log"
      local last="" phase="-" status="pending" elapsed=0 ts="" event="" extra=""
      if [[ -s "$state_file" ]]; then
        last="$(tail -n 1 "$state_file")"
        ts="$(awk -F'\t' '{print $1}' <<< "$last")"
        event="$(awk -F'\t' '{print $2}' <<< "$last")"
        phase="$(awk -F'\t' '{print $3}' <<< "$last")"
        extra="$(awk -F'\t' '{print $4}' <<< "$last")"
        if [[ -n "$ts" ]]; then
          elapsed=$((now - ts))
        fi
        case "$event" in
          start) status="running" ;;
          end)   status="done" ;;
          fail)  status="FAIL($extra)" ;;
        esac
      fi
      local records="-" labels="-" size="-"
      if [[ -d "$RUN_DIR/$w/tao_trace" ]]; then
        records="$(find "$RUN_DIR/$w/tao_trace" -maxdepth 1 -name '*.records.micro.jsonl' -print0 2>/dev/null \
                   | xargs -0 -r wc -l 2>/dev/null | awk 'END{print $1+0}')"
        labels="$(find "$RUN_DIR/$w/tao_trace" -maxdepth 1 -name '*.labels.micro.jsonl' -print0 2>/dev/null \
                  | xargs -0 -r wc -l 2>/dev/null | awk 'END{print $1+0}')"
      fi
      if [[ -d "$RUN_DIR/$w" ]]; then
        size="$(du -sh "$RUN_DIR/$w" 2>/dev/null | awk '{print $1}')"
      fi
      local rc="${WL_RC[$w]:--}"
      local pid_disp="${pid:--}"
      if [[ -n "$pid" ]] && ! kill -0 "$pid" 2>/dev/null; then
        pid_disp="(exited)"
      fi
      printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$w" "$phase" "$status" "$elapsed" "$records" "$labels" "${size:-0}" "$pid_disp" "$rc"
    done
    local free; free="$(free_gb)"
    printf '%s\t%s\t%s\t-\t-\t-\t-\t-\t-\n' "DISK_FREE_GB" "-" "${free}GB" 
  } > "$DASHBOARD"
}

# ========================================================== main
log "[experiment] OUT_BASE=$OUT_BASE PARALLEL=$PARALLEL SMOKE=$SMOKE"
log "[experiment] WORKLOADS=$WORKLOADS"
log "[experiment] MIN_DEDUP_PER_WORKLOAD=$MIN_DEDUP_PER_WORKLOAD HEARTBEAT_SEC=$HEARTBEAT_SEC"
log "[env] PY=$PY"
log "[env] PY_LIBDIR=${PY_LIBDIR:-} LIBSTDCXX_DIR=${LIBSTDCXX_DIR:-}"
log "[disk] free=$(free_gb)GB required>=${MIN_FREE_GB}GB"
if (( $(free_gb) < MIN_FREE_GB )); then
  log "[STOP] free disk < ${MIN_FREE_GB}GB"
  exit 3
fi

declare -A WL_PID
declare -A WL_RC

# 简单的 PARALLEL 节流：当 active >= PARALLEL 时等待任一子进程退出。
active_count() {
  local n=0
  for w in "${!WL_PID[@]}"; do
    local p="${WL_PID[$w]}"
    if [[ -n "$p" ]] && kill -0 "$p" 2>/dev/null; then
      n=$((n+1))
    fi
  done
  echo "$n"
}

# 启动子进程
for w in $WORKLOADS; do
  while (( $(active_count) >= PARALLEL )); do
    sleep 1
  done
  log "[spawn] workload=$w args='${WL_ARGS[$w]}'"
  ( run_pipeline "$w"; exit $? ) &
  WL_PID[$w]=$!
  log "[spawn] workload=$w pid=${WL_PID[$w]}"
done

# watchdog 循环：直到所有子进程退出
log "[watchdog] start; dashboard=$DASHBOARD heartbeat=${HEARTBEAT_SEC}s"
while :; do
  render_dashboard
  # 控制台打印 dashboard（覆盖式不便阅读，这里直接 cat）
  echo "===== [$(date '+%F %T')] dashboard (cat $DASHBOARD) ====="
  column -t "$DASHBOARD" || cat "$DASHBOARD"
  # 检查活动进程
  cur_active=$(active_count)
  if (( cur_active == 0 )); then
    break
  fi
  sleep "$HEARTBEAT_SEC"
done

# 收集 rc
overall=0
for w in $WORKLOADS; do
  pid="${WL_PID[$w]:-}"
  if [[ -n "$pid" ]]; then
    if wait "$pid" 2>/dev/null; then
      WL_RC[$w]=0
    else
      WL_RC[$w]=$?
      overall=1
    fi
  else
    WL_RC[$w]="(no_pid)"
    overall=1
  fi
  log "[done] workload=$w rc=${WL_RC[$w]}"
done
render_dashboard
log "===== final dashboard ====="
column -t "$DASHBOARD" || cat "$DASHBOARD"

if (( overall != 0 )); then
  log "[FAIL] some workloads failed; check $LOG_DIR/<W>.<phase>.log and $STATE_DIR/<W>.log"
  exit 2
fi

# ---- 可选：balanced final sampling
if [[ "$FINAL_SAMPLE" == "1" ]]; then
  log "[final_sample] start (target=$FINAL_TARGET)"
  if "$PY" "$REPO/tools/sample_balanced_dedup_parquet.py" \
        --in-root "$DEDUP_DIR" --out-dir "$FINAL_DIR" \
        --target-total "$FINAL_TARGET" --block-size 4096 \
        > "$LOG_DIR/final_balanced_sample.log" 2>&1 ; then
    log "[DONE] final_balanced_dataset=$FINAL_DIR"
  else
    log "[FAIL] final_balanced_sample (see $LOG_DIR/final_balanced_sample.log)"
    exit 4
  fi
else
  log "[skip] final balanced sampling (FINAL_SAMPLE=$FINAL_SAMPLE); per-workload dedup is at $DEDUP_DIR"
fi

log "[ALL DONE]"
log "  state logs : $STATE_DIR/<W>.log"
log "  dashboard  : $DASHBOARD"
log "  progress   : $PROGRESS"
log "  status.tsv : $STATUS"
