#!/usr/bin/env bash
set -euo pipefail

ROOT=/data00/yinhaolang/FastSim
PYTHON=${PYTHON:-/data00/yinhaolang/infer/.venv/bin/python}
MATRIX=${ROOT}/configs/spec2026-uarch-exploration-v1.json
COLLECTOR=${ROOT}/tools/collect_spec2026_uarch_fs.py
MATERIALIZER=${ROOT}/tools/materialize_spec2026_uarch_dataset.py
SMOKE_ROOT=${ROOT}/tmp/spec2026-uarch-exploration-v1-native-v28_2-smoke
FORMAL_ROOT=${ROOT}/tmp/spec2026-uarch-exploration-v1-native-v28_2
CHECKPOINT_ROOT=${ROOT}/tmp/spec2026-uarch-exploration-checkpoints-v1
# The maintained host has 192 CPUs and about 2 TiB RAM.  The matrix has only
# 54 cases, so launch the entire collection wave concurrently; FastSim replay
# can use every hardware thread.
COLLECT_JOBS=${COLLECT_JOBS:-54}
FASTSIM_JOBS=${FASTSIM_JOBS:-192}
FASTSIM_CONFIG=${FASTSIM_CONFIG:-${ROOT}/configs/gem5-v28_2-fs-native-kernel.cfg}
FASTSIM_ROOT=${FASTSIM_ROOT:-${FORMAL_ROOT}/fastsim-v28_2-native}
EVALUATION_ROOT=${EVALUATION_ROOT:-${FORMAL_ROOT}/evaluation-v28_2-native}
ACTION=${1:-status}

mkdir -p "${ROOT}/tmp" "${CHECKPOINT_ROOT}"
export TMPDIR=${ROOT}/tmp/spec2026-uarch-process-tmp
mkdir -p "${TMPDIR}"

collect_smoke() {
  "${PYTHON}" "${COLLECTOR}" \
    --matrix "${MATRIX}" --run-root "${SMOKE_ROOT}" \
    --checkpoint-root "${CHECKPOINT_ROOT}" \
    --target-records 10000 --jobs "${COLLECT_JOBS}" \
    --sample-timeout-seconds 900
}

collect_formal() {
  "${PYTHON}" "${COLLECTOR}" \
    --matrix "${MATRIX}" --run-root "${FORMAL_ROOT}" \
    --checkpoint-root "${CHECKPOINT_ROOT}" \
    --target-records 10000000 --jobs "${COLLECT_JOBS}" \
    --sample-timeout-seconds 1800
}

replay() {
  "${PYTHON}" "${MATERIALIZER}" --matrix "${MATRIX}" --run-root "${FORMAL_ROOT}"
  "${PYTHON}" "${ROOT}/tools/run_uarch_fastsim.py" \
    --root "${FORMAL_ROOT}" --matrix "${MATRIX}" \
    --config "${FASTSIM_CONFIG}" --out "${FASTSIM_ROOT}" \
    --fastsim "${ROOT}/build/fastsim" --jobs "${FASTSIM_JOBS}"
}

report() {
  "${PYTHON}" "${ROOT}/tools/evaluate_uarch_generalization.py" \
    --root "${FORMAL_ROOT}" --fastsim-root "${FASTSIM_ROOT}" \
    --out "${EVALUATION_ROOT}" \
    --ranking-materiality-threshold 0.005
}

status() {
  for root in "${SMOKE_ROOT}" "${FORMAL_ROOT}"; do
    if [[ -f "${root}/status.json" ]]; then
      echo "[spec2026-uarch] ${root}"
      jq -c '{target_records_per_core,summary,updated_at_utc}' "${root}/status.json"
    else
      echo "[spec2026-uarch] ${root}: not started"
    fi
  done
  if [[ -f "${EVALUATION_ROOT}/generalization-report.json" ]]; then
    jq -c '{cases,variant_cases,uarch_cpi_ranking,pmu_variants,gates}' \
      "${EVALUATION_ROOT}/generalization-report.json"
  fi
}

case "${ACTION}" in
  smoke) collect_smoke ;;
  formal) collect_formal ;;
  replay) replay ;;
  report) report ;;
  analyze) replay; report ;;
  all) collect_smoke; collect_formal; replay; report ;;
  status) status ;;
  *) echo "usage: $0 {smoke|formal|replay|report|analyze|all|status}" >&2; exit 2 ;;
esac
