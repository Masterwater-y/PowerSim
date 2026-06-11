#!/usr/bin/env bash
set -euo pipefail

THIS_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"

export RAW_ROOT="${RAW_ROOT:-${TAO_ROOT}/datagen/tmp/exp_w11_w15_parallel_20260604_215144/runs}"
export RUN_ROOT="${RUN_ROOT:-${THIS_DIR}/../runs/w11_w15_100k_driver_$(date +%Y%m%d_%H%M%S)}"
export LOG_PREFIX="${LOG_PREFIX:-validate-w}"
export WORKLOADS="${WORKLOADS:-W11_stream_mix W12_stencil2d W13_graph_walk W14_branch_state W15_indirect}"

exec "${THIS_DIR}/_validate_100k_common.sh" "$@"
