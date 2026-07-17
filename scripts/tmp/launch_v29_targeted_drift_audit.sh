#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT=${ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
cd "$PROJECT_ROOT"

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
CKPT=${CKPT:-$PROJECT_ROOT/ckpt/tcsim_v29_packed3_100m_8gpu_60k/best.pt}
MANIFEST=${MANIFEST:-$PROJECT_ROOT/data/v29_global_time_dataset/manifest.json}
RUN_STAMP=$(date +%Y%m%d_%H%M%S)
RUN_TAG=v29_targeted_oracle_drift_s256_seed0_${RUN_STAMP}
OUT_ROOT=${OUT_ROOT:-$PROJECT_ROOT/logs/$RUN_TAG}
PID_FILE=${PID_FILE:-$PROJECT_ROOT/scripts/tmp/$RUN_TAG.pid}
MEMORY_LOG=${MEMORY_LOG:-$PROJECT_ROOT/logs/tmp/$RUN_TAG.memory_seq_c32.log}
REDIS8_LOG=${REDIS8_LOG:-$PROJECT_ROOT/logs/tmp/$RUN_TAG.redis_heldout_c08.log}
REDIS32_LOG=${REDIS32_LOG:-$PROJECT_ROOT/logs/tmp/$RUN_TAG.redis_heldout_c32.log}

[[ -x "$PY" ]] || { echo "[v29-drift][ERROR] missing Python: $PY" >&2; exit 2; }
[[ -f "$CKPT" ]] || { echo "[v29-drift][ERROR] missing checkpoint: $CKPT" >&2; exit 2; }
[[ -f "$MANIFEST" ]] || { echo "[v29-drift][ERROR] missing manifest: $MANIFEST" >&2; exit 2; }

mkdir -p "$OUT_ROOT" "$PROJECT_ROOT/logs/tmp" "$PROJECT_ROOT/scripts/tmp"

common_args=(
  --ckpt "$CKPT"
  --manifest "$MANIFEST"
  --splits seed0_inference,development_heldout
  --mode free
  --device cuda
  --amp-dtype bf16
  --sdpa-backend auto
  --max-traces 1
  --max-free-steps 0
  --target-stride 256
  --min-step-cycles 4
  --max-step-cycles 1024
  --max-no-progress-steps 64
  --oracle-drift-diagnostics
  --progress-every 200
  --fail-fast
)

nohup env CUDA_VISIBLE_DEVICES=2 "$PY" scripts/infer_v29.py \
  "${common_args[@]}" \
  --out "$OUT_ROOT/memory_seq_c32" \
  --core-counts 32 \
  --workloads W_v28_memory_seq_moderate \
  >"$MEMORY_LOG" 2>&1 &
memory_pid=$!

nohup env CUDA_VISIBLE_DEVICES=3 "$PY" scripts/infer_v29.py \
  "${common_args[@]}" \
  --out "$OUT_ROOT/redis_heldout_c08" \
  --core-counts 8 \
  --workloads W_v28_redis_heldout \
  >"$REDIS8_LOG" 2>&1 &
redis8_pid=$!

nohup env CUDA_VISIBLE_DEVICES=4 "$PY" scripts/infer_v29.py \
  "${common_args[@]}" \
  --out "$OUT_ROOT/redis_heldout_c32" \
  --core-counts 32 \
  --workloads W_v28_redis_heldout \
  >"$REDIS32_LOG" 2>&1 &
redis32_pid=$!

printf 'memory_seq_c32=%s\nredis_heldout_c08=%s\nredis_heldout_c32=%s\n' \
  "$memory_pid" "$redis8_pid" "$redis32_pid" > "$PID_FILE"

printf '[v29-drift] pids memory_seq_c32=%s redis_c08=%s redis_c32=%s\n' \
  "$memory_pid" "$redis8_pid" "$redis32_pid"
printf '[v29-drift] probes=memory_seq_c32,redis_heldout_c08,redis_heldout_c32\n'
printf '[v29-drift] mode=free oracle_drift=on target_stride=256 max_step_cycles=1024\n'
printf '[v29-drift] out=%s\n' "$OUT_ROOT"
printf '[v29-drift] logs=%s %s %s\n' "$MEMORY_LOG" "$REDIS8_LOG" "$REDIS32_LOG"
printf '[v29-drift] monitor: tail -f %q %q %q\n' \
  "$MEMORY_LOG" "$REDIS8_LOG" "$REDIS32_LOG"
