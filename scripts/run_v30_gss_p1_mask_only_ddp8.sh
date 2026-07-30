#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
cd "$ROOT"

export MANIFEST=${MANIFEST:-data/v30_gss_ready_dataset/manifest.json}
export CONFIG=${CONFIG:-configs/v30_gss_p1_mask_only_frozen_adapter.yaml}
export OUT=${OUT:-ckpt/tcsim_v30_gss_p1_mask_only_5k_seed1234}
export STEPS=${STEPS:-${TARGET_STEPS:-5000}}
export GPUS=${GPUS:-0,1,2,3,4,5,6,7}
export NPROC=${NPROC:-8}

if [[ -n "${RESUME_CKPT:-}" ]]; then
  unset INIT_CHECKPOINT
else
  export INIT_CHECKPOINT=${V29_CHECKPOINT:-ckpt/tcsim_v29_packed3_100m_8gpu_60k/best.pt}
fi

exec bash scripts/run_v29_ddp8.sh
