#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
cd "$ROOT"

export CKPT=${CKPT:-ckpt/tcsim_v29_frozen_memory_probe_e2_10k_seed1234/best.pt}
export MANIFEST=${MANIFEST:-data/v29_long_history_dataset/manifest.json}
export OUT=${OUT:-logs/v29_frozen_probe_best9500_targeted_c04_c32_s256_8gpu}
export GPUS=${GPUS:-0,1,2,3,4,5,6,7}
export SPLITS=${SPLITS:-seed0_inference,development_heldout,deployment_inference}
export CORE_COUNTS=${CORE_COUNTS:-4,32}
export WORKLOADS=${WORKLOADS:-W_v28_redis_base,W_v28_redis_heldout,W_v28_simd_sse_dense,W_v28_marine_heldout,W_v28_coh_readmostly_sparse,W_v28_flink_heldout,W_v28_memory_random_mlp,W_v28_memory_seq_moderate,W_v28_gofeed_base}
export MODE=${MODE:-free}
export TARGET_STRIDE=${TARGET_STRIDE:-256}
export MIN_STEP_CYCLES=${MIN_STEP_CYCLES:-4}
export MAX_STEP_CYCLES=${MAX_STEP_CYCLES:-1024}
export MAX_NO_PROGRESS_STEPS=${MAX_NO_PROGRESS_STEPS:-64}
export WINDOW_PARALLEL_MODE=${WINDOW_PARALLEL_MODE:-serial}
export AMP_DTYPE=${AMP_DTYPE:-bf16}
export SDPA_BACKEND=${SDPA_BACKEND:-auto}
export PROGRESS_EVERY=${PROGRESS_EVERY:-100}
export RESUME=${RESUME:-1}

exec bash scripts/run_v29_eval_8gpu.sh
