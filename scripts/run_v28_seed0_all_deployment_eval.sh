#!/usr/bin/env bash
# Evaluate every unique seed0 base and heldout trace in predicted-state mode.
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
export ROOT
export SPLIT=${SPLIT:-train,test_business}
export OUT_ROOT=${OUT_ROOT:-$ROOT/logs/v28_seed0_all_deployment_$(date +%Y%m%d_%H%M%S)}

exec bash "$ROOT/scripts/run_v28_seed1_deployment_eval.sh"
