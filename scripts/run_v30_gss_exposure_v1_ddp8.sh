#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
cd "$ROOT"

export MANIFEST=${MANIFEST:-data/v30_exposure_v1_dataset/manifest.json}
export CONFIG=${CONFIG:-configs/v30_gss_exposure_v1_60k.yaml}
export OUT=${OUT:-ckpt/tcsim_v30_gss_exposure_v1_60k_seed1234}
export STEPS=${STEPS:-${TARGET_STEPS:-60000}}
export GPUS=${GPUS:-0,1,2,3,4,5,6,7}
export NPROC=${NPROC:-8}
export PROFILE_ATTENTION=${PROFILE_ATTENTION:-0}

# A fresh formal run imports only canonical v29 parameters.  The incompatible
# ready-clock v30/P1 checkpoints are never resumed or relabelled.  The formal
# zero-output adapter and Exposure router are trained on commit-clock cache.
if [[ -n "${RESUME_CKPT:-}" ]]; then
  unset INIT_CHECKPOINT
else
  export INIT_CHECKPOINT=${V29_CHECKPOINT:-ckpt/tcsim_v29_packed3_100m_8gpu_60k/best.pt}
fi

exec bash scripts/run_v29_ddp8.sh
