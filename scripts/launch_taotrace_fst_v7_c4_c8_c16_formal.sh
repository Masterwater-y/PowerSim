#!/usr/bin/env bash
set -euo pipefail

export RUN_TAG=${RUN_TAG:-taotrace-fst-v7-c4-c8-c16-asid-formal-20260818}
export CORE_LIST=${CORE_LIST:-"4 8 16"}
export BASE_JOBS=${BASE_JOBS:-8}
export STOCKFISH_JOBS=${STOCKFISH_JOBS:-2}
export SPH_JOBS=${SPH_JOBS:-2}
export WARMTRACE_JOBS=${WARMTRACE_JOBS:-4}
export MAX_ATTEMPTS=${MAX_ATTEMPTS:-0}
export WATCHDOG_POLL_SECONDS=${WATCHDOG_POLL_SECONDS:-30}
export MIN_FREE_GIB=${MIN_FREE_GIB:-512}
export TARGET_RECORDS=${TARGET_RECORDS:-10000000}

exec /data00/yinhaolang/FastSim/scripts/launch_taotrace_fst_v7_c16_c32_formal.sh "$@"
