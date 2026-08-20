#!/usr/bin/env bash
set -euo pipefail

FASTSIM_ROOT=/data00/yinhaolang/FastSim
TCSIM_ROOT=/data00/yinhaolang/TCSim
GEM5_ROOT=/data00/yinhaolang/gem5-fs
PYTHON_BIN=${PYTHON_BIN:-/data00/yinhaolang/infer/.venv/bin/python}
MATRIX_RUNNER=${TCSIM_ROOT}/scripts/run_gem5_fs_cpi_matrix.py
export FASTSIM_EFFECTIVE_TARGET_GENERATOR=${FASTSIM_ROOT}/tools/generate_fs_effective_target.py
export FASTSIM_EFFECTIVE_TARGET_PYTHON=${PYTHON_BIN}
export FASTSIM_EVENT_DICTIONARY=${FASTSIM_ROOT}/configs/pmu-event-dictionary-v1.json

action=${1:-start}
RUN_TAG=${2:-${RUN_TAG:-taotrace-scoped-frontend-c4-pilots-20260820}}
TARGET_RECORDS=${3:-${TARGET_RECORDS:-100000}}
RUN_ROOT=${FASTSIM_ROOT}/tmp/${RUN_TAG}
RESULT_ROOT=${RUN_ROOT}/source
TRACE_TMP_ROOT=${RUN_ROOT}/trace-scratch
DRIVER_TMP_ROOT=${RUN_ROOT}/driver-tmp
AUDIT_ROOT=${RUN_ROOT}/audit
LOG_FILE=${RUN_ROOT}/launch.log
PID_FILE=${RUN_ROOT}/launcher.pid
EXIT_FILE=${RUN_ROOT}/exit.code
MATRIX_NAB=${RUN_ROOT}/matrix-nab
MATRIX_ZSTD=${RUN_ROOT}/matrix-zstd
MATRIX_NEUTRON=${RUN_ROOT}/matrix-neutron
MATRIX_STOCKFISH=${RUN_ROOT}/matrix-stockfish
SAMPLE_TIMEOUT_SECONDS=${SAMPLE_TIMEOUT_SECONDS:-7200}
ROI_SAFETY_MULTIPLIER=${ROI_SAFETY_MULTIPLIER:-100}

BASE_DISK=${TCSIM_ROOT}/data/spec2026_diskimg/spec2026.ext4
ORIGINAL_DISK=${TCSIM_ROOT}/data/spec2026_diskimg/spec2026-usergate-original.ext4
WARMTRACE_DISK=${TCSIM_ROOT}/data/spec2026_diskimg/spec2026-native-multicore-warmtrace.ext4

fail() {
  echo "[scoped-frontend][ERROR] $*" >&2
  exit 2
}

pid_is_running() {
  [[ -s "${PID_FILE}" ]] || return 1
  local pid
  pid=$(<"${PID_FILE}")
  [[ "${pid}" =~ ^[0-9]+$ ]] || return 1
  kill -0 "${pid}" 2>/dev/null || return 1
  local command
  command=$(ps -ww -p "${pid}" -o args= 2>/dev/null) || return 1
  [[ "${command}" == *"${BASH_SOURCE[0]} worker"* ]]
}

require_contract() {
  grep -Fq 'class TaoTraceFrontendRegistry' \
    "${GEM5_ROOT}/src/mem/taotrace_response.hh" || \
    fail "gem5 scoped frontend registry is not applied"
  grep -Fq 'taotrace-scoped-frontend-v1' \
    "${GEM5_ROOT}/src/cpu/o3/probe/tao_trace.cc" || \
    fail "gem5 scoped frontend oracle output is not applied"
  grep -Fq 'exact-cpl-first-event-to-functional-target-window' \
    "${GEM5_ROOT}/src/cpu/o3/probe/tao_trace.cc" || \
    fail "gem5 frontend ledger is not aligned to the CPL start boundary"
  grep -Fq 'frontend_accounting' \
    "${TCSIM_ROOT}/scripts/gem5_fs_roi.py" || \
    fail "TCSim scoped frontend merger is not applied"
}

run_case() {
  local matrix=$1
  local disk=$2
  local workload=$3
  "${PYTHON_BIN}" "${MATRIX_RUNNER}" \
    --stage sample \
    --workloads "${workload}" \
    --cores 4 \
    --roi-insts "${TARGET_RECORDS}" \
    --roi-target-domain user-fst \
    --roi-safety-multiplier "${ROI_SAFETY_MULTIPLIER}" \
    --roi-stop-policy all-core \
    --warmup-mode source \
    --sample-timeout-seconds "${SAMPLE_TIMEOUT_SECONDS}" \
    --sample-jobs 1 \
    --prepare-jobs 1 \
    --matrix-root "${matrix}" \
    --result-root "${RESULT_ROOT}" \
    --tmp-root "${DRIVER_TMP_ROOT}" \
    --trace-tmp-root "${TRACE_TMP_ROOT}" \
    --gem5-root "${GEM5_ROOT}" \
    --aux-disk "${disk}" \
    --emit-functional-trace \
    --trace-format fst \
    --measure-cpl \
    --native-anomaly-limit 32 \
    --functional-user-only \
    --reuse-binary-mismatch \
    --reuse-restore-config-mismatch
}

