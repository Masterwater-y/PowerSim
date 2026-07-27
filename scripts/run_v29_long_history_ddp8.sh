#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
cd "$ROOT"

export MANIFEST=${MANIFEST:-data/v29_long_history_dataset/manifest.json}
export CONFIG=${CONFIG:-configs/v29_long_history_100m.yaml}
export OUT=${OUT:-ckpt/tcsim_v29_long_history_100m_8gpu_60000}
export STEPS=${STEPS:-${TARGET_STEPS:-60000}}
export GPUS=${GPUS:-0,1,2,3,4,5,6,7}
export NPROC=${NPROC:-8}

exec bash scripts/run_v29_ddp8.sh
