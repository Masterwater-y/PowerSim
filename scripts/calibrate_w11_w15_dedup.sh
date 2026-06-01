#!/usr/bin/env bash
# W11-W15 ctx128 C-DUP 保留率校准实验。
# 目的：先真实测每个 workload 的稳定期样本去重保留率，再反推正式实验所需 pack_input_target。
#
# 用法：
#   bash scripts/calibrate_w11_w15_dedup.sh [OUT_BASE]
#
# 输出：
#   <OUT_BASE>/calibration.tsv
#   字段：workload, stable_sampled, dedup_rows, keep_rate, recommended_pack_target
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$(cd "$REPO/.." && pwd)"

GEM5="${GEM5:-$ROOT/gem5/build/X86_MESI_Three_Level/gem5.opt}"
CFG="$REPO/configs/run_mt_mvp.py"
REFSIM="$REPO/mesi_ref_sim/build/mesi_ref_sim"
WL="$REPO/workloads"
PY="${PYTHON:-/root/.pyenv/versions/3.11.14/bin/python3.11}"

OUT_BASE="${1:-$REPO/tmp/calib_w11_w15_dedup_$(date +%Y%m%d_%H%M%S)}"
CALIB_SAMPLE_TARGET="${CALIB_SAMPLE_TARGET:-500000}"
MIN_DEDUP_PER_WORKLOAD="${MIN_DEDUP_PER_WORKLOAD:-2000000}"
SAFETY="${SAFETY:-1.25}"
MIN_FREE_GB="${MIN_FREE_GB:-150}"
HEARTBEAT_SEC="${HEARTBEAT_SEC:-30}"
CTX_LEN="${CTX_LEN:-128}"
HEAD_SKIP="${HEAD_SKIP:-0.05}"
TAIL_SKIP="${TAIL_SKIP:-0.05}"
CTX_WARMUP_SKIP="${CTX_WARMUP_SKIP:-128}"
NUM_CORES="${NUM_CORES:-4}"
START_FROM="${START_FROM:-W11_stream_mix}"
APPEND_CALIB="${APPEND_CALIB:-0}"

export LD_LIBRARY_PATH="/opt/gcc-11/lib64:/root/.pyenv/versions/3.8.0/lib:${LD_LIBRARY_PATH:-}"

LOG_DIR="$OUT_BASE/logs"
RUN_DIR="$OUT_BASE/runs"
JSONL_DIR="$OUT_BASE/jsonl"
PACK_DIR="$OUT_BASE/packed"
DEDUP_DIR="$OUT_BASE/dedup"
PROGRESS="$LOG_DIR/progress.log"
CALIB="$OUT_BASE/calibration.tsv"
mkdir -p "$LOG_DIR" "$RUN_DIR" "$JSONL_DIR" "$PACK_DIR" "$DEDUP_DIR"
if [[ "$APPEND_CALIB" == "1" && -f "$PROGRESS" ]]; then
  :
else
  : > "$PROGRESS"
fi
if [[ "$APPEND_CALIB" == "1" && -f "$CALIB" ]]; then
  :
else
  echo -e "workload\tstable_sampled\tdedup_rows\tkeep_rate\trecommended_pack_target\targs" > "$CALIB"
fi

