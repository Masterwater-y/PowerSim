#!/usr/bin/env bash
# scripts/02_train.sh
# 阶段 2：训练 V10.3 ckpt
#
# 用法：
#   bash scripts/02_train.sh                    # 全量
#   bash scripts/02_train.sh --smoke            # 5 步烟囱
#   bash scripts/02_train.sh --run-name 0603_v10_3

set -euo pipefail
THIS_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
source "${THIS_DIR}/env.sh"

SMOKE=0
RUN_NAME=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --smoke) SMOKE=1; shift ;;
        --run-name) RUN_NAME="$2"; shift 2 ;;
        *) echo "unknown arg: $1"; exit 1 ;;
    esac
done

cd "${TAO_ROOT}/train"

if [[ ${SMOKE} -eq 1 ]]; then
    echo "[02_train] smoke (5 steps)"
    bash run_smoke.sh
else
    echo "[02_train] full training"
    if [[ -n "${RUN_NAME}" ]]; then
        RUN_NAME="${RUN_NAME}" bash run_train.sh
    else
        bash run_train.sh
    fi
fi

echo "[02_train] ckpt path: ${TAO_CKPT_ROOT}"
