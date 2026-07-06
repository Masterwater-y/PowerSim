#!/usr/bin/env bash
# 并行采集 LLMSim raw trace：
# - 可配置 NUM_CORES（1/4/8/16/32），默认 8
# - 目标每核约 TARGET_PER_CORE records.micro（默认 500k）
# - 自动先 probe，再按 probe 结果估算正式 scale
# - 正式产物目录前缀为 W_，可直接被 data/build_windows.py 消费

set -euo pipefail

ROOT=/data00/yinhaolang/LLMSim
REPO=/data00/yinhaolang/taogen
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
GEM5=${GEM5:-/data00/yinhaolang/gem5/build/X86_MESI_Three_Level/gem5.opt}
CFG=${CFG:-$REPO/configs/run_mt_mvp.py}
BIN_DIR=${BIN_DIR:-$ROOT/workloads/bin}
OUT_BASE=${OUT_BASE:-$ROOT/data/raw_8c_500k}

NUM_CORES=${NUM_CORES:-8}
TARGET_PER_CORE=${TARGET_PER_CORE:-500000}
MIN_ACCEPT_PER_CORE=${MIN_ACCEPT_PER_CORE:-450000}
MAX_ACCEPT_PER_CORE=${MAX_ACCEPT_PER_CORE:-0}
PROBE_SCALE=${PROBE_SCALE:-1}
PARALLEL=${PARALLEL:-2}
TIMEOUT_SECS=${TIMEOUT_SECS:-7200}
MAX_FINAL_ATTEMPTS=${MAX_FINAL_ATTEMPTS:-3}
SCALE_MARGIN_PCT=${SCALE_MARGIN_PCT:-110}
VALIDATE_WINDOWS=${VALIDATE_WINDOWS:-1}
SANITY_WINDOW=${SANITY_WINDOW:-256}
SANITY_STRIDE=${SANITY_STRIDE:-64}
PROGRESS_INTERVAL=${PROGRESS_INTERVAL:-60}
PROBE_STOP_REC=${PROBE_STOP_REC:-700000}
REUSE_PROBE_IF_SUFFICIENT=${REUSE_PROBE_IF_SUFFICIENT:-1}
# 1=Atomic 跑 init，首个 m5_work_begin 切到 O3+Ruby；trace 文件名变成
# board.processor.switch{i}.* 而不是 cores{i}.*（find_trace_file 双兼容）。
FF_ATOMIC=${FF_ATOMIC:-0}

export LD_LIBRARY_PATH="/data00/yinhaolang/LLMSim/data/_gem5libs:/opt/gcc-11.5.0/lib64:${LD_LIBRARY_PATH:-}"

if [[ ! -x "$GEM5" ]]; then
  echo "[error] gem5 不存在或不可执行: $GEM5" >&2
  exit 1
fi
if [[ ! -f "$CFG" ]]; then
  echo "[error] gem5 配置脚本不存在: $CFG" >&2
  exit 1
fi
if [[ ! -d "$BIN_DIR" ]]; then
  echo "[error] workload bin 目录不存在: $BIN_DIR" >&2
  echo "先执行: make -C $ROOT/workloads" >&2
  exit 1
fi

mkdir -p "$OUT_BASE"

ts() {
  date '+%F %T'
}

log() {
  echo "[$(ts)] $*"
}

cleanup_children() {
  jobs -pr | xargs -r kill >/dev/null 2>&1 || true
}
trap cleanup_children INT TERM

