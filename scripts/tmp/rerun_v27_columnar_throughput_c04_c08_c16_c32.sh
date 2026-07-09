#!/usr/bin/env bash
# Rerun optimized columnar-native eval throughput for c04/c08/c16/c32.
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TSim}
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
cd "$ROOT"

CKPT=${CKPT:-ckpt/v27_ss_tw5000_8l_t32768_bs1_20k_20260709_015028}
MAX_WINDOWS=${MAX_WINDOWS:-300}
MAX_LEN=${MAX_LEN:-32768}
GPU=${GPU:-}
GPUS=${GPUS:-${GPU:-0,1,2,3,4,5,6,7}}
QUERY_PLACEMENT=${QUERY_PLACEMENT:-tail_local}
PLANNER_STATE_SOURCE=${PLANNER_STATE_SOURCE:-pred}
INFER_DTYPE=${INFER_DTYPE:-bf16}
SDPA_BACKEND=${SDPA_BACKEND:-no_flash}
EVAL_CACHE_DIR=${EVAL_CACHE_DIR:-data/eval_columnar_cache}
CACHE_BUILD_IF_MISSING=${CACHE_BUILD_IF_MISSING:-1}
CACHE_BUILD_REBUILD=${CACHE_BUILD_REBUILD:-0}
CACHE_BUILD_PARALLEL=${CACHE_BUILD_PARALLEL:-16}
PROGRESS_EVERY=${PROGRESS_EVERY:-0}

DEFAULT_WORKLOADS=(W_stream)
read -r -a WORKLOADS <<< "${WORKLOADS:-${DEFAULT_WORKLOADS[*]}}"

TS=$(date +%Y%m%d_%H%M%S)
LOG_DIR=${LOG_DIR:-logs/tmp/v27_columnar_throughput_c04_c08_c16_c32_${TS}}
mkdir -p "$LOG_DIR" "$EVAL_CACHE_DIR"

declare -A RAW_BY_TAG=(
  [c04]=data/raw_trace_pool/activecore_eval/c04_seedB_infer17
  [c08]=data/raw_trace_pool/activecore_eval/c08_seedB_infer17
  [c16]=data/raw_trace_pool/activecore_eval/c16_seedB_infer17
  [c32]=data/raw_trace_pool/activecore_eval/c32_seedB_infer17
)
TAGS=(c04 c08 c16 c32)

echo "[meta] ROOT=$ROOT"
echo "[meta] CKPT=$CKPT"
echo "[meta] TAGS=${TAGS[*]}"
echo "[meta] WORKLOADS=${WORKLOADS[*]}"
echo "[meta] GPUS=$GPUS"
echo "[meta] MAX_WINDOWS=$MAX_WINDOWS"
echo "[meta] QUERY_PLACEMENT=$QUERY_PLACEMENT"
echo "[meta] EVAL_CACHE_DIR=$EVAL_CACHE_DIR"
echo "[meta] CACHE_BUILD_REBUILD=$CACHE_BUILD_REBUILD"
echo "[meta] CACHE_BUILD_PARALLEL=$CACHE_BUILD_PARALLEL"
echo "[meta] LOG_DIR=$LOG_DIR"
echo

for tag in "${TAGS[@]}"; do
  raw=${RAW_BY_TAG[$tag]}
  if [[ ! -d "$raw" ]]; then
    echo "[error] missing raw root for $tag: $raw" >&2
    exit 2
  fi
  for workload in "${WORKLOADS[@]}"; do
    if [[ ! -d "$raw/$workload/tao_trace" ]]; then
      echo "[error] missing trace dir: $raw/$workload/tao_trace" >&2
      exit 3
    fi
  done
done

