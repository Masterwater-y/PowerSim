#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
cd "$ROOT"

TORCHRUN=${TORCHRUN:-/data00/yinhaolang/infer/.venv/bin/torchrun}
MANIFEST=${MANIFEST:-data/v29_global_time_dataset/manifest.json}
CONFIG=${CONFIG:-configs/v29_100m.yaml}
OUT=${OUT:-ckpt/tcsim_v29_global_time_100m_8gpu_30000}
STEPS=${STEPS:-${TARGET_STEPS:-30000}}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
NPROC=${NPROC:-8}

[[ -f "$MANIFEST" ]] || { echo "[v29-ddp][ERROR] missing manifest: $MANIFEST" >&2; exit 2; }
[[ -f "$CONFIG" ]] || { echo "[v29-ddp][ERROR] missing config: $CONFIG" >&2; exit 2; }
mkdir -p logs ckpt tmp "$OUT"

echo "[v29-ddp] manifest=$MANIFEST"
echo "[v29-ddp] config=$CONFIG out=$OUT"
echo "[v29-ddp] gpus=$GPUS nproc=$NPROC steps=$STEPS rendezvous=standalone-free-port"
echo "[v29-ddp] resume=${RESUME_CKPT:-<none>}"
echo "[v29-ddp] sdpa=${SDPA_BACKEND:-auto} amp=${AMP_DTYPE:-bf16} profile=${PROFILE_ATTENTION:-1}"

export CUDA_VISIBLE_DEVICES="$GPUS"
export OMP_NUM_THREADS=${OMP_NUM_THREADS:-4}
export NCCL_P2P_DISABLE=${NCCL_P2P_DISABLE:-0}
export NCCL_IB_DISABLE=${NCCL_IB_DISABLE:-1}
export PYTORCH_CUDA_ALLOC_CONF=${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}
export TMPDIR=${TMPDIR:-"$ROOT/tmp"}

resume_args=()
if [[ -n "${RESUME_CKPT:-}" ]]; then
  resume_args=(--resume "$RESUME_CKPT")
fi

profile_args=(--profile-attention)
if [[ "${PROFILE_ATTENTION:-1}" == "0" ]]; then
  profile_args=(--no-profile-attention)
fi

exec "$TORCHRUN" \
  --standalone \
  --nproc_per_node="$NPROC" \
  "$ROOT/scripts/train_v29.py" \
  --manifest "$MANIFEST" \
  --out "$OUT" \
  --config "$CONFIG" \
  --device cuda \
  --max-steps "$STEPS" \
  --sdpa-backend "${SDPA_BACKEND:-auto}" \
  --amp-dtype "${AMP_DTYPE:-bf16}" \
  "${profile_args[@]}" \
  "${resume_args[@]}"