log() { echo "[$(date '+%F %T')] $*" | tee -a "$PROGRESS"; }
free_gb() { df -BG "$REPO" | awk 'NR==2 {gsub("G","",$4); print $4}'; }
check_space() {
  local free; free="$(free_gb)"
  log "[disk] phase=$1 free=${free}GB required>=${MIN_FREE_GB}GB out=$OUT_BASE"
  if (( free < MIN_FREE_GB )); then
    log "[STOP] disk free ${free}GB < ${MIN_FREE_GB}GB"
    exit 3
  fi
}
count_trace_rows() {
  local out="$1"
  find "$out/tao_trace" -name '*.records.micro.jsonl' -type f -print0 2>/dev/null | xargs -0 -r wc -l 2>/dev/null | awk 'END{print $1+0}'
}
monitor_pid() {
  local name="$1" phase="$2" pid="$3" out="$4" logfile="$5"
  local start now elapsed rows size free
  start="$(date +%s)"
  while kill -0 "$pid" 2>/dev/null; do
    sleep "$HEARTBEAT_SEC"
    now="$(date +%s)"; elapsed=$((now-start))
    rows="$(count_trace_rows "$out")"
    size="$(du -sh "$out" 2>/dev/null | awk '{print $1}')"
    free="$(free_gb)"
    log "[progress] workload=$name phase=$phase elapsed=${elapsed}s records=$rows out_size=${size:-0} free=${free}GB pid=$pid"
    if [[ -s "$logfile" ]]; then tail -n 4 "$logfile" | sed "s/^/[tail:$name:$phase] /" | tee -a "$PROGRESS" >/dev/null; fi
    if (( free < MIN_FREE_GB )); then kill "$pid" 2>/dev/null || true; exit 3; fi
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
  kill "$mon" 2>/dev/null || true; wait "$mon" 2>/dev/null || true
  log "[done] workload=$name phase=$phase rc=$rc"
  if (( rc != 0 )); then log "[FAIL] logfile=$logfile"; exit "$rc"; fi
}
merge_mem_events() {
  local out="$1"
  cat "$out/tao_trace/"*.mem_events.jsonl 2>/dev/null | python3 -c '
import json,sys
rows=[]
for ln in sys.stdin:
    s=ln.strip()
    if not s.startswith("{"): continue
    try: rows.append(json.loads(s))
    except Exception: pass
rows.sort(key=lambda r:(r.get("commit_tick",0), r.get("seq",0)))
for r in rows: print(json.dumps(r, separators=(",",":")))
' > "$out/mem_events.merged.jsonl"
}
recommended_target() {
  "$PY" - <<PY
import math
keep=float("$1")
min_dedup=int("$MIN_DEDUP_PER_WORKLOAD")
safety=float("$SAFETY")
if keep <= 0:
    print(0)
else:
    print(math.ceil(min_dedup * safety / keep))
PY
}
calib_one() {
  local name="$1" bin="$2"; shift 2
  local args=("$@")
  local out="$RUN_DIR/$name" jsonl="$JSONL_DIR/$name.jsonl" pack="$PACK_DIR/$name" dedup="$DEDUP_DIR/$name"
  rm -rf "$out" "$pack" "$dedup" "$jsonl"; mkdir -p "$out"
  check_space "$name:begin"
  run_monitored "$name" gem5 "$out" "$LOG_DIR/$name.gem5.log" \
    "$GEM5" --outdir="$out" "$CFG" --cmd "$bin" --workload-args "${args[@]}" --num-cores "$NUM_CORES" --require-roi
  log "[count] workload=$name raw_records=$(count_trace_rows "$out") calib_target=$CALIB_SAMPLE_TARGET"
  merge_mem_events "$out"
  run_monitored "$name" refsim "$out" "$LOG_DIR/$name.refsim.log" \
    "$REFSIM" "$out/uarch_profile.json" "$out/mem_events.merged.jsonl" "$out/pred.jsonl"
  run_monitored "$name" sample_stable "$out" "$LOG_DIR/$name.sample.log" \
    "$PY" "$REPO/tools/sample_steady_balanced.py" --run "$name=$out" --target "$CALIB_SAMPLE_TARGET" --out "$jsonl" \
      --head-skip "$HEAD_SKIP" --tail-skip "$TAIL_SKIP" --context-warmup-skip "$CTX_WARMUP_SKIP"
  local stable_sampled; stable_sampled="$(wc -l < "$jsonl")"
  run_monitored "$name" pack "$out" "$LOG_DIR/$name.pack.log" \
    "$PY" "$REPO/tools/pack_to_parquet.py" --in-jsonl "$jsonl" --out-dir "$pack"
  run_monitored "$name" dedup "$out" "$LOG_DIR/$name.dedup.log" \
    "$PY" "$REPO/tools/dedup_context.py" --in-dir "$pack" --out-dir "$dedup" --context-len "$CTX_LEN"
  local dedup_rows keep rec
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
  keep="$($PY - <<PY
print($dedup_rows / max($stable_sampled, 1))
PY
)"
  rec="$(recommended_target "$keep")"
  echo -e "$name\t$stable_sampled\t$dedup_rows\t$keep\t$rec\t${args[*]}" | tee -a "$CALIB"
  log "[calib] workload=$name stable=$stable_sampled dedup=$dedup_rows keep=$keep recommended_pack_target=$rec"
}

should_run() {
  local name="$1"
  case "$START_FROM" in
    W11_stream_mix) return 0 ;;
    W12_stencil2d) [[ "$name" != "W11_stream_mix" ]] ;;
    W13_graph_walk|W3) [[ "$name" != "W11_stream_mix" && "$name" != "W12_stencil2d" ]] ;;
    W14_branch_state) [[ "$name" == "W14_branch_state" || "$name" == "W15_indirect" ]] ;;
    W15_indirect) [[ "$name" == "W15_indirect" ]] ;;
    *) echo "unknown START_FROM=$START_FROM" >&2; exit 2 ;;
  esac
}

log "[calibration] OUT_BASE=$OUT_BASE CALIB_SAMPLE_TARGET=$CALIB_SAMPLE_TARGET MIN_DEDUP_PER_WORKLOAD=$MIN_DEDUP_PER_WORKLOAD SAFETY=$SAFETY"
log "[calibration] START_FROM=$START_FROM APPEND_CALIB=$APPEND_CALIB"
check_space initial
# 参数目标是让每个 workload 稳定期容量 >= CALIB_SAMPLE_TARGET，避免校准过重。
should_run W11_stream_mix   && calib_one W11_stream_mix   "$WL/mt_stream_mix/mt_stream_mix"                     4 3 256 1 11
should_run W12_stencil2d    && calib_one W12_stencil2d    "$WL/mt_stencil2d/mt_stencil2d"                       4 5 256 1 12
should_run W13_graph_walk   && calib_one W13_graph_walk   "$WL/mt_graph_walk/mt_graph_walk"                     4 1 1024 1 13
should_run W14_branch_state && calib_one W14_branch_state "$WL/mt_branch_state_machine/mt_branch_state_machine" 4 5 64 1 14
should_run W15_indirect     && calib_one W15_indirect     "$WL/mt_indirect_dispatch/mt_indirect_dispatch"       4 5 256 1 15
log "[DONE] calibration=$CALIB"
