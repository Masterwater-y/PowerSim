#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
cd "$ROOT"

TORCHRUN=${TORCHRUN:-/data00/yinhaolang/infer/.venv/bin/torchrun}
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
MANIFEST=${MANIFEST:-data/v28_1_business_a2_sharedzipf_dataset/manifest.json}
CONFIG=${CONFIG:-configs/mvp_100m.yaml}
OUT=${OUT:-ckpt/tcsim_v281_business_100m_8gpu_30000}

STEPS=${STEPS:-${TARGET_STEPS:-30000}}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
NPROC=${NPROC:-8}
MASTER_PORT=${MASTER_PORT:-29617}

mkdir -p logs ckpt tmp "$OUT"

echo "[tcsim-ddp] ROOT=$ROOT"
echo "[tcsim-ddp] MANIFEST=$MANIFEST"
echo "[tcsim-ddp] CONFIG=$CONFIG"
echo "[tcsim-ddp] OUT=$OUT"
echo "[tcsim-ddp] GPUS=$GPUS NPROC=$NPROC STEPS=$STEPS"
echo "[tcsim-ddp] RESUME_CKPT=${RESUME_CKPT:-<none>}"

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

exec "$TORCHRUN" \
  --standalone \
  --nproc_per_node="$NPROC" \
  --master_port="$MASTER_PORT" \
  "$ROOT/scripts/train_mvp.py" \
  --manifest "$MANIFEST" \
  --out "$OUT" \
  --config "$CONFIG" \
  --device cuda \
  --max_steps "$STEPS" \
  "${resume_args[@]}"
