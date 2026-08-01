#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT=${ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
cd "$PROJECT_ROOT"

RUN_STAMP=${RUN_STAMP:-$(date +%Y%m%d_%H%M%S)}
RUN_TAG=${RUN_TAG:-v29_latent32_best55k_gss_seed1_plus_heldout120_$RUN_STAMP}
OUT_DIR=${OUT:-$PROJECT_ROOT/logs/$RUN_TAG}
PIPELINE_LOG=${PIPELINE_LOG:-$OUT_DIR/pipeline.log}
PID_FILE=${PID_FILE:-$OUT_DIR/pipeline.pid}

if [[ -f "$PID_FILE" ]]; then
  existing_pid=$(<"$PID_FILE")
  if [[ "$existing_pid" =~ ^[0-9]+$ ]] && kill -0 "$existing_pid" 2>/dev/null; then
    echo "[latent-gss-120][ERROR] pipeline already running: pid=$existing_pid" >&2
    echo "[latent-gss-120] log=$PIPELINE_LOG" >&2
    exit 2
  fi
fi

mkdir -p "$OUT_DIR"
printf '[%s] launch seed0-heldout28 + seed1-deployment92\n' \
  "$(date '+%Y-%m-%d %H:%M:%S')" >>"$PIPELINE_LOG"

nohup env \
  ROOT="$PROJECT_ROOT" \
  OUT="$OUT_DIR" \
  PY="${PY:-/data00/yinhaolang/infer/.venv/bin/python}" \
  CKPT="${CKPT:-$PROJECT_ROOT/ckpt/tcsim_v29_latent32_scratch_100m_8gpu_60k/best.pt}" \
  BASE_MANIFEST="${BASE_MANIFEST:-$PROJECT_ROOT/data/v29_global_time_dataset/manifest.json}" \
  GSS_ROOT="${GSS_ROOT:-$PROJECT_ROOT/data/v30_gss_commit_sidecars}" \
  MANIFEST="${MANIFEST:-$PROJECT_ROOT/data/v30_gss_commit_dataset/manifest.json}" \
  GPUS="${GPUS:-0,1,2,3,4,5,6,7}" \
  SIDECAR_WORKERS="${SIDECAR_WORKERS:-64}" \
  TARGET_STRIDE="${TARGET_STRIDE:-256}" \
  PROGRESS_EVERY="${PROGRESS_EVERY:-100}" \
  RESUME="${RESUME:-1}" \
  bash scripts/run_v29_latent32_gss_seed1_plus_heldout_8gpu.sh \
  >>"$PIPELINE_LOG" 2>&1 &

pipeline_pid=$!
printf '%s\n' "$pipeline_pid" >"$PID_FILE"

printf '[latent-gss-120] pid=%s\n' "$pipeline_pid"
printf '[latent-gss-120] scope=seed0-heldout28 + seed1-deployment92 (total=120)\n'
printf '[latent-gss-120] output=%s\n' "$OUT_DIR"
printf '[latent-gss-120] log=%s\n' "$PIPELINE_LOG"
printf '[latent-gss-120] report=%s/report.txt\n' "$OUT_DIR"
printf '[latent-gss-120] monitor: tail -f %q\n' "$PIPELINE_LOG"
