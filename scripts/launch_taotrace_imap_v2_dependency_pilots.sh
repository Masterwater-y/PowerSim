#!/usr/bin/env bash
set -euo pipefail

FASTSIM_ROOT=/data00/yinhaolang/FastSim
TCSIM_ROOT=/data00/yinhaolang/TCSim
GEM5_ROOT=/data00/yinhaolang/gem5-fs
PYTHON_BIN=${PYTHON_BIN:-/data00/yinhaolang/infer/.venv/bin/python}
MATRIX_RUNNER=${TCSIM_ROOT}/scripts/run_gem5_fs_cpi_matrix.py

RUN_TAG=${RUN_TAG:-taotrace-imap-v2-dependency-c4-pilots-20260817}
RUN_ROOT=${FASTSIM_ROOT}/tmp/${RUN_TAG}
RESULT_ROOT=${RUN_ROOT}/source
TRACE_TMP_ROOT=${RUN_ROOT}/trace-scratch
DRIVER_TMP_ROOT=${RUN_ROOT}/driver-tmp
MATRIX_GRAPH500=${RUN_ROOT}/matrix-graph500
MATRIX_STOCKFISH=${RUN_ROOT}/matrix-stockfish
MATRIX_NAB=${RUN_ROOT}/matrix-nab
MATRIX_TEALEAF=${RUN_ROOT}/matrix-tealeaf
AUDIT_ROOT=${RUN_ROOT}/audit
LOG_FILE=${RUN_ROOT}/launch.log
PID_FILE=${RUN_ROOT}/launcher.pid
EXIT_FILE=${RUN_ROOT}/exit.code
TARGET_RECORDS=${TARGET_RECORDS:-100000}
SAMPLE_TIMEOUT_SECONDS=${SAMPLE_TIMEOUT_SECONDS:-3600}
ROI_SAFETY_MULTIPLIER=${ROI_SAFETY_MULTIPLIER:-100}

GRAPH500_DISK=${TCSIM_ROOT}/data/spec2026_diskimg/spec2026.ext4
STOCKFISH_DISK=${TCSIM_ROOT}/data/spec2026_diskimg/spec2026-usergate-original.ext4
NAB_DISK=${TCSIM_ROOT}/data/spec2026_diskimg/spec2026-native-multicore-warmtrace.ext4
TEALEAF_DISK=${TCSIM_ROOT}/data/spec2026_diskimg/spec2026.ext4

action=${1:-start}

fail() {
  echo "[imap-v2-dependency-pilots][ERROR] $*" >&2
  exit 2
}

pid_is_running() {
  [[ -s "${PID_FILE}" ]] || return 1
  local pid
  pid=$(<"${PID_FILE}")
  [[ "${pid}" =~ ^[0-9]+$ ]] || return 1
  kill -0 "${pid}" 2>/dev/null
}

run_case() {
  local matrix_root=$1
  local aux_disk=$2
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
    --matrix-root "${matrix_root}" \
    --result-root "${RESULT_ROOT}" \
    --tmp-root "${DRIVER_TMP_ROOT}" \
    --trace-tmp-root "${TRACE_TMP_ROOT}" \
    --gem5-root "${GEM5_ROOT}" \
    --aux-disk "${aux_disk}" \
    --emit-functional-trace \
    --trace-format fst \
    --measure-cpl \
    --functional-user-only \
    --reuse-binary-mismatch \
    --reuse-restore-config-mismatch
}

run_audits() {
  mkdir -p "${AUDIT_ROOT}"
  "${PYTHON_BIN}" "${FASTSIM_ROOT}/tools/audit_functional_warmup_matrix.py" \
    --matrix "${MATRIX_GRAPH500}" \
    --matrix "${MATRIX_STOCKFISH}" \
    --matrix "${MATRIX_NAB}" \
    --matrix "${MATRIX_TEALEAF}" \
    --output "${AUDIT_ROOT}/matrix-integrity.json" \
    --expected-cases 4 \
    --expected-fst-files 16 \
    --require-destination-classes
  "${PYTHON_BIN}" \
    "${FASTSIM_ROOT}/tools/audit_fst_static_instruction_maps.py" \
    --require-map \
    --require-operands \
    --output "${AUDIT_ROOT}/static-instruction-maps.json" \
    "${RESULT_ROOT}"
}

worker() {
  unset PYTHONHOME
  export PYTHONUNBUFFERED=1
  echo "[imap-v2-dependency-pilots] started=$(date -Is)"
  echo "[imap-v2-dependency-pilots] root=${RUN_ROOT}"
  run_case "${MATRIX_GRAPH500}" "${GRAPH500_DISK}" \
    854.graph500_s &
  local graph500_pid=$!
  run_case "${MATRIX_STOCKFISH}" "${STOCKFISH_DISK}" \
    706.stockfish_r &
  local stockfish_pid=$!
  run_case "${MATRIX_NAB}" "${NAB_DISK}" \
    816.nab_s &
  local nab_pid=$!
  run_case "${MATRIX_TEALEAF}" "${TEALEAF_DISK}" \
    811.tealeaf_s &
  local tealeaf_pid=$!
  local failed=0
  wait "${graph500_pid}" || failed=1
  wait "${stockfish_pid}" || failed=1
  wait "${nab_pid}" || failed=1
  wait "${tealeaf_pid}" || failed=1
  if [[ "${failed}" -ne 0 ]]; then
    fail "one or more parallel C4 pilots failed"
  fi
  run_audits
  echo "[imap-v2-dependency-pilots] completed=$(date -Is)"
}

show_status() {
  if pid_is_running; then
    local pid
    pid=$(<"${PID_FILE}")
    echo "[imap-v2-dependency-pilots] running pid=${pid}"
    ps -p "${pid}" -o pid,ppid,stat,pcpu,pmem,etime,args
  else
    echo "[imap-v2-dependency-pilots] not running"
  fi
  local matrix
  for matrix in \
    "${MATRIX_GRAPH500}" "${MATRIX_STOCKFISH}" \
    "${MATRIX_NAB}" "${MATRIX_TEALEAF}"; do
    if [[ -f "${matrix}/status.json" ]]; then
      jq -c '{matrix:input_filename,summary,tasks}' \
        "${matrix}/status.json"
    fi
  done
  if [[ -f "${EXIT_FILE}" ]]; then
    echo "[imap-v2-dependency-pilots] exit=$(<"${EXIT_FILE}")"
  fi
  echo "[imap-v2-dependency-pilots] log=${LOG_FILE}"
}

mkdir -p "${RUN_ROOT}"

case "${action}" in
  start)
    pid_is_running && fail "launcher already running: $(<"${PID_FILE}")"
    : >"${LOG_FILE}"
    nohup setsid bash "${BASH_SOURCE[0]}" worker \
      >"${LOG_FILE}" 2>&1 </dev/null &
    launcher_pid=$!
    printf '%s\n' "${launcher_pid}" >"${PID_FILE}"
    echo "[imap-v2-dependency-pilots] started pid=${launcher_pid}"
    echo "[imap-v2-dependency-pilots] status: " \
      "bash ${BASH_SOURCE[0]} status"
    ;;
  worker)
    printf '%s\n' "$$" >"${PID_FILE}"
    trap 'status=$?; printf "%s\n" "${status}" >"${EXIT_FILE}"; trap - EXIT; exit "${status}"' EXIT
    worker
    ;;
  audit)
    run_audits
    ;;
  status)
    show_status
    ;;
  *)
    fail "usage: $0 [start|status|worker|audit]"
    ;;
esac
