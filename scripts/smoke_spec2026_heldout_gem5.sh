#!/usr/bin/env bash
set -euo pipefail

FASTSIM_ROOT=/data00/yinhaolang/FastSim
TCSIM_ROOT=/data00/yinhaolang/TCSim
GEM5_ROOT=/data00/yinhaolang/gem5-fs
PYTHON_BIN=${PYTHON_BIN:-/data00/yinhaolang/infer/.venv/bin/python}
MATRIX_RUNNER=${TCSIM_ROOT}/scripts/run_gem5_fs_cpi_matrix.py
AUX_DISK=${AUX_DISK:-${TCSIM_ROOT}/data/spec2026_diskimg/spec2026-uarch-exploration-v1.ext4}
RUN_TAG=${RUN_TAG:-spec2026-uarch-astcenc-gem5-smoke}
RUN_ROOT=${FASTSIM_ROOT}/tmp/${RUN_TAG}
CORE_SET=${CORE_SET:-"4 8"}
WORKLOAD_SET=${WORKLOAD_SET:-"731.astcenc_r"}
TARGET_RECORDS=${TARGET_RECORDS:-10000}
JOBS=${JOBS:-2}
# KVM covers guest boot and input setup.  Bound only the restored OoO source
# warmup plus smoke ROI; ten minutes is the agreed diagnostic ceiling.
SAMPLE_TIMEOUT_SECONDS=${SAMPLE_TIMEOUT_SECONDS:-600}
FUNCTIONAL_TRACE_MODE=${FUNCTIONAL_TRACE_MODE:-native-kernel}

fail() {
  echo "[spec2026-heldout-gem5-smoke][ERROR] $*" >&2
  exit 2
}

[[ -x "${PYTHON_BIN}" ]] || fail "missing Python: ${PYTHON_BIN}"
[[ -f "${MATRIX_RUNNER}" ]] || fail "missing matrix runner: ${MATRIX_RUNNER}"
[[ -x "${GEM5_ROOT}/build/X86_MESI_Three_Level/gem5.opt" ]] || \
  fail "missing gem5 binary"
[[ -f "${AUX_DISK}" ]] || fail "missing heldout image: ${AUX_DISK}"
[[ "${TARGET_RECORDS}" =~ ^[0-9]+$ ]] && (( TARGET_RECORDS > 0 )) || \
  fail "TARGET_RECORDS must be positive"
[[ "${JOBS}" =~ ^[0-9]+$ ]] && (( JOBS > 0 )) || fail "JOBS must be positive"
[[ "${SAMPLE_TIMEOUT_SECONDS}" =~ ^[0-9]+$ ]] && \
  (( SAMPLE_TIMEOUT_SECONDS > 0 )) || fail "SAMPLE_TIMEOUT_SECONDS must be positive"

case "${FUNCTIONAL_TRACE_MODE}" in
  user) functional_trace_arg=--functional-user-only ;;
  native-kernel) functional_trace_arg=--functional-include-kernel ;;
  *) fail "FUNCTIONAL_TRACE_MODE must be user or native-kernel" ;;
esac

read -r -a cores <<<"${CORE_SET}"
(( ${#cores[@]} > 0 )) || fail "CORE_SET is empty"
for core in "${cores[@]}"; do
  [[ "${core}" =~ ^[0-9]+$ ]] && (( core > 0 )) || fail "invalid core count: ${core}"
done

read -r -a workloads <<<"${WORKLOAD_SET}"
(( ${#workloads[@]} > 0 )) || fail "WORKLOAD_SET is empty"

mkdir -p \
  "${RUN_ROOT}/matrix" \
  "${RUN_ROOT}/source" \
  "${RUN_ROOT}/driver-tmp" \
  "${RUN_ROOT}/trace-scratch" \
  "${RUN_ROOT}/process-tmp"
export TMPDIR=${RUN_ROOT}/process-tmp

export FASTSIM_EFFECTIVE_TARGET_GENERATOR=${FASTSIM_ROOT}/tools/generate_fs_effective_target.py
export FASTSIM_EFFECTIVE_TARGET_PYTHON=${PYTHON_BIN}
export FASTSIM_EVENT_DICTIONARY=${FASTSIM_ROOT}/configs/pmu-event-dictionary-v1.json
export FUNCTIONAL_TRACE_MODE

echo "[spec2026-heldout-gem5-smoke] workloads=${WORKLOAD_SET} cores=${CORE_SET} target=${TARGET_RECORDS} mode=${FUNCTIONAL_TRACE_MODE}"
"${PYTHON_BIN}" "${MATRIX_RUNNER}" \
  --stage full \
  --workloads "${workloads[@]}" \
  --cores "${cores[@]}" \
  --roi-insts "${TARGET_RECORDS}" \
  --roi-target-domain user-fst \
  --roi-safety-multiplier 100 \
  --roi-stop-policy all-core \
  --warmup-mode source \
  --sample-timeout-seconds "${SAMPLE_TIMEOUT_SECONDS}" \
  --prepare-jobs "${JOBS}" \
  --sample-jobs "${JOBS}" \
  --matrix-root "${RUN_ROOT}/matrix" \
  --result-root "${RUN_ROOT}/source" \
  --tmp-root "${RUN_ROOT}/driver-tmp" \
  --trace-tmp-root "${RUN_ROOT}/trace-scratch" \
  --gem5-root "${GEM5_ROOT}" \
  --aux-disk "${AUX_DISK}" \
  --emit-functional-trace \
  --trace-format fst \
  --measure-cpl \
  --native-anomaly-limit 32 \
  "${functional_trace_arg}" \
  --reuse-restore-config-mismatch

status=${RUN_ROOT}/matrix/status.json
jq -e \
  --argjson expected "$(( ${#cores[@]} * ${#workloads[@]} ))" \
  '.summary.status == "complete" and
   .summary.completed_results == $expected and
   .summary.expected_results == $expected' \
  "${status}" >/dev/null || fail "matrix did not complete: ${status}"

echo "[spec2026-heldout-gem5-smoke] status=${status}"
jq -c '{summary,tasks}' "${status}"
