#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
cd "$ROOT"

export MANIFEST=${MANIFEST:-data/v29_long_history_dataset/manifest.json}
export CONFIG=${CONFIG:-configs/v29_frozen_memory_probe.yaml}
export OUT=${OUT:-ckpt/tcsim_v29_frozen_memory_probe_e2_10k_seed1234}
export STEPS=${STEPS:-${TARGET_STEPS:-10000}}
export GPUS=${GPUS:-0,1,2,3,4,5,6,7}
export NPROC=${NPROC:-8}

# A resumed probe already contains the initialized E0 state and correction
# optimizer. A fresh probe imports only model weights from this E0 checkpoint.
if [[ -n "${RESUME_CKPT:-}" ]]; then
  unset INIT_CHECKPOINT
else
  export INIT_CHECKPOINT=${E0_CHECKPOINT:-ckpt/tcsim_v29_packed3_100m_8gpu_60k/best.pt}
fi

exec bash scripts/run_v29_ddp8.sh
