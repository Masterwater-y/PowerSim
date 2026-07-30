#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
cd "$ROOT"

export MANIFEST=${MANIFEST:-data/v30_gss_ready_dataset/manifest.json}
export CONFIG=${CONFIG:-configs/v30_gss_joint_v2_60k.yaml}
export OUT=${OUT:-ckpt/tcsim_v30_gss_joint_v2_60k_seed1234}
export STEPS=${STEPS:-${TARGET_STEPS:-60000}}
export GPUS=${GPUS:-0,1,2,3,4,5,6,7}
export NPROC=${NPROC:-8}
export PROFILE_ATTENTION=${PROFILE_ATTENTION:-0}

# Resume restores the model, staged optimizer groups, and global step.  A fresh
# run imports model tensors only from P1 and resets all training state.  P1 is
# initialization-only; no frozen teacher is constructed during optimization.
if [[ -n "${RESUME_CKPT:-}" ]]; then
  unset INIT_CHECKPOINT
else
  export INIT_CHECKPOINT=${P1_CHECKPOINT:-ckpt/tcsim_v30_gss_p1_frozen_adapter_5k_seed1234/best.pt}
fi

exec bash scripts/run_v29_ddp8.sh
