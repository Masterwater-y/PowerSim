#!/usr/bin/env bash
# scripts/01_taogen_collect.sh
# 阶段 1：用 gem5 detailed + AtomicSimpleCPU 探针生成训练 parquet
#
# 这是一个 thin wrapper，真实采集脚本在 datagen/scripts/。
# 提供两档：smoke (3M) 与 full (10M × N workloads)。
#
# 用法：
#   bash scripts/01_taogen_collect.sh --profile 3m
#   bash scripts/01_taogen_collect.sh --workloads W11 W12 W13 W14 W15 \
#                                     --rows-per-workload 10000000

set -euo pipefail
THIS_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
source "${THIS_DIR}/env.sh"

PROFILE=""
WORKLOADS=()
ROWS_PER_WL=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --profile) PROFILE="$2"; shift 2 ;;
        --workloads) shift
            while [[ $# -gt 0 && "$1" != --* ]]; do WORKLOADS+=("$1"); shift; done ;;
        --rows-per-workload) ROWS_PER_WL="$2"; shift 2 ;;
        *) echo "unknown arg: $1"; exit 1 ;;
    esac
done

cd "${TAO_ROOT}/datagen"

if [[ "${PROFILE}" == "3m" ]]; then
    echo "[01_taogen] profile=3m → run_w11_w15_train.sh"
    bash scripts/run_w11_w15_train.sh
elif [[ -n "${ROWS_PER_WL}" && ${#WORKLOADS[@]} -gt 0 ]]; then
    if [[ "${ROWS_PER_WL}" -ge 10000000 ]]; then
        echo "[01_taogen] full 10M experiment → run_w11_w15_10m_experiment.sh"
        bash scripts/run_w11_w15_10m_experiment.sh
    else
        echo "[01_taogen] custom rows_per_wl=${ROWS_PER_WL} → fallback to run_w11_w15_train.sh"
        bash scripts/run_w11_w15_train.sh
    fi
else
    echo "[01_taogen] no profile/workloads given → smoke (3M)"
    bash scripts/run_w11_w15_train.sh
fi

echo "[01_taogen] done. Outputs under: ${TAO_TAOGEN_DATA_ROOT}"
