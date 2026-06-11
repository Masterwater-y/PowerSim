#!/usr/bin/env bash
# 4卡 DDP 吞吐量扫参脚本。
# 默认对 bs / workers 做网格搜索，读取每次训练的 status.json，
# 以 samples_per_sec 作为主指标，输出排序后的结果。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
DEFAULT_DATA="${TAO_DATAGEN_ROOT:-${TAO_ROOT}/datagen}/tmp/06031920/final_balanced_50000000_pq"
if [[ ! -d "$DEFAULT_DATA" ]]; then
  DEFAULT_DATA="./data/final_balanced_50000000_pq_dedup_v10_3_ma16"
fi
DATA="${DATA:-$DEFAULT_DATA}"

CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0,1,2,3}"
IFS=',' read -r -a GPU_IDS <<< "$CUDA_VISIBLE_DEVICES"
NPROC="${#GPU_IDS[@]}"
TORCHRUN_BIN="${TORCHRUN_BIN:-/root/miniconda3/envs/yinhaolang/bin/torchrun}"
THREADS_PER_RANK="${THREADS_PER_RANK:-$(( 32 / NPROC > 0 ? 32 / NPROC : 1 ))}"

BENCH_DIR="${BENCH_DIR:-$HERE/ckpt/bench}"
STEPS="${STEPS:-120}"
LOG_EVERY="${LOG_EVERY:-20}"
BS_LIST="${BS_LIST:-1024 2048 3072 4096}"
WORKERS_LIST="${WORKERS_LIST:-4 8 12 16}"

mkdir -p "$BENCH_DIR"
RESULTS_TSV="$BENCH_DIR/results.tsv"
RESULTS_TXT="$BENCH_DIR/results.txt"

export CUDA_VISIBLE_DEVICES
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-$THREADS_PER_RANK}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-$THREADS_PER_RANK}"

echo -e "bs\tworkers\tsamples_per_sec\tstep_time_ms\tdata_time_ms\tcompute_time_ms\tstatus_path" >"$RESULTS_TSV"

run_one() {
  local bs="$1"
  local workers="$2"
  local tag="bs${bs}_w${workers}"
  local save_path="$BENCH_DIR/${tag}.pt"
  local status_path="$BENCH_DIR/${tag}.status.json"
  local log_path="$BENCH_DIR/${tag}.stdout.log"

  rm -f "$save_path" "$save_path".tmp "$status_path" "$status_path".tmp \
        "$BENCH_DIR/${tag}.best.pt" "$BENCH_DIR/${tag}.last.pt" "$log_path"

  echo "[bench] running bs=$bs workers=$workers ..."
  "$TORCHRUN_BIN" --standalone --nnodes=1 --nproc_per_node="$NPROC" -m ml.train \
    --data "$DATA" \
    --ctx 128 \
    --bs "$bs" \
    --steps "$STEPS" \
    --lr 3e-4 \
    --warmup 20 \
    --workers "$workers" \
    --gpus "$NPROC" \
    --bf16 \
    --save "$save_path" \
    --save-every 0 \
    --keep-last 1 \
    --log-every "$LOG_EVERY" \
    >"$log_path" 2>&1

  python3 - "$status_path" "$bs" "$workers" >>"$RESULTS_TSV" <<'PY'
import json
import sys

status_path, bs, workers = sys.argv[1:4]
with open(status_path) as f:
    data = json.load(f)

def fmt(x):
    return "na" if x is None else str(x)

print(
    "\t".join(
        [
            bs,
            workers,
            fmt(data.get("samples_per_sec")),
            fmt(data.get("step_time_ms")),
            fmt(data.get("data_time_ms")),
            fmt(data.get("compute_time_ms")),
            status_path,
        ]
    )
)
PY
}

for workers in $WORKERS_LIST; do
  for bs in $BS_LIST; do
    run_one "$bs" "$workers"
  done
done

python3 - "$RESULTS_TSV" <<'PY' | tee "$RESULTS_TXT"
import csv
import math
import sys

path = sys.argv[1]
with open(path) as f:
    rows = list(csv.DictReader(f, delimiter="\t"))

def to_float(v):
    try:
        return float(v)
    except Exception:
        return float("-inf")

rows.sort(key=lambda r: to_float(r["samples_per_sec"]), reverse=True)

print("rank  bs   workers  samples/s  step_ms  data_ms  compute_ms")
for idx, row in enumerate(rows, 1):
    print(
        f"{idx:<5} {row['bs']:<4} {row['workers']:<8} "
        f"{row['samples_per_sec']:<10} {row['step_time_ms']:<8} "
        f"{row['data_time_ms']:<8} {row['compute_time_ms']}"
    )

if rows:
    best = rows[0]
    print("")
    print(
        f"BEST bs={best['bs']} workers={best['workers']} "
        f"samples/s={best['samples_per_sec']} step_ms={best['step_time_ms']} "
        f"data_ms={best['data_time_ms']} compute_ms={best['compute_time_ms']}"
    )
PY
