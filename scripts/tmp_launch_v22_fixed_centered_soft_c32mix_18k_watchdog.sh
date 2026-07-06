#!/usr/bin/env bash
set -euo pipefail

RUN_NAME=v22_fixed_centered_soft_c32mix_18k_watch \
OUT=ckpt/v22_fixed_centered_soft_c32mix_8gpu_18000 \
DATA=data/windows_v23_v16_tail_local_all_plus_c32_seedB/windows.jsonl \
CACHE_PATH=data/windows_v23_v16_tail_local_all_plus_c32_seedB/windows.maxlen32768.tensor_cache \
TARGET_STEPS=18000 \
STEPS=18000 \
GPUS=0,1,2,3,4,5,6,7 \
NPROC=8 \
BS=1 \
GRAD_ACCUM=1 \
EVAL_EVERY=500 \
SAVE_EVERY=500 \
EVAL_BATCHES=0 \
NUM_WORKERS=2 \
LOSS_WEIGHT_MODE=fixed \
LAMBDA_CPI_ABS=1.0 \
LAMBDA_CYCLES=1.0 \
LAMBDA_AUX_PMU=0.05 \
LAMBDA_CENTERED_CPI=0.3 \
CENTERED_REF_STD=0.30 \
CENTERED_WEIGHT_MIN=0.10 \
CENTERED_WEIGHT_MAX=3.0 \
CENTERED_MIN_STD=0.30 \
bash scripts/launch_v22_no_tstart_12k_watchdog.sh
