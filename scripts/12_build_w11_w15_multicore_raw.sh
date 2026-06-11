#!/usr/bin/env bash
set -euo pipefail

THIS_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"

export NUM_CORES="${NUM_CORES:-8}"
export GROUP="${GROUP:-w}"
export RAW_ROOT="${RAW_ROOT:-${TAO_ROOT}/datagen/tmp/w11_w15_${NUM_CORES}c_raw_$(date +%Y%m%d_%H%M%S)/runs}"
export WORKLOADS="${WORKLOADS:-W11_stream_mix W12_stencil2d W13_graph_walk W14_branch_state W15_indirect}"

exec "${THIS_DIR}/14_collect_multicore_raw.sh" "$@"