build_cache_one() {
  local tag=$1
  local raw=$2
  local workload=$3
  PYTHONUNBUFFERED=1 "$PY" - \
    "$tag" "$raw" "$workload" "$EVAL_CACHE_DIR" \
    "$CACHE_BUILD_IF_MISSING" "$CACHE_BUILD_REBUILD" <<'PY'
import sys
from pathlib import Path
from types import SimpleNamespace

from eval.eval_quota_cycles import _eval_cache_path, load_eval_columnar_bundle

tag, raw, workload, cache_dir, build_if_missing, rebuild = sys.argv[1:]
build_if_missing = build_if_missing == "1"
rebuild = rebuild == "1"
trace_dir = f"{raw}/{workload}/tao_trace"
path, files, _sig = _eval_cache_path(trace_dir, 8192, cache_dir)
aligned = sum(1 for fp in files.values() if "aligned" in fp)
total = len(files)
exists = bool(path and Path(path).is_file() and Path(path).stat().st_size > 0)
print(
    f"[cache] {tag} {workload} aligned={aligned}/{total} "
    f"exists={exists} path={path}",
    flush=True,
)
if aligned != total or aligned == 0:
    raise SystemExit(f"aligned parquet incomplete: {tag} {workload}")
if rebuild or (build_if_missing and not exists):
    args = SimpleNamespace(
        eval_cache_mode="rebuild" if rebuild else "auto",
        load_max_rows_per_core=0,
        rd_window=8192,
        eval_cache_dir=cache_dir,
    )
    bundle = load_eval_columnar_bundle(trace_dir, args)
    print(
        f"[cache] built tag={tag} workload={workload} "
        f"cache_hit={bundle.get('cache_hit')} "
        f"build_s={float(bundle.get('build_s', 0.0)):.1f} "
        f"path={bundle.get('cache_path')}",
        flush=True,
    )
else:
    print(f"[cache] reuse tag={tag} workload={workload}", flush=True)
PY
}