find_trace_file() {
  local out_dir=$1
  local core=$2
  local kind=$3
  local core2
  printf -v core2 "%02d" "$core"
  # 同时兼容三种 SimObject 路径前缀：
  #   - SimpleProcessor 多核:        board.processor.cores{i}.core.tao_trace.*
  #   - SimpleProcessor 10+ cores:   board.processor.cores0{i}.core.tao_trace.*
  #   - SimpleSwitchableProcessor:   board.processor.switch{i}.core.tao_trace.*
  #   - SimpleSwitchableProcessor 10+ cores may also be zero-padded:
  #                                    board.processor.switch0{i}.core.tao_trace.*
  #     （来自 --ff-atomic 模式，TaoTrace 挂在 _switchable_cores["switch"] 上）
  #   - SimpleProcessor 单核:        board.processor.cores.core.tao_trace.*
  #     （gem5 stdlib 在 num_cores==1 时省略数字后缀）
  if (( NUM_CORES == 1 )) && (( core == 0 )); then
    find "$out_dir/tao_trace" -maxdepth 1 -type f \
      \( -name "*cores.core.tao_trace.tao_trace.${kind}.micro.jsonl" \
         -o -name "*cores0.core*.${kind}.micro.jsonl" \
         -o -name "*switch.core.tao_trace.tao_trace.${kind}.micro.jsonl" \
         -o -name "*switch0.core*.${kind}.micro.jsonl" \) \
      2>/dev/null | sort | head -n 1
  else
    find "$out_dir/tao_trace" -maxdepth 1 -type f \
      \( -name "*cores${core}.core*.${kind}.micro.jsonl" \
         -o -name "*cores${core2}.core*.${kind}.micro.jsonl" \
         -o -name "*switch${core}.core*.${kind}.micro.jsonl" \
         -o -name "*switch${core2}.core*.${kind}.micro.jsonl" \) \
      2>/dev/null | sort | head -n 1
  fi
}

count_lines() {
  local f=$1
  wc -l < "$f" | tr -d ' '
}

min_rec_count() {
  local out_dir=$1
  local c
  local min=-1
  local f
  local n
  for ((c=0; c<NUM_CORES; c++)); do
    f=$(find_trace_file "$out_dir" "$c" "records")
    if [[ -z "$f" || ! -f "$f" ]]; then
      echo 0
      return 0
    fi
    n=$(count_lines "$f")
    if (( min < 0 || n < min )); then
      min=$n
    fi
  done
  echo "$min"
}

print_core_counts() {
  local out_dir=$1
  local c
  local rf lf rn ln
  for ((c=0; c<NUM_CORES; c++)); do
    rf=$(find_trace_file "$out_dir" "$c" "records")
    lf=$(find_trace_file "$out_dir" "$c" "labels")
    rn=0
    ln=0
    [[ -n "$rf" && -f "$rf" ]] && rn=$(count_lines "$rf")
    [[ -n "$lf" && -f "$lf" ]] && ln=$(count_lines "$lf")
    echo "core${c}: rec=${rn} lab=${ln}"
  done
}

core_count_summary() {
  local out_dir=$1
  local c
  local rf
  local rn
  local min=-1
  local parts=()
  for ((c=0; c<NUM_CORES; c++)); do
    rf=$(find_trace_file "$out_dir" "$c" "records")
    rn=0
    [[ -n "$rf" && -f "$rf" ]] && rn=$(count_lines "$rf")
    parts+=("c${c}=${rn}")
    if (( min < 0 || rn < min )); then
      min=$rn
    fi
  done
  echo "min=${min} ${parts[*]}"
}

dataset_compatible() {
  local out_dir=$1
  local c
  local rf lf rn ln
  for ((c=0; c<NUM_CORES; c++)); do
    rf=$(find_trace_file "$out_dir" "$c" "records")
    lf=$(find_trace_file "$out_dir" "$c" "labels")
    if [[ -z "$rf" || -z "$lf" || ! -f "$rf" || ! -f "$lf" ]]; then
      return 1
    fi
    rn=$(count_lines "$rf")
    ln=$(count_lines "$lf")
    if (( rn <= 0 || ln <= 0 )); then
      return 1
    fi
  done
  return 0
}

estimate_scale() {
  local base_scale=$1
  local observed_count=$2
  local est
  if (( observed_count <= 0 )); then
    echo $(( base_scale * 2 ))
    return 0
  fi
  est=$(( (base_scale * TARGET_PER_CORE * SCALE_MARGIN_PCT + observed_count * 100 - 1) / (observed_count * 100) ))
  if (( est < 1 )); then
    est=1
  fi
  echo "$est"
}

