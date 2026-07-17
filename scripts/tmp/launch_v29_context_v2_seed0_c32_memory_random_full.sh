#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT=${ROOT:-/data00/yinhaolang/TCSim}
cd "$PROJECT_ROOT"

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
CKPT=${CKPT:-$PROJECT_ROOT/ckpt/tcsim_v29_packed3_100m_8gpu_60k/best.pt}
MANIFEST=${MANIFEST:-$PROJECT_ROOT/data/v29_global_time_dataset/manifest.json}
GPU=${GPU:-2}
RUN_TAG=${RUN_TAG:-v29_context_v2_seed0_c32_memory_random_full_20260717_184636}
OUT_DIR=${OUT:-$PROJECT_ROOT/logs/$RUN_TAG}
LAUNCH_LOG=${LAUNCH_LOG:-$PROJECT_ROOT/logs/tmp/$RUN_TAG.log}
PID_FILE=${PID_FILE:-$PROJECT_ROOT/scripts/tmp/$RUN_TAG.pid}

[[ -x "$PY" ]] || { echo "[v29-c32-full][ERROR] missing Python: $PY" >&2; exit 2; }
[[ -f "$CKPT" ]] || { echo "[v29-c32-full][ERROR] missing checkpoint: $CKPT" >&2; exit 2; }
[[ -f "$MANIFEST" ]] || { echo "[v29-c32-full][ERROR] missing manifest: $MANIFEST" >&2; exit 2; }

if [[ -f "$PID_FILE" ]]; then
  existing_pid=$(<"$PID_FILE")
  if [[ "$existing_pid" =~ ^[0-9]+$ ]] && kill -0 "$existing_pid" 2>/dev/null; then
    echo "[v29-c32-full][ERROR] already running: pid=$existing_pid" >&2
    exit 2
  fi
fi

mkdir -p "$OUT_DIR" "$PROJECT_ROOT/logs/tmp" "$PROJECT_ROOT/scripts/tmp"

command=(env CUDA_VISIBLE_DEVICES="$GPU" "$PY" scripts/infer_v29.py \
  --ckpt "$CKPT" \
  --manifest "$MANIFEST" \
  --splits seed0_inference \
  --out "$OUT_DIR" \
  --mode free \
  --device cuda \
  --amp-dtype bf16 \
  --sdpa-backend auto \
  --core-counts 32 \
  --workloads W_v28_memory_random_mlp \
  --seeds 0 \
  --max-traces 1 \
  --max-oracle-samples 0 \
  --max-free-steps 0 \
  --target-stride 256 \
  --min-step-cycles 4 \
  --max-step-cycles 1024 \
  --max-no-progress-steps 64 \
  --progress-every 100 \
  --fail-fast)

if [[ "${FOREGROUND:-0}" == "1" ]]; then
  printf '%s\n' "$$" >"$PID_FILE"
  printf '[v29-c32-full] foreground pid=%s gpu=%s log=%s\n' \
    "$$" "$GPU" "$LAUNCH_LOG"
  "${command[@]}" 2>&1 | tee "$LAUNCH_LOG"
  exit "${PIPESTATUS[0]}"
fi

nohup "${command[@]}" >"$LAUNCH_LOG" 2>&1 &

pid=$!
printf '%s\n' "$pid" >"$PID_FILE"
printf '[v29-c32-full] pid=%s\n' "$pid"
printf '[v29-c32-full] gpu=%s workload=W_v28_memory_random_mlp seed=0 cores=32\n' "$GPU"
printf '[v29-c32-full] out=%s\n' "$OUT_DIR"
printf '[v29-c32-full] log=%s\n' "$LAUNCH_LOG"
printf '[v29-c32-full] report=%s/report.txt\n' "$OUT_DIR"
