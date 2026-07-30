#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
cd "$ROOT"

export MANIFEST=${MANIFEST:-data/v30_gss_ready_dataset/manifest.json}
export CONFIG=${CONFIG:-configs/v30_gss_gate_only_2k.yaml}
export OUT=${OUT:-ckpt/tcsim_v30_gss_gate_only_2k_seed1234}
export STEPS=${STEPS:-${TARGET_STEPS:-2000}}
export GPUS=${GPUS:-0,1,2,3,4,5,6,7}
export NPROC=${NPROC:-8}

if [[ -n "${RESUME_CKPT:-}" ]]; then
  unset INIT_CHECKPOINT
else
  export INIT_CHECKPOINT=${G1_CHECKPOINT:-ckpt/tcsim_v30_gss_p1_frozen_adapter_5k_seed1234/best.pt}
fi

exec bash scripts/run_v29_ddp8.sh
