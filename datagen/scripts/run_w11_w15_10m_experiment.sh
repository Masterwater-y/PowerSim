#!/usr/bin/env bash
# W11-W15 采集实验：每个 workload 先采集足量 trace，再从 ROI 稳定期抽取。
# 目标：每个 workload C-DUP 后约 10M，最终均衡抽样生成约 50M 数据集。
#
# 用法：
#   bash scripts/run_w11_w15_10m_experiment.sh [OUT_BASE]
#
# 监控：
#   tail -f <OUT_BASE>/logs/progress.log
#   column -t <OUT_BASE>/status.tsv
#
# 关键环境变量：
#   MIN_DEDUP_PER_WORKLOAD  每负载 dedup 后最小样本数，默认 10,000,000
#   DEDUP_SAFETY            稳定期 pack 输入安全系数，默认 1.05
#   FINAL_TARGET            最终均衡数据集总量，默认 50,000,000
#   MIN_FREE_GB             磁盘保护阈值，默认 300GB
#   HEARTBEAT_SEC           进度心跳间隔，默认 30s
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
PY="${PYTHON:-/root/.pyenv/versions/3.11.14/bin/python3.11}"

OUT_BASE="${1:-$REPO/tmp/exp_w11_w15_balanced_$(date +%Y%m%d_%H%M%S)}"
MIN_DEDUP_PER_WORKLOAD="${MIN_DEDUP_PER_WORKLOAD:-10000000}"
DEDUP_SAFETY="${DEDUP_SAFETY:-1.05}"
FINAL_TARGET="${FINAL_TARGET:-50000000}"
MIN_FREE_GB="${MIN_FREE_GB:-300}"
HEARTBEAT_SEC="${HEARTBEAT_SEC:-30}"
CTX_LEN="${CTX_LEN:-128}"
CTX_WARMUP_SKIP="${CTX_WARMUP_SKIP:-128}"
HEAD_SKIP="${HEAD_SKIP:-0.05}"
TAIL_SKIP="${TAIL_SKIP:-0.05}"
NUM_CORES="${NUM_CORES:-4}"

export LD_LIBRARY_PATH="/opt/gcc-11/lib64:/root/.pyenv/versions/3.8.0/lib:${LD_LIBRARY_PATH:-}"

LOG_DIR="$OUT_BASE/logs"
RUN_DIR="$OUT_BASE/runs"
JSONL_DIR="$OUT_BASE/jsonl_sampled_stable"
PACK_DIR="$OUT_BASE/packed_by_workload"
DEDUP_DIR="$OUT_BASE/dedup_by_workload"
FINAL_DIR="$OUT_BASE/final_balanced_${FINAL_TARGET}_pq"
PROGRESS="$LOG_DIR/progress.log"
STATUS="$OUT_BASE/status.tsv"
PLAN="$OUT_BASE/plan.tsv"

mkdir -p "$LOG_DIR" "$RUN_DIR" "$JSONL_DIR" "$PACK_DIR" "$DEDUP_DIR"
: > "$PROGRESS"
: > "$STATUS"
: > "$PLAN"

log() {
  local msg="$*"
  echo "[$(date '+%F %T')] $msg" | tee -a "$PROGRESS"
}

free_gb() {
  df -BG "$REPO" | awk 'NR==2 {gsub("G","",$4); print $4}'
}

check_space() {
  local phase="$1"
  local free
  free="$(free_gb)"
  log "[disk] phase=$phase free=${free}GB required>=${MIN_FREE_GB}GB out=$OUT_BASE"
  if (( free < MIN_FREE_GB )); then
    log "[STOP] disk free ${free}GB < ${MIN_FREE_GB}GB at phase=$phase"
    exit 3
  fi
}

count_trace_rows() {
  local out="$1"
  find "$out/tao_trace" -name '*.records.micro.jsonl' -type f -print0 2>/dev/null \
    | xargs -0 -r wc -l 2>/dev/null | awk 'END{print $1+0}'
}

monitor_pid() {
  local name="$1" phase="$2" pid="$3" out="$4" logfile="$5"
  local start now elapsed rows labels size free
  start="$(date +%s)"
  while kill -0 "$pid" 2>/dev/null; do
    sleep "$HEARTBEAT_SEC"
    now="$(date +%s)"
    elapsed=$((now - start))
    rows="$(count_trace_rows "$out")"
    labels="$(find "$out/tao_trace" -name '*.labels.micro.jsonl' -type f -print0 2>/dev/null | xargs -0 -r wc -l 2>/dev/null | awk 'END{print $1+0}')"
    size="$(du -sh "$out" 2>/dev/null | awk '{print $1}')"
    free="$(free_gb)"
    log "[progress] workload=$name phase=$phase elapsed=${elapsed}s records=$rows labels=$labels out_size=${size:-0} free=${free}GB pid=$pid"
    if [[ -s "$logfile" ]]; then
      tail -n 5 "$logfile" | sed "s/^/[tail:$name:$phase] /" | tee -a "$PROGRESS" >/dev/null
    fi
    if (( free < MIN_FREE_GB )); then
      log "[STOP] disk free ${free}GB < ${MIN_FREE_GB}GB; killing pid=$pid"
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
      exit 3
    fi
  done
}

