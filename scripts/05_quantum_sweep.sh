#!/usr/bin/env bash
# scripts/05_quantum_sweep.sh
# Quantum Δt 扫描：度量不同 Δt 下的 CPI 偏差与吞吐
#
# 用法：
#   bash scripts/05_quantum_sweep.sh \
#        --trace-dir <gem5_records_micro_dir> \
#        --ckpt      <ckpt.pt> \
#        --deltas    1,128,256,512,1024
#
# Δt=1 视为严格全序 baseline。

set -euo pipefail
THIS_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
source "${THIS_DIR}/env.sh"

TRACE_DIR=""
CKPT="${TAO_CKPT_ROOT}/0602.pt"
DELTAS="1,128,256,512,1024"
SMOKE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --trace-dir) TRACE_DIR="$2"; shift 2 ;;
        --ckpt)      CKPT="$2"; shift 2 ;;
        --deltas)    DELTAS="$2"; shift 2 ;;
        --smoke)     SMOKE=1; shift ;;
        *) echo "unknown arg: $1"; exit 1 ;;
    esac
done

TS=$(date +%Y%m%d_%H%M%S)
SWEEP_ROOT="${TAO_ROOT}/runs/quantum_sweep_${TS}"
mkdir -p "${SWEEP_ROOT}"

IFS=',' read -ra DELTA_ARR <<< "${DELTAS}"

for d in "${DELTA_ARR[@]}"; do
    OUT_DIR="${SWEEP_ROOT}/delta_${d}"
    echo ""
    echo "============================================================"
    echo "[quantum_sweep] Δt=${d} → ${OUT_DIR}"
    echo "============================================================"

    if [[ ${SMOKE} -eq 1 ]]; then
        bash "${THIS_DIR}/04_infer.sh" --mode ckpt --smoke --quantum-cycles "${d}"
        # 把 smoke 结果搬到 sweep 目录
        mv "${TAO_ROOT}/runs/smoke_ckpt_q${d}" "${OUT_DIR}" 2>/dev/null || true
    else
        if [[ -z "${TRACE_DIR}" ]]; then
            echo "ERROR: --trace-dir is required (or use --smoke)"
            exit 1
        fi
        bash "${THIS_DIR}/04_infer.sh" \
             --trace-dir "${TRACE_DIR}" \
             --ckpt      "${CKPT}" \
             --mode      ckpt \
             --quantum-cycles "${d}"
    fi
done

echo ""
echo "[quantum_sweep] all done. Aggregated under: ${SWEEP_ROOT}"
echo "[quantum_sweep] TODO: write a small Python helper to summarize CPI/IPS into compare.json"