build_caches_for_tag() {
  local tag=$1
  local raw=$2
  local -a queue=("${WORKLOADS[@]}")
  local -A cache_pid=()
  local -A cache_workload=()
  local fails=0
  local max_parallel=$CACHE_BUILD_PARALLEL
  if (( max_parallel < 1 )); then
    max_parallel=1
  fi

  launch_cache() {
    local workload=$1
    local log="$LOG_DIR/cache_${tag}_${workload}.log"
    echo "[cache-launch] tag=$tag workload=$workload -> $log"
    build_cache_one "$tag" "$raw" "$workload" > "$log" 2>&1 &
    cache_pid[$workload]=$!
    cache_workload[$workload]=$workload
  }

  echo
  echo "============ cache tag=$tag raw=$raw workloads=${#WORKLOADS[@]} parallel=$max_parallel ============"
  while [[ ${#queue[@]} -gt 0 && ${#cache_pid[@]} -lt $max_parallel ]]; do
    local workload=${queue[0]}
    queue=("${queue[@]:1}")
    launch_cache "$workload"
  done

  while true; do
    local running=0
    for key in "${!cache_pid[@]}"; do
      local pid=${cache_pid[$key]}
      local workload=${cache_workload[$key]}
      if kill -0 "$pid" 2>/dev/null; then
        running=$((running + 1))
      else
        local rc=0
        wait "$pid" 2>/dev/null || rc=$?
        local log="$LOG_DIR/cache_${tag}_${workload}.log"
        if [[ $rc -ne 0 ]]; then
          echo "[cache-error] tag=$tag workload=$workload exit=$rc log=$log"
          tail -n 20 "$log" 2>/dev/null || true
          fails=$((fails + 1))
        else
          local line
          line=$(tail -n 1 "$log" 2>/dev/null || true)
          echo "[cache-done] tag=$tag workload=$workload ${line}"
        fi
        unset "cache_pid[$key]"
        unset "cache_workload[$key]"
        if [[ ${#queue[@]} -gt 0 ]]; then
          local next_workload=${queue[0]}
          queue=("${queue[@]:1}")
          launch_cache "$next_workload"
          running=$((running + 1))
        fi
      fi
    done
    if [[ $running -eq 0 && ${#queue[@]} -eq 0 ]]; then
      break
    fi
    sleep 2
  done

  if [[ $fails -ne 0 ]]; then
    echo "[cache-error] tag=$tag failed cache builds=$fails"
    return 1
  fi
  echo "============ cache done tag=$tag @ $(date +%H:%M:%S) ============"
}

cache_status_report() {
  "$PY" - "$EVAL_CACHE_DIR" "${WORKLOADS[@]}" <<'PY'
import sys
from pathlib import Path
from eval.eval_quota_cycles import _eval_cache_path

cache_dir, *workloads = sys.argv[1:]
roots = {
    "c04": "data/raw_trace_pool/activecore_eval/c04_seedB_infer17",
    "c08": "data/raw_trace_pool/activecore_eval/c08_seedB_infer17",
    "c16": "data/raw_trace_pool/activecore_eval/c16_seedB_infer17",
    "c32": "data/raw_trace_pool/activecore_eval/c32_seedB_infer17",
}
for tag, raw in roots.items():
    ok = []
    missing = []
    for workload in workloads:
        path, _files, _sig = _eval_cache_path(
            f"{raw}/{workload}/tao_trace", 8192, cache_dir)
        exists = bool(path and Path(path).is_file() and Path(path).stat().st_size > 0)
        (ok if exists else missing).append(workload)
    print(f"[cache-summary] {tag}: {len(ok)}/{len(workloads)} ready", flush=True)
    if missing:
        print(
            f"[cache-summary] {tag} missing: {' '.join(missing)}",
            flush=True,
        )
PY
}

IFS=',' read -r -a GPU_LIST <<< "$GPUS"

run_one_tag() {
  local tag=$1
  local raw=$2
  local -a queue=("${WORKLOADS[@]}")
  local -A gpu_pid=()
  local -A gpu_workload=()
  local fails=0
  local progress_pid=""

  launch_one() {
    local gpu=$1
    local workload=$2
    local log="$LOG_DIR/${tag}_${workload}.log"
    echo "[launch] tag=$tag gpu=$gpu workload=$workload -> $log"
    /usr/bin/time -v env CUDA_VISIBLE_DEVICES="$gpu" \
      "$PY" eval/eval_quota_cycles.py \
        --raw-root "$raw" \
        --workload "$workload" \
        --ckpt "$CKPT" \
        --max-len "$MAX_LEN" \
        --max-windows "$MAX_WINDOWS" \
        --query-placement "$QUERY_PLACEMENT" \
        --planner-state-source "$PLANNER_STATE_SOURCE" \
        --device cuda \
        --infer-dtype "$INFER_DTYPE" \
        --sdpa-backend "$SDPA_BACKEND" \
        --eval-cache-mode auto \
        --eval-cache-dir "$EVAL_CACHE_DIR" \
      > "$log" 2>&1 &
    gpu_pid[$gpu]=$!
    gpu_workload[$gpu]=$workload
  }

  progress_loop() {
    while true; do
      sleep "$PROGRESS_EVERY"
      echo
      echo "============ progress tag=$tag @ $(date +%H:%M:%S) ============"
      for workload in "${WORKLOADS[@]}"; do
        local log="$LOG_DIR/${tag}_${workload}.log"
        [[ -f "$log" ]] || continue
        local line
        line=$(grep -E "^\s*\[$workload\] " "$log" 2>/dev/null | tail -n 1 || true)
        if [[ -z "$line" ]]; then
          line=$(tail -n 1 "$log" 2>/dev/null || true)
        fi
        printf "%-4s %-28s %s\n" "$tag" "$workload" "$line"
      done
    done
  }

  echo
  echo "============ run tag=$tag raw=$raw workloads=${#WORKLOADS[@]} ============"
  if [[ "$PROGRESS_EVERY" != "0" ]]; then
    progress_loop &
    progress_pid=$!
  fi

  for gpu in "${GPU_LIST[@]}"; do
    [[ ${#queue[@]} -gt 0 ]] || break
    local workload=${queue[0]}
    queue=("${queue[@]:1}")
    launch_one "$gpu" "$workload"
  done

  while true; do
    local running=0
    for gpu in "${!gpu_pid[@]}"; do
      local pid=${gpu_pid[$gpu]}
      if kill -0 "$pid" 2>/dev/null; then
        running=$((running + 1))
      else
        local rc=0
        wait "$pid" 2>/dev/null || rc=$?
        if [[ $rc -ne 0 ]]; then
          echo "[error] tag=$tag gpu=$gpu workload=${gpu_workload[$gpu]:-unknown} exit=$rc log=$LOG_DIR/${tag}_${gpu_workload[$gpu]:-unknown}.log"
          fails=$((fails + 1))
        else
          echo "[done-one] tag=$tag gpu=$gpu workload=${gpu_workload[$gpu]}"
        fi
        unset "gpu_pid[$gpu]"
        unset "gpu_workload[$gpu]"
        if [[ ${#queue[@]} -gt 0 ]]; then
          local workload=${queue[0]}
          queue=("${queue[@]:1}")
          launch_one "$gpu" "$workload"
          running=$((running + 1))
        fi
      fi
    done
    if [[ $running -eq 0 && ${#queue[@]} -eq 0 ]]; then
      break
    fi
    sleep 5
  done

  if [[ -n "$progress_pid" ]]; then
    kill "$progress_pid" 2>/dev/null || true
    wait "$progress_pid" 2>/dev/null || true
  fi
  if [[ $fails -ne 0 ]]; then
    echo "[error] tag=$tag failed workloads=$fails"
    return 1
  fi
  echo "============ done tag=$tag @ $(date +%H:%M:%S) ============"
}

for tag in "${TAGS[@]}"; do
  raw=${RAW_BY_TAG[$tag]}
  build_caches_for_tag "$tag" "$raw"
  run_one_tag "$tag" "$raw"
done

cache_status_report

summary="$LOG_DIR/throughput_summary.tsv"
"$PY" - "$LOG_DIR" "$summary" <<'PY'
import glob
import os
import re
import sys

log_dir, summary_path = sys.argv[1:3]

progress_re = re.compile(
    r"\[(W_[^\]]+)\]\s+(\d+)\s+windows\s+\((\d+)s\).*?uops/s=([0-9.]+)"
)
timing_re = re.compile(
    r"timing\(avg/window\):\s+build=([0-9.]+)ms\s+encode=([0-9.]+)ms\s+"
    r"tensor=([0-9.]+)ms\s+forward=([0-9.]+)ms\s+update=([0-9.]+)ms\s+"
    r"total=([0-9.]+)ms"
)

rows = []
for fp in sorted(glob.glob(os.path.join(log_dir, "c*_W_*.log"))):
    name = os.path.basename(fp)[:-4]
    tag, workload = name.split("_", 1)
    text = open(fp, errors="replace").read()
    progress = progress_re.findall(text)
    timings = timing_re.findall(text)
    cache_hit = "[eval-cache] hit" in text
    native_on = "columnar_native=on" in text
    if progress:
        workload2, windows, seconds, uops_s = progress[-1]
    else:
        workload2, windows, seconds, uops_s = workload, "0", "0", "nan"
    if timings:
        build, encode, tensor, forward, update, total = timings[-1]
    else:
        build = encode = tensor = forward = update = total = "nan"
    rows.append({
        "tag": tag,
        "workload": workload2,
        "windows": windows,
        "seconds": seconds,
        "uops_s": uops_s,
        "cache_hit": str(cache_hit),
        "columnar_native": str(native_on),
        "build_ms": build,
        "encode_ms": encode,
        "tensor_ms": tensor,
        "forward_ms": forward,
        "update_ms": update,
        "total_ms": total,
        "log": fp,
    })

cols = [
    "tag", "workload", "windows", "seconds", "uops_s", "cache_hit",
    "columnar_native", "build_ms", "encode_ms", "tensor_ms", "forward_ms",
    "update_ms", "total_ms", "log",
]
with open(summary_path, "w") as f:
    f.write("\t".join(cols) + "\n")
    for row in rows:
        f.write("\t".join(str(row[c]) for c in cols) + "\n")

print("[summary] throughput")
print("\t".join(cols[:-1]))
for row in rows:
    print("\t".join(str(row[c]) for c in cols[:-1]))
print(f"[summary] tsv={summary_path}")
PY

echo
echo "[done] logs=$LOG_DIR"
