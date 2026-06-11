#!/usr/bin/env bash
set -euo pipefail

THIS_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"

export LOG_PREFIX="${LOG_PREFIX:-validate-datasets}"
export RUN_ROOT="${RUN_ROOT:-${THIS_DIR}/../runs/validate_datasets_$(date +%Y%m%d_%H%M%S)}"

exec "${THIS_DIR}/_validate_100k_common.sh" "$@"
