#!/usr/bin/env bash
set -euo pipefail

FASTSIM_ROOT=/data00/yinhaolang/FastSim
TCSIM_ROOT=/data00/yinhaolang/TCSim
GEM5_ROOT=/data00/yinhaolang/gem5-fs
PYTHON_BIN=${PYTHON_BIN:-/data00/yinhaolang/infer/.venv/bin/python}
MATRIX_RUNNER=${TCSIM_ROOT}/scripts/run_gem5_fs_cpi_matrix.py

action=${1:-worker}
PILOT_CORES=${PILOT_CORES:-${2:-4}}
[[ "${PILOT_CORES}" =~ ^(4|8)$ ]] || {
  echo "PILOT_CORES must be 4 or 8" >&2
  exit 2
}
RUN_TAG=${RUN_TAG:-taotrace-imap-v2-wrong-path-c${PILOT_CORES}-pilots-20260817}
RUN_ROOT=${FASTSIM_ROOT}/tmp/${RUN_TAG}
RESULT_ROOT=${RUN_ROOT}/source
TRACE_TMP_ROOT=${RUN_ROOT}/trace-scratch
DRIVER_TMP_ROOT=${RUN_ROOT}/driver-tmp
MATRIX_NEUTRON=${RUN_ROOT}/matrix-neutron
MATRIX_NAB=${RUN_ROOT}/matrix-nab
AUDIT_ROOT=${RUN_ROOT}/audit
PID_FILE=${RUN_ROOT}/launcher.pid
EXIT_FILE=${RUN_ROOT}/exit.code
TARGET_RECORDS=${TARGET_RECORDS:-100000}
SAMPLE_TIMEOUT_SECONDS=${SAMPLE_TIMEOUT_SECONDS:-3600}
ROI_SAFETY_MULTIPLIER=${ROI_SAFETY_MULTIPLIER:-100}

WARMTRACE_DISK=${TCSIM_ROOT}/data/spec2026_diskimg/spec2026-native-multicore-warmtrace.ext4

fail() {
  echo "[imap-v2-wrong-path-pilots][ERROR] $*" >&2
  exit 2
}

run_case() {
  local matrix_root=$1
  local workload=$2
  "${PYTHON_BIN}" "${MATRIX_RUNNER}" \
    --stage sample \
    --workloads "${workload}" \
    --cores "${PILOT_CORES}" \
    --roi-insts "${TARGET_RECORDS}" \
    --roi-target-domain user-fst \
    --roi-safety-multiplier "${ROI_SAFETY_MULTIPLIER}" \
    --roi-stop-policy all-core \
    --warmup-mode source \
    --sample-timeout-seconds "${SAMPLE_TIMEOUT_SECONDS}" \
    --sample-jobs 1 \
    --prepare-jobs 1 \
    --matrix-root "${matrix_root}" \
    --result-root "${RESULT_ROOT}" \
    --tmp-root "${DRIVER_TMP_ROOT}" \
    --trace-tmp-root "${TRACE_TMP_ROOT}" \
    --gem5-root "${GEM5_ROOT}" \
    --aux-disk "${WARMTRACE_DISK}" \
    --emit-functional-trace \
    --trace-format fst \
    --measure-cpl \
    --wrong-path-oracle \
    --functional-user-only \
    --reuse-binary-mismatch \
    --reuse-restore-config-mismatch
}

matrix_result() {
  local matrix=$1
  jq -er '.tasks[] | select(.sample.status == "completed") | .sample.result_dir' \
    "${matrix}/status.json"
}

run_audits() {
  mkdir -p "${AUDIT_ROOT}"
  "${PYTHON_BIN}" "${FASTSIM_ROOT}/tools/audit_functional_warmup_matrix.py" \
    --matrix "${MATRIX_NEUTRON}" \
    --matrix "${MATRIX_NAB}" \
    --output "${AUDIT_ROOT}/matrix-integrity.json" \
    --expected-cases 2 \
    --expected-fst-files "$((2 * PILOT_CORES))" \
    --require-destination-classes
  "${PYTHON_BIN}" \
    "${FASTSIM_ROOT}/tools/audit_fst_static_instruction_maps.py" \
    --require-map \
    --require-operands \
    --output "${AUDIT_ROOT}/static-instruction-maps.json" \
    "${RESULT_ROOT}"
  local matrix result label
  for matrix in "${MATRIX_NEUTRON}" "${MATRIX_NAB}"; do
    result=$(matrix_result "${matrix}")
    label=$(basename "$(dirname "$(dirname "${result}")")")
    "${PYTHON_BIN}" "${FASTSIM_ROOT}/tools/validate_wrong_path_oracle.py" \
      "${result}/oracle/wrong_path.jsonl" \
      --gem5-stats "${result}/stats.txt" \
      --json-out "${AUDIT_ROOT}/wrong-path-validation-${label}.json" \
      --markdown-out "${AUDIT_ROOT}/wrong-path-validation-${label}.md"
  done
}

worker() {
  unset PYTHONHOME
  export PYTHONUNBUFFERED=1
  echo "[imap-v2-wrong-path-pilots] started=$(date -Is) cores=${PILOT_CORES}"
  echo "[imap-v2-wrong-path-pilots] root=${RUN_ROOT}"
  run_case "${MATRIX_NEUTRON}" 881.neutron_s &
  local neutron_pid=$!
  run_case "${MATRIX_NAB}" 816.nab_s &
  local nab_pid=$!
  local failed=0
  wait "${neutron_pid}" || failed=1
  wait "${nab_pid}" || failed=1
  [[ "${failed}" -eq 0 ]] || fail "one or more parallel C4 pilots failed"
  run_audits
  echo "[imap-v2-wrong-path-pilots] completed=$(date -Is)"
}

show_status() {
  local matrix
  for matrix in "${MATRIX_NEUTRON}" "${MATRIX_NAB}"; do
    if [[ -f "${matrix}/status.json" ]]; then
      jq -c '{matrix:input_filename,summary,tasks}' "${matrix}/status.json"
    fi
  done
  [[ -f "${EXIT_FILE}" ]] && echo "[imap-v2-wrong-path-pilots] exit=$(<"${EXIT_FILE}")"
  echo "[imap-v2-wrong-path-pilots] root=${RUN_ROOT}"
}

mkdir -p "${RUN_ROOT}"

case "${action}" in
  worker)
    printf '%s\n' "$$" >"${PID_FILE}"
    trap 'status=$?; printf "%s\n" "${status}" >"${EXIT_FILE}"; trap - EXIT; exit "${status}"' EXIT
    worker
    ;;
  audit)
    run_audits
    printf '%s\n' 0 >"${EXIT_FILE}"
    ;;
  status)
    show_status
    ;;
  *)
    fail "usage: $0 [worker|audit|status] [4|8]"
    ;;
esac