result_for() {
  local matrix=$1
  jq -er '.tasks[] | select(.sample.status == "completed" or
    (.sample.status == "skipped" and
     .sample.reason == "current successful result exists")) |
    .sample.result_dir' "${matrix}/status.json"
}

audit_matrix() {
  local matrix=$1
  local result
  result=$(result_for "${matrix}")
  "${PYTHON_BIN}" "${FASTSIM_ROOT}/tools/audit_scoped_frontend_ledger.py" \
    "${result}/oracle"
}

worker() {
  require_contract
  unset PYTHONHOME
  export PYTHONUNBUFFERED=1
  mkdir -p "${AUDIT_ROOT}"
  echo "[scoped-frontend] started=$(date -Is) target=${TARGET_RECORDS}"
  echo "[scoped-frontend] phase=nab-smoke"
  run_case "${MATRIX_NAB}" "${WARMTRACE_DISK}" 816.nab_s
  audit_matrix "${MATRIX_NAB}"

  echo "[scoped-frontend] phase=three-controls"
  run_case "${MATRIX_ZSTD}" "${BASE_DISK}" 777.zstd_r &
  local zstd_pid=$!
  run_case "${MATRIX_NEUTRON}" "${WARMTRACE_DISK}" 881.neutron_s &
  local neutron_pid=$!
  run_case "${MATRIX_STOCKFISH}" "${ORIGINAL_DISK}" 706.stockfish_r &
  local stockfish_pid=$!
  local failed=0
  wait "${zstd_pid}" || failed=1
  wait "${neutron_pid}" || failed=1
  wait "${stockfish_pid}" || failed=1
  [[ "${failed}" -eq 0 ]] || fail "one or more control pilots failed"
  audit_matrix "${MATRIX_ZSTD}"
  audit_matrix "${MATRIX_NEUTRON}"
  audit_matrix "${MATRIX_STOCKFISH}"
  echo "[scoped-frontend] completed=$(date -Is)"
}

show_status() {
  if pid_is_running; then
    local pid
    pid=$(<"${PID_FILE}")
    echo "[scoped-frontend] running pid=${pid}"
    ps -p "${pid}" -o pid,ppid,stat,pcpu,pmem,etime,args
  else
    echo "[scoped-frontend] not running"
  fi
  local matrix
  for matrix in \
    "${MATRIX_NAB}" "${MATRIX_ZSTD}" \
    "${MATRIX_NEUTRON}" "${MATRIX_STOCKFISH}"; do
    if [[ -f "${matrix}/status.json" ]]; then
      jq -c '{matrix:input_filename,summary,tasks}' "${matrix}/status.json"
    fi
  done
  [[ -f "${EXIT_FILE}" ]] && echo "[scoped-frontend] exit=$(<"${EXIT_FILE}")"
  echo "[scoped-frontend] log=${LOG_FILE}"
}

mkdir -p "${RUN_ROOT}"

case "${action}" in
  start)
    pid_is_running && fail "launcher already running: $(<"${PID_FILE}")"
    rm -f "${EXIT_FILE}"
    : >"${LOG_FILE}"
    nohup setsid bash "${BASH_SOURCE[0]}" worker \
      "${RUN_TAG}" "${TARGET_RECORDS}" \
      >"${LOG_FILE}" 2>&1 </dev/null &
    launcher_pid=$!
    printf '%s\n' "${launcher_pid}" >"${PID_FILE}"
    echo "[scoped-frontend] started pid=${launcher_pid}"
    echo "[scoped-frontend] log=${LOG_FILE}"
    ;;
  worker)
    printf '%s\n' "$$" >"${PID_FILE}"
    trap 'status=$?; printf "%s\n" "${status}" >"${EXIT_FILE}"; trap - EXIT; exit "${status}"' EXIT
    worker
    ;;
  status)
    show_status
    ;;
  stop)
    if pid_is_running; then
      launcher_pid=$(<"${PID_FILE}")
      kill -TERM -- "-${launcher_pid}"
      echo "[scoped-frontend] stop requested pgid=${launcher_pid}"
    else
      echo "[scoped-frontend] not running"
    fi
    ;;
  *)
    fail "usage: $0 [start|worker|status|stop] [run-tag] [target-records]"
    ;;
esac
