#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_ROOT="${SOURCE_ROOT:-${ROOT}/tmp/business-excitation-c4}"
DATASET_ROOT="${DATASET_ROOT:-${ROOT}/tmp/business-excitation-c4-v6-directed}"
PYTHON="${FASTSIM_HOST_PYTHON:-/data00/yinhaolang/infer/.venv/bin/python}"
JOBS="${JOBS:-8}"

if [[ ! -x "${PYTHON}" ]]; then
  PYTHON=python3
fi

mkdir -p "${DATASET_ROOT}"
if [[ ! -e "${DATASET_ROOT}/labels" ]]; then
  ln -s "${SOURCE_ROOT}/labels" "${DATASET_ROOT}/labels"
fi

"${PYTHON}" "${ROOT}/tools/collect_functional_traces.py" \
  --matrix "${ROOT}/configs/workloads/business_excitation.json" \
  --bin-dir "${ROOT}/workloads/business_excitation/bin/gem5" \
  --out "${DATASET_ROOT}/traces" \
  --cores 4 \
  --jobs 2 \
  --workload gofeed_fanout_wide \
  --workload pytorch_dense_batch \
  "$@"

for arg in "$@"; do
  if [[ "${arg}" == "--dry-run" || "${arg}" == "--list" ]]; then
    exit 0
  fi
done

run_variant() {
  local name=$1
  shift
  local out="${DATASET_ROOT}/fastsim-${name}"
  "${PYTHON}" "${ROOT}/tools/run_uarch_fastsim.py" \
    --root "${DATASET_ROOT}" \
    --out "${out}" \
    --matrix "${ROOT}/configs/workloads/business_excitation.json" \
    --config "${ROOT}/configs/gem5/v28_1-time-epoch.cfg" \
    --fastsim "${ROOT}/build/fastsim" \
    --jobs "${JOBS}" \
    --uarch baseline \
    --uarch core_width4 \
    --uarch rob96 \
    --uarch rob256 \
    --uarch iq32 \
    --uarch iq96 \
    --workload gofeed_fanout_wide \
    --workload pytorch_dense_batch \
    "$@"
  "${PYTHON}" "${ROOT}/tools/evaluate_uarch_generalization.py" \
    --root "${DATASET_ROOT}" \
    --fastsim-root "${out}" \
    --out "${DATASET_ROOT}/evaluation-${name}" \
    --workload gofeed_fanout_wide \
    --workload pytorch_dense_batch \
    --min-material-cases-per-uarch 0
}

run_variant production
run_variant shadow --branch-shadow-rob
run_variant response-rename --response-rename-feedback
run_variant shadow-response \
  --branch-shadow-rob \
  --response-rename-feedback

echo "directed v6 response/rename matrix complete: ${DATASET_ROOT}"
