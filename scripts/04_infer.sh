#!/usr/bin/env bash
# scripts/04_infer.sh
# 阶段 3b：部署侧推理（仅 functional），三种模式：ckpt / label / mock
# 也可作为 benchmark 工具：--smoke 用 5K rows 现成样本跑。
#
# 用法：
#   bash scripts/04_infer.sh --trace-dir <dir> --ckpt <pt> --mode ckpt --quantum-cycles 256
#   bash scripts/04_infer.sh --mode label --smoke
#   bash scripts/04_infer.sh --mode ckpt  --smoke

set -euo pipefail
THIS_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
source "${THIS_DIR}/env.sh"

TRACE_DIR=""
CKPT="${TAO_CKPT_ROOT}/0602.pt"
MODE="ckpt"
QUANTUM_CYCLES="${TAO_QUANTUM_CYCLES}"
SMOKE=0
BATCH=8
REF_SIM_BACKEND="${REF_SIM_BACKEND:-timing-functional}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --trace-dir)      TRACE_DIR="$2"; shift 2 ;;
        --ckpt)           CKPT="$2"; shift 2 ;;
        --mode)           MODE="$2"; shift 2 ;;
        --quantum-cycles) QUANTUM_CYCLES="$2"; shift 2 ;;
        --batch)          BATCH="$2"; shift 2 ;;
        --ref-sim-backend) REF_SIM_BACKEND="$2"; shift 2 ;;
        --smoke)          SMOKE=1; shift ;;
        *) echo "unknown arg: $1"; exit 1 ;;
    esac
done

cd "${TAO_ROOT}/infer"

# Smoke: 用现成 5K rows
if [[ ${SMOKE} -eq 1 ]]; then
    SMOKE_FUNC_DIR="${TAO_ROOT}/infer/tmp_smoke_5k/functional_parquet"
    SMOKE_LABEL_DIR="${TAO_ROOT}/infer/data/W11_stream_mix/labels_parquet"
    UARCH="${TAO_ROOT}/infer/data/W11_stream_mix/uarch_profile.json"
    OUT_DIR="${TAO_ROOT}/runs/smoke_${MODE}_q${QUANTUM_CYCLES}"

    mkdir -p "${OUT_DIR}"

    # 若没有 5K 样本，先切一份
    if [[ ! -f "${SMOKE_FUNC_DIR}/functional.core0.parquet" ]]; then
        echo "[04_infer] preparing 5K smoke sample..."
        ${TAO_ROOT}/scripts/_prep_smoke_5k.py
    fi

    EXTRA_ARGS=( --functional-dir "${SMOKE_FUNC_DIR}"
                 --uarch-profile  "${UARCH}"
                 --ref-sim-module-dir "${TAO_REF_SIM_BUILD_DIR}"
                 --ref-sim-backend "${REF_SIM_BACKEND}"
                 --out-jsonl      "${OUT_DIR}/infer.jsonl"
                 --report-json    "${OUT_DIR}/report.json"
                 --quantum-cycles "${QUANTUM_CYCLES}"
                 --model-batch-size "${BATCH}" )

    case "${MODE}" in
        ckpt)  EXTRA_ARGS+=( --ckpt "${CKPT}" ) ;;
        label) EXTRA_ARGS+=( --labels-dir "${SMOKE_LABEL_DIR}" --label-driven ) ;;
        mock)  EXTRA_ARGS+=( --mock-model ) ;;
        *) echo "unknown mode: ${MODE}"; exit 1 ;;
    esac

    /usr/bin/time -f "elapsed=%e s" \
        /root/miniconda3/envs/yinhaolang/bin/python driver/inference_driver.py \
        "${EXTRA_ARGS[@]}"
    exit 0
fi

# Full: 真实 trace
if [[ -z "${TRACE_DIR}" ]]; then
    echo "ERROR: --trace-dir is required (or use --smoke)"
    exit 1
fi

bash scripts/infer_from_functional.sh \
     --trace-dir       "${TRACE_DIR}" \
     --ckpt            "${CKPT}" \
     --mode            "${MODE}" \
     --quantum-cycles  "${QUANTUM_CYCLES}" \
     --model-batch-size "${BATCH}" \
     --ref-sim-backend "${REF_SIM_BACKEND}"

echo "[04_infer] done."
