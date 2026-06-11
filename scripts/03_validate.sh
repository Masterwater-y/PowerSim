#!/usr/bin/env bash
# scripts/03_validate.sh
# 阶段 3a：验证侧 6 步流水线（带 oracle bit-exact 校验）
#
# 用法：
#   bash scripts/03_validate.sh \
#        --trace-dir <gem5_records_micro_dir> \
#        --ckpt <ckpt.pt>

set -euo pipefail
THIS_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
source "${THIS_DIR}/env.sh"

TRACE_DIR=""
CKPT="${TAO_CKPT_ROOT}/0602.pt"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --trace-dir) TRACE_DIR="$2"; shift 2 ;;
        --ckpt)      CKPT="$2"; shift 2 ;;
        *) echo "unknown arg: $1"; exit 1 ;;
    esac
done

if [[ -z "${TRACE_DIR}" ]]; then
    echo "ERROR: --trace-dir is required"
    exit 1
fi

cd "${TAO_ROOT}/infer"
bash scripts/validate_from_trace.sh \
     --trace-dir "${TRACE_DIR}" \
     --ckpt      "${CKPT}"

echo "[03_validate] done."
