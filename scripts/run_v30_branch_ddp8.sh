#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
cd "$ROOT"

variant=$(printf '%s' "${VARIANT:-b1}" | tr '[:upper:]' '[:lower:]')
case "$variant" in
  b1)
    default_config=configs/v30_branch_b1_headless_100m.yaml
    default_out=ckpt/tcsim_v30_branch_b1_headless_100m_8gpu_60000
    ;;
  b2)
    default_config=configs/v30_branch_b2_replay_event_100m.yaml
    default_out=ckpt/tcsim_v30_branch_b2_replay_event_100m_8gpu_60000
    ;;
  b3)
    default_config=configs/v30_branch_b3_replay_history_100m.yaml
    default_out=ckpt/tcsim_v30_branch_b3_replay_history_100m_8gpu_60000
    ;;
  *)
    echo "[v30-branch-ddp][ERROR] VARIANT must be b1, b2, or b3: $variant" >&2
    exit 2
    ;;
esac

export VARIANT="$variant"
export MANIFEST=${MANIFEST:-data/v30_branch_replay_dataset/manifest.json}
export CONFIG=${CONFIG:-$default_config}
export OUT=${OUT:-$default_out}
export STEPS=${STEPS:-${TARGET_STEPS:-60000}}
export GPUS=${GPUS:-0,1,2,3,4,5,6,7}
export NPROC=${NPROC:-8}

# B1--B3 are clean, independently trained ablations.  A watchdog resume is
# valid, but initializing from a v29 or another branch variant is not.
unset INIT_CHECKPOINT

exec bash scripts/run_v29_ddp8.sh