run_monitored() {
  local name="$1" phase="$2" out="$3" logfile="$4"; shift 4
  log "[start] workload=$name phase=$phase cmd=$*"
  "$@" > "$logfile" 2>&1 &
  local pid=$!
  monitor_pid "$name" "$phase" "$pid" "$out" "$logfile" &
  local mon=$!
  local rc=0
  wait "$pid" || rc=$?
  kill "$mon" 2>/dev/null || true
  wait "$mon" 2>/dev/null || true
  log "[done] workload=$name phase=$phase rc=$rc"
  if (( rc != 0 )); then
    log "[FAIL] workload=$name phase=$phase logfile=$logfile"
    exit "$rc"
  fi
}

merge_mem_events() {
  local out="$1"
  cat "$out/tao_trace/"*.mem_events.jsonl 2>/dev/null \
    | python3 -c "
import json,sys
rows=[]
for ln in sys.stdin:
    s=ln.strip()
    if not s.startswith('{'): continue
    try: rows.append(json.loads(s))
    except Exception: pass
rows.sort(key=lambda r:(r.get('commit_tick',0), r.get('seq',0)))
for r in rows: print(json.dumps(r, separators=(',',':')))
" > "$out/mem_events.merged.jsonl"
}

# 预估来源：20260530 真实稳定期 500K ctx128 C-DUP 校准。
# keep rate 均接近 1，因此 pack 输入目标只比 MIN_DEDUP_PER_WORKLOAD 多 DEDUP_SAFETY。
pack_target_for() {
  local keep
  keep="$(keep_rate_for "$1")"
  "$PY" - <<PY
import math
keep=float("$keep")
target=int("$MIN_DEDUP_PER_WORKLOAD")
safety=float("$DEDUP_SAFETY")
print(math.ceil(target * safety / keep))
PY
}

keep_rate_for() {
  case "$1" in
    W11_stream_mix)   echo 0.999988 ;;
    W12_stencil2d)    echo 1.0 ;;
    W13_graph_walk)   echo 0.999998 ;;
    W14_branch_state) echo 1.0 ;;
    W15_indirect)     echo 0.999988 ;;
    *) echo 0.95 ;;
  esac
}

run_one() {
  local name="$1" bin="$2"; shift 2
  local args=("$@")
  local out="$RUN_DIR/$name"
  local jsonl="$JSONL_DIR/$name.jsonl"
  local pack="$PACK_DIR/$name"
  local dedup="$DEDUP_DIR/$name"
  local pack_target keep_rate est_dedup
  pack_target="$(pack_target_for "$name")"
  keep_rate="$(keep_rate_for "$name")"
  est_dedup="$($PY - <<PY
print(int($pack_target * $keep_rate))
PY
)"

  echo -e "$name\tkeep_rate_est=$keep_rate\tpack_input_target=$pack_target\test_dedup=$est_dedup\targs=${args[*]}" >> "$PLAN"
  log "[plan] workload=$name keep_rate_est=$keep_rate pack_input_target=$pack_target est_dedup=$est_dedup min_dedup=$MIN_DEDUP_PER_WORKLOAD"

  check_space "$name:begin"
  rm -rf "$out" "$pack" "$dedup" "$jsonl"
  mkdir -p "$out"
  echo -e "$(date '+%F %T')\t$name\tRUNNING\tgem5\targs=${args[*]}\tpack_target=$pack_target" >> "$STATUS"

  run_monitored "$name" "gem5" "$out" "$LOG_DIR/$name.gem5.log" \
    "$GEM5" --outdir="$out" "$CFG" \
      --cmd "$bin" --workload-args "${args[@]}" --num-cores "$NUM_CORES" \
      --require-roi

  local raw_records
  raw_records="$(count_trace_rows "$out")"
  log "[count] workload=$name raw_records=$raw_records pack_input_target=$pack_target"

  check_space "$name:refsim"
  log "[start] workload=$name phase=merge_mem_events"
  merge_mem_events "$out"
  log "[done] workload=$name phase=merge_mem_events"
  run_monitored "$name" "refsim" "$out" "$LOG_DIR/$name.refsim.log" \
    "$REFSIM" "$out/uarch_profile.json" "$out/mem_events.merged.jsonl" "$out/pred.jsonl"
  python3 "$COMPARE" "$out/mem_events.merged.jsonl" "$out/pred.jsonl" > "$out/compare.log" 2>&1 || true
  python3 "$COMPARE_I" "$out/mem_events.merged.jsonl" "$out/pred.jsonl" > "$out/compare.ifetch.log" 2>&1 || true
  python3 "$PMU" "$out/mem_events.merged.jsonl" "$out/pred.jsonl" --uarch-profile "$out/uarch_profile.json" > "$out/pmu.log" 2>&1 || true

  check_space "$name:sample_stable"
  run_monitored "$name" "sample_stable" "$out" "$LOG_DIR/$name.sample_stable.log" \
    "$PY" "$REPO/tools/sample_steady_balanced.py" \
      --run "$name=$out" --target "$pack_target" --out "$jsonl" \
      --head-skip "$HEAD_SKIP" --tail-skip "$TAIL_SKIP" \
      --context-warmup-skip "$CTX_WARMUP_SKIP"

  local samples
  samples="$(wc -l < "$jsonl")"
  log "[count] workload=$name stable_sampled=$samples target=$pack_target jsonl=$jsonl"
  if (( samples < pack_target )); then
    log "[FAIL] workload=$name stable_sampled=$samples < pack_target=$pack_target; increase workload args"
    exit 4
  fi

  check_space "$name:pack"
  run_monitored "$name" "pack" "$out" "$LOG_DIR/$name.pack.log" \
    "$PY" "$REPO/tools/pack_to_parquet.py" --in-jsonl "$jsonl" \
      --out-dir "$pack" --uarch-profile "$out/uarch_profile.json"

  check_space "$name:dedup"
  run_monitored "$name" "dedup" "$out" "$LOG_DIR/$name.dedup.log" \
    "$PY" "$REPO/tools/dedup_context.py" --in-dir "$pack" --out-dir "$dedup" --context-len "$CTX_LEN"

  local dedup_rows
  dedup_rows="$($PY - <<PY