estimate_scale_down() {
  local base_scale=$1
  local observed_count=$2
  local est
  if (( observed_count <= 0 )); then
    echo "$base_scale"
    return 0
  fi
  est=$(( (base_scale * TARGET_PER_CORE + observed_count / 2) / observed_count ))
  if (( est < 1 )); then
    est=1
  fi
  if (( est >= base_scale && base_scale > 1 )); then
    est=$(( base_scale - 1 ))
  fi
  echo "$est"
}

within_accept_range() {
  local count=$1
  if (( count < MIN_ACCEPT_PER_CORE )); then
    return 1
  fi
  if (( MAX_ACCEPT_PER_CORE > 0 && count > MAX_ACCEPT_PER_CORE )); then
    return 1
  fi
  return 0
}

run_gem5() {
  local tag=$1
  local bin=$2
  local scale=$3
  local out_dir=$4
  local target_rec=${5:-0}
  local stop_rec=${6:-0}
  local gem5_pid
  local mon_pid
  local rc
  local start_ts
  local marker_early="$out_dir/.early_stop"
  local marker_timeout="$out_dir/.timed_out"
  rm -rf "$out_dir"
  mkdir -p "$out_dir"
  log "[$tag] start scale=$scale out=$out_dir"
  rm -f "$marker_early" "$marker_timeout"
  local -a extra_args=()
  if [[ "$FF_ATOMIC" == "1" ]]; then
    extra_args+=(--ff-atomic)
  fi
  set +e
  "$GEM5" --outdir="$out_dir" "$CFG" \
    --cmd "$bin" \
    --workload-args "$NUM_CORES" "$scale" 1 "$SEED" \
    --num-cores "$NUM_CORES" \
    --require-roi "${extra_args[@]}" > "$out_dir/gem5.log" 2>&1 &
  gem5_pid=$!
  start_ts=$(date +%s)
  (
    local last_summary=""
    while kill -0 "$gem5_pid" >/dev/null 2>&1; do
      local summary
      local min_rec
      local pct=""
      summary=$(core_count_summary "$out_dir")
      min_rec=${summary#min=}
      min_rec=${min_rec%% *}
      if (( target_rec > 0 )); then
        pct=$(( min_rec * 100 / target_rec ))
        summary="$summary target=${target_rec} pct=${pct}%"
      fi
      if [[ "$summary" != "$last_summary" ]]; then
        log "[$tag] progress $summary"
        last_summary="$summary"
      fi
      if (( stop_rec > 0 && min_rec >= stop_rec )); then
        log "[$tag] probe_stop reached min_rec=$min_rec stop_rec=$stop_rec"
        touch "$marker_early"
        kill -TERM "$gem5_pid" >/dev/null 2>&1 || true
        break
      fi
      if (( $(date +%s) - start_ts >= TIMEOUT_SECS )); then
        log "[$tag] timeout reached secs=$TIMEOUT_SECS"
        touch "$marker_timeout"
        kill -TERM "$gem5_pid" >/dev/null 2>&1 || true
        break
      fi
      sleep "$PROGRESS_INTERVAL"
    done
  ) &
  mon_pid=$!
  wait "$gem5_pid"
  rc=$?
  kill "$mon_pid" >/dev/null 2>&1 || true
  wait "$mon_pid" >/dev/null 2>&1 || true
  if [[ -f "$marker_early" ]]; then
    rc=0
    log "[$tag] stopped early after reaching probe threshold"
  elif [[ -f "$marker_timeout" ]]; then
    rc=124
  fi
  set -e
  log "[$tag] final_progress $(core_count_summary "$out_dir")"
  log "[$tag] exit=$rc"
  return "$rc"
}

write_collect_meta() {
  local out_dir=$1
  local name=$2
  local final_scale=$3
  local final_min=$4
  {
    echo "workload=$name"
    echo "num_cores=$NUM_CORES"
    echo "probe_scale=$PROBE_SCALE"
    echo "final_scale=$final_scale"
    echo "target_per_core=$TARGET_PER_CORE"
    echo "min_accept_per_core=$MIN_ACCEPT_PER_CORE"
    echo "max_accept_per_core=$MAX_ACCEPT_PER_CORE"
    echo "min_final_rec=$final_min"
    echo "ff_atomic=$FF_ATOMIC"
  } > "$out_dir/collect.meta"
  print_core_counts "$out_dir" > "$out_dir/counts.txt"
}

collect_one() {
  local bin=$1
  local name
  local probe_dir
  local final_dir
  local debug_dir
  local probe_min
  local scale
  local attempt
  local tmp_dir
  local final_min

  name=$(basename "$bin")
  probe_dir="$OUT_BASE/probe_${name}"
  final_dir="$OUT_BASE/W_${name}"
  debug_dir="$OUT_BASE/_debug_${name}"
  mkdir -p "$debug_dir"

  if [[ ! -x "$bin" ]]; then
    log "[${name}] skip: bin 不可执行: $bin"
    return 1
  fi

  if [[ -d "$final_dir" ]] && dataset_compatible "$final_dir"; then
    final_min=$(min_rec_count "$final_dir")
    if within_accept_range "$final_min"; then
      log "[${name}] reuse existing final dir, min_rec=$final_min"
      print_core_counts "$final_dir" > "$final_dir/counts.txt"
      return 0
    fi
  fi

  log "[${name}] phase=probe"
  if ! run_gem5 "probe:${name}" "$bin" "$PROBE_SCALE" "$probe_dir" 0 "$PROBE_STOP_REC"; then
    log "[${name}] probe failed"
    return 1
  fi

  if ! dataset_compatible "$probe_dir"; then
    log "[${name}] probe 输出不完整"
    print_core_counts "$probe_dir" | tee "$debug_dir/probe_counts.txt"
    return 1
  fi

  probe_min=$(min_rec_count "$probe_dir")
  print_core_counts "$probe_dir" | tee "$debug_dir/probe_counts.txt"
  log "[${name}] probe_min_rec=$probe_min"

  # probe 本身已达到目标时，默认可直接复用 probe 结果，避免再跑一次 final。
  # 但若 REUSE_PROBE_IF_SUFFICIENT=0，则 probe 只用于估算 scale，之后仍完整重跑 final。
  if (( REUSE_PROBE_IF_SUFFICIENT == 1 )) && within_accept_range "$probe_min"; then
    rm -rf "$final_dir"
    mv "$probe_dir" "$final_dir"
    write_collect_meta "$final_dir" "$name" "$PROBE_SCALE" "$probe_min"
    log "[${name}] success_from_probe -> $final_dir"
    return 0
  fi

  scale=$(estimate_scale "$PROBE_SCALE" "$probe_min")
  log "[${name}] phase=estimate estimated_scale=$scale"

  attempt=1
  while (( attempt <= MAX_FINAL_ATTEMPTS )); do
    tmp_dir="$OUT_BASE/_tmp_${name}_a${attempt}"
    log "[${name}] phase=final attempt=$attempt scale=$scale"
    set +e
    run_gem5 "final:${name}:a${attempt}" "$bin" "$scale" "$tmp_dir" "$TARGET_PER_CORE"
    rc=$?
    set -e

    if ! dataset_compatible "$tmp_dir"; then
      log "[${name}] final attempt $attempt 输出不完整 (rc=$rc)"
      print_core_counts "$tmp_dir" | tee "$debug_dir/final_attempt_${attempt}_counts.txt"
      mv "$tmp_dir" "$debug_dir/final_attempt_${attempt}" 2>/dev/null || true
      attempt=$(( attempt + 1 ))
      scale=$(( scale * 2 ))
      continue
    fi

    final_min=$(min_rec_count "$tmp_dir")
    print_core_counts "$tmp_dir" | tee "$debug_dir/final_attempt_${attempt}_counts.txt"
    log "[${name}] final attempt $attempt min_rec=$final_min rc=$rc"

    if (( rc != 0 )); then
      log "[${name}] final attempt $attempt gem5 abnormal exit (rc=$rc), reject trace"
      mv "$tmp_dir" "$debug_dir/final_attempt_${attempt}_rc${rc}" 2>/dev/null || true
      attempt=$(( attempt + 1 ))
      continue
    fi

    if within_accept_range "$final_min"; then
      rm -rf "$final_dir"
      mv "$tmp_dir" "$final_dir"
      write_collect_meta "$final_dir" "$name" "$scale" "$final_min"
      log "[${name}] success -> $final_dir"
      return 0
    fi

    mv "$tmp_dir" "$debug_dir/final_attempt_${attempt}" 2>/dev/null || true
    if (( final_min < MIN_ACCEPT_PER_CORE )); then
      scale=$(estimate_scale "$scale" "$final_min")
      log "[${name}] retry with larger scale=$scale"
    else
      scale=$(estimate_scale_down "$scale" "$final_min")
      log "[${name}] retry with smaller scale=$scale"
    fi
    attempt=$(( attempt + 1 ))
  done

  log "[${name}] failed after $MAX_FINAL_ATTEMPTS attempts"
  return 1
}

resolve_workloads() {
  local arg
  local path
  if (( $# > 0 )); then
    for arg in "$@"; do
      path="$BIN_DIR/$arg"
      if [[ ! -x "$path" ]]; then
        echo "[error] workload 不存在或不可执行: $path" >&2
        exit 1
      fi
      echo "$path"
    done
    return 0
  fi
  find "$BIN_DIR" -maxdepth 1 -type f -perm -111 | sort
}

validate_windows() {
  local sanity_out="$OUT_BASE/_windows_sanity"
  log "[sanity] phase=build_windows start"
  rm -rf "$sanity_out"
  mkdir -p "$sanity_out"
  "$PY" "$ROOT/data/build_windows.py" \
    --raw "$OUT_BASE" \
    --out "$sanity_out" \
    --window "$SANITY_WINDOW" \
    --stride "$SANITY_STRIDE"
  log "[sanity] phase=build_windows done -> $sanity_out/windows.jsonl"
}

main() {
  local -a bins
  local overall_rc=0
  local b

  mapfile -t bins < <(resolve_workloads "$@")
  if (( ${#bins[@]} == 0 )); then
    echo "[error] 没有可采集的 workload。" >&2
    exit 1
  fi

  log "ROOT=$ROOT"
  log "OUT_BASE=$OUT_BASE"
  log "NUM_CORES=$NUM_CORES TARGET_PER_CORE=$TARGET_PER_CORE MIN_ACCEPT_PER_CORE=$MIN_ACCEPT_PER_CORE"
  log "PARALLEL=$PARALLEL PROBE_SCALE=$PROBE_SCALE TIMEOUT_SECS=$TIMEOUT_SECS FF_ATOMIC=$FF_ATOMIC SEED=$SEED"
  log "workloads: $(printf '%s ' "${bins[@]##*/}")"

  for b in "${bins[@]}"; do
    while (( $(jobs -pr | wc -l) >= PARALLEL )); do
      if ! wait -n; then
        overall_rc=1
      fi
    done
    collect_one "$b" &
  done

  while (( $(jobs -pr | wc -l) > 0 )); do
    if ! wait -n; then
      overall_rc=1
    fi
  done

  if (( overall_rc != 0 )); then
    log "[summary] 至少一个 workload 采集失败"
    exit "$overall_rc"
  fi

  if [[ "$VALIDATE_WINDOWS" == "1" ]]; then
    validate_windows
  fi

  log "[summary] all done"
}

main "$@"