import json
m=json.load(open("$dedup/meta.json"))
summary=m.get("dedup_summary", {})
if "$name" in summary:
    print(summary["$name"].get("out", 0))
else:
    print(m.get("dedup_output_rows", m.get("total_rows", m.get("n_total", 0))))
PY
)"
  log "[dedup] workload=$name input=$samples output=$dedup_rows keep_pct=$(awk -v a="$dedup_rows" -v b="$samples" 'BEGIN{if(b) printf "%.2f", a/b*100; else print "0"}')%"
  echo -e "$(date '+%F %T')\t$name\tDONE\tdedup\tinput=$samples output=$dedup_rows" >> "$STATUS"
  if (( dedup_rows < MIN_DEDUP_PER_WORKLOAD )); then
    log "[FAIL] workload=$name dedup_rows=$dedup_rows < MIN_DEDUP_PER_WORKLOAD=$MIN_DEDUP_PER_WORKLOAD; increase pack target or workload args"
    exit 5
  fi
}

log "[experiment] OUT_BASE=$OUT_BASE MIN_DEDUP_PER_WORKLOAD=$MIN_DEDUP_PER_WORKLOAD DEDUP_SAFETY=$DEDUP_SAFETY FINAL_TARGET=$FINAL_TARGET MIN_FREE_GB=$MIN_FREE_GB"
check_space "initial"

# 参数按 20260530 校准结果线性缩放，使 stable-cap 约 10.6M-11.0M。
# 这样 pack 输入约 10.5M，去重前不会比目标 10M 超出太多。
run_one W11_stream_mix   "$WL/mt_stream_mix/mt_stream_mix"                     4 47 256 1 11
run_one W12_stencil2d    "$WL/mt_stencil2d/mt_stencil2d"                       4 11 256 1 12
run_one W13_graph_walk   "$WL/mt_graph_walk/mt_graph_walk"                     4 1 640 1 13
run_one W14_branch_state "$WL/mt_branch_state_machine/mt_branch_state_machine" 4 11 64 1 14
run_one W15_indirect     "$WL/mt_indirect_dispatch/mt_indirect_dispatch"       4 11 256 1 15

check_space "final_sample"
run_monitored "ALL" "balanced_sample" "$OUT_BASE" "$LOG_DIR/final_balanced_sample.log" \
  "$PY" "$REPO/tools/sample_balanced_dedup_parquet.py" \
    --in-root "$DEDUP_DIR" --out-dir "$FINAL_DIR" --target-total "$FINAL_TARGET" --block-size 4096

log "[DONE] final_balanced_dataset=$FINAL_DIR"
log "[HOWTO] monitor: tail -f '$PROGRESS' ; status: column -t '$STATUS' ; plan: column -t '$PLAN'"
