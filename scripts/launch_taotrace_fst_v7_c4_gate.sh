#!/usr/bin/env bash
set -euo pipefail

FASTSIM_ROOT=/data00/yinhaolang/FastSim
TCSIM_ROOT=/data00/yinhaolang/TCSim
GEM5_ROOT=/data00/yinhaolang/gem5-fs
PYTHON_BIN=${PYTHON_BIN:-/data00/yinhaolang/infer/.venv/bin/python}
export FASTSIM_EFFECTIVE_TARGET_GENERATOR=${FASTSIM_ROOT}/tools/generate_fs_effective_target.py
export FASTSIM_EFFECTIVE_TARGET_PYTHON=${PYTHON_BIN}
TAOGEN_SHARED_ROOT=${TAOGEN_SHARED:-/data00/yinhaolang/taogen/shared}
MATRIX_RUNNER=${TCSIM_ROOT}/scripts/run_gem5_fs_cpi_matrix.py
STRICT_VALIDATOR=${TCSIM_ROOT}/scripts/validate_gem5_usergate_result.py

action=${1:-start}
RUN_TAG=${2:-${RUN_TAG:-taotrace-fst-v7-c4-gate-20260815}}
RUN_ROOT=${FASTSIM_ROOT}/tmp/${RUN_TAG}
CANONICAL_EVENT_DICTIONARY=${FASTSIM_ROOT}/configs/pmu-event-dictionary-v1.json
FROZEN_EVENT_DICTIONARY=${RUN_ROOT}/pmu-event-dictionary-v1.json
export FASTSIM_EVENT_DICTIONARY=${FROZEN_EVENT_DICTIONARY}
RESULT_ROOT=${RUN_ROOT}/source
TRACE_TMP_ROOT=${RUN_ROOT}/trace-scratch
DRIVER_TMP_ROOT=${RUN_ROOT}/driver-tmp
MATRIX_BASE=${RUN_ROOT}/matrix-base
MATRIX_SPH=${RUN_ROOT}/matrix-sph
MATRIX_WARMTRACE=${RUN_ROOT}/matrix-warmtrace
AUDIT_ROOT=${RUN_ROOT}/audit
LOG_FILE=${RUN_ROOT}/launch.log
PID_FILE=${RUN_ROOT}/launcher.pid
EXIT_FILE=${RUN_ROOT}/exit.code
TARGET_RECORDS=${3:-${TARGET_RECORDS:-500000}}
CORES=${CORES:-4}
SAMPLE_TIMEOUT_SECONDS=${SAMPLE_TIMEOUT_SECONDS:-28800}
ROI_SAFETY_MULTIPLIER=${ROI_SAFETY_MULTIPLIER:-100}
NATIVE_ANOMALY_LIMIT=${NATIVE_ANOMALY_LIMIT:-32}

BASE_DISK=${TCSIM_ROOT}/data/spec2026_diskimg/spec2026.ext4
SPH_DISK=${TCSIM_ROOT}/data/spec2026_diskimg/spec2026-usergate-extended-v2.ext4
WARMTRACE_DISK=${TCSIM_ROOT}/data/spec2026_diskimg/spec2026-native-multicore-warmtrace.ext4

fail() {
  echo "[taotrace-c4-gate][ERROR] $*" >&2
  exit 2
}

require_p0_external_contract() {
  grep -Fq 'taotrace-path-class-v3' \
    "${GEM5_ROOT}/src/cpu/o3/probe/tao_trace.cc" || \
    fail "gem5/TCSim P0 patch is not applied; see patches/README.md"
  grep -Fq 'treeVictim' "${TAOGEN_SHARED_ROOT}/lru_banked.hh" || \
    fail "TaoTrace TreePLRU support is not applied; see patches/README.md"
  grep -Fq 'FASTSIM_EFFECTIVE_TARGET_GENERATOR must name' \
    "${TCSIM_ROOT}/configs/gem5/x86_fs_kvm_boot_checkpoint_tao.py" || \
    fail "final-config sidecar hook is not applied; see patches/README.md"
  grep -Fq 'TaoTraceNativeAccessRegistry::noteResponse' \
    "${GEM5_ROOT}/src/mem/ruby/system/Sequencer.cc" || \
    fail "P1 Ruby native-response sideband is not applied; see patches/README.md"
  grep -Fq 'TaoTraceNativeAccessRegistry::noteAdmission' \
    "${GEM5_ROOT}/src/mem/ruby/system/Sequencer.cc" || \
    fail "P1 Ruby native-admission lifecycle is not applied; see patches/README.md"
  grep -Fq 'committed-native-drain' \
    "${GEM5_ROOT}/src/cpu/o3/probe/tao_trace.cc" || \
    fail "P1 committed target drain is not applied; see patches/README.md"
  grep -Fq 'taotrace-native-response-v6' \
    "${GEM5_ROOT}/src/cpu/o3/probe/tao_trace.cc" || \
    fail "P2 native Ruby hierarchy/transaction schema is not applied; see patches/README.md"
  grep -Fq 'taotrace-native-summary-v1' \
    "${GEM5_ROOT}/src/cpu/o3/probe/tao_trace.cc" || \
    fail "online native Ruby summary is not applied; see patches/README.md"
  grep -Fq 'emit_native_response_jsonl = Param.Bool(False' \
    "${GEM5_ROOT}/src/cpu/o3/probe/TaoTrace.py" || \
    fail "full native JSONL is not default-off; see patches/README.md"
  grep -Fq 'taotraceCacheOutcome' \
    "${GEM5_ROOT}/src/mem/ruby/slicc_interface/RubySlicc_Util.hh" || \
    fail "P2 Ruby controller outcome hooks are not applied; see patches/README.md"
}

pid_is_running() {
  [[ -s "${PID_FILE}" ]] || return 1
  local pid
  pid=$(<"${PID_FILE}")
  [[ "${pid}" =~ ^[0-9]+$ ]] || return 1
  kill -0 "${pid}" 2>/dev/null || return 1
  local command
  command=$(ps -p "${pid}" -o args= 2>/dev/null) || return 1
  [[ "${command}" == *"${BASH_SOURCE[0]} worker"* ]]
}

show_status() {
  if pid_is_running; then
    local pid
    pid=$(<"${PID_FILE}")
    echo "[taotrace-c4-gate] running pid=${pid}"
    ps -p "${pid}" -o pid,ppid,stat,pcpu,pmem,etime,args
  else
    echo "[taotrace-c4-gate] not running"
  fi
  for matrix in "${MATRIX_BASE}" "${MATRIX_SPH}" "${MATRIX_WARMTRACE}"; do
    if [[ -f "${matrix}/status.json" ]]; then
      jq -c '{matrix:input_filename,summary,tasks}' "${matrix}/status.json"
    fi
  done
  if [[ -f "${EXIT_FILE}" ]]; then
    echo "[taotrace-c4-gate] exit=$(<"${EXIT_FILE}")"
  fi
  echo "[taotrace-c4-gate] log=${LOG_FILE}"
  echo "[taotrace-c4-gate] audit=${AUDIT_ROOT}/matrix-integrity.json"
}

run_matrix() {
  local matrix_root=$1
  local aux_disk=$2
  local jobs=$3
  shift 3
  "${PYTHON_BIN}" "${MATRIX_RUNNER}" \
    --stage sample \
    --workloads "$@" \
    --cores "${CORES}" \
    --roi-insts "${TARGET_RECORDS}" \
    --roi-target-domain user-fst \
    --roi-safety-multiplier "${ROI_SAFETY_MULTIPLIER}" \
    --roi-stop-policy all-core \
    --warmup-mode source \
    --sample-timeout-seconds "${SAMPLE_TIMEOUT_SECONDS}" \
    --sample-jobs "${jobs}" \
    --prepare-jobs "${jobs}" \
    --matrix-root "${matrix_root}" \
    --result-root "${RESULT_ROOT}" \
    --tmp-root "${DRIVER_TMP_ROOT}" \
    --trace-tmp-root "${TRACE_TMP_ROOT}" \
    --gem5-root "${GEM5_ROOT}" \
    --aux-disk "${aux_disk}" \
    --emit-functional-trace \
    --trace-format fst \
    --measure-cpl \
    --native-anomaly-limit "${NATIVE_ANOMALY_LIMIT}" \
    --functional-user-only \
    --reuse-binary-mismatch \
    --reuse-restore-config-mismatch
}

validate_case() {
  local matrix=$1
  local workload=$2
  local key="${CORES}c/${workload}"
  local result
  result=$(jq -r --arg key "${key}" \
    '.tasks[$key].sample.result_dir // empty' "${matrix}/status.json")
  [[ -n "${result}" && -d "${result}" ]] || \
    fail "missing result for ${key} in ${matrix}"
  local binary_sha aux_sha actual_target_records
  binary_sha=$(jq -r '.workload_binary_sha256' "${result}/request.json")
  aux_sha=$(jq -r '.aux_disk.sha256' "${result}/request.json")
  actual_target_records=$(jq -er '.sampling.roi_insts' "${result}/request.json")
  "${PYTHON_BIN}" "${STRICT_VALIDATOR}" \
    "${matrix}" "${workload}" "${CORES}" "${actual_target_records}" \
    --expected-binary-sha256 "${binary_sha}" \
    --expected-aux-sha256 "${aux_sha}" \
    --result-root "${RESULT_ROOT}/sample"
  [[ -s "${result}/effective-target.json" ]] || \
    fail "runtime did not emit effective-target.json for ${key}"
  "${PYTHON_BIN}" \
    "${FASTSIM_ROOT}/tools/validate_fs_oracle_identity.py" \
    --result "${result}" \
    --event-dictionary "${FROZEN_EVENT_DICTIONARY}" \
    --output "${AUDIT_ROOT}/target-${workload}.json"
  "${PYTHON_BIN}" \
    "${FASTSIM_ROOT}/tools/merge_kernel_events_oracle_v3.py" \
    "${result}/oracle" \
    --output "${result}/oracle/kernel_events.json"
  "${PYTHON_BIN}" "${FASTSIM_ROOT}/tools/validate_kernel_events_oracle.py" \
    "${result}/oracle/kernel_events.json" \
    >"${AUDIT_ROOT}/kernel-${workload}.json"
}

run_audits() {
  mkdir -p "${AUDIT_ROOT}"
  "${PYTHON_BIN}" "${FASTSIM_ROOT}/tools/audit_functional_warmup_matrix.py" \
    --matrix "${MATRIX_BASE}" \
    --matrix "${MATRIX_SPH}" \
    --matrix "${MATRIX_WARMTRACE}" \
    --output "${AUDIT_ROOT}/matrix-integrity.json" \
    --expected-cases 4 \
    --expected-fst-files "$((4 * CORES))" \
    --require-destination-classes

  validate_case "${MATRIX_BASE}" 777.zstd_r
  validate_case "${MATRIX_BASE}" 854.graph500_s
  validate_case "${MATRIX_SPH}" 803.sph_exa_s
  validate_case "${MATRIX_WARMTRACE}" 857.namd_s

  local trace_args=()
  while IFS= read -r result; do
    trace_args+=(--trace-dir "${result}/tao_trace")
  done < <(jq -r '.cases[].result_dir' "${AUDIT_ROOT}/matrix-integrity.json")
  "${PYTHON_BIN}" "${FASTSIM_ROOT}/tools/audit_fst_syscall_metadata.py" \
    "${trace_args[@]}" \
    --require-entry-coverage \
    --require-semantic-plausibility \
    --output "${AUDIT_ROOT}/syscall-metadata.json" \
    >"${AUDIT_ROOT}/syscall-metadata.stdout.json"
  "${PYTHON_BIN}" "${FASTSIM_ROOT}/tools/audit_fst_virtual_page_map.py" \
    "${trace_args[@]}" \
    --output "${AUDIT_ROOT}/virtual-page-map.json" \
    >"${AUDIT_ROOT}/virtual-page-map.stdout.json"
  "${PYTHON_BIN}" \
    "${FASTSIM_ROOT}/tools/audit_p1_native_response_sideband.py" \
    --matrix "${MATRIX_BASE}" \
    --matrix "${MATRIX_SPH}" \
    --matrix "${MATRIX_WARMTRACE}" \
    --output "${AUDIT_ROOT}/native-summary.json" \
    >"${AUDIT_ROOT}/native-summary.stdout.json"
}

worker() {
  require_p0_external_contract
  unset PYTHONHOME
  export PYTHONUNBUFFERED=1
  echo "[taotrace-c4-gate] started=$(date -Is) cores=${CORES} target=${TARGET_RECORDS}"
  echo "[taotrace-c4-gate] run_root=${RUN_ROOT}"

  run_matrix "${MATRIX_BASE}" "${BASE_DISK}" 2 \
    777.zstd_r 854.graph500_s &
  local pid_base=$!
  run_matrix "${MATRIX_SPH}" "${SPH_DISK}" 1 \
    803.sph_exa_s &
  local pid_sph=$!
  run_matrix "${MATRIX_WARMTRACE}" "${WARMTRACE_DISK}" 1 \
    857.namd_s &
  local pid_warm=$!

  local failed=0
  wait "${pid_base}" || failed=1
  wait "${pid_sph}" || failed=1
  wait "${pid_warm}" || failed=1
  if [[ "${failed}" -ne 0 ]]; then
    echo "[taotrace-c4-gate][ERROR] one or more matrices failed" >&2
    return 1
  fi

  run_audits
  echo "[taotrace-c4-gate] completed=$(date -Is)"
}

mkdir -p "${RUN_ROOT}"

case "${action}" in
  start)
    pid_is_running && fail "launcher already running: $(<"${PID_FILE}")"
    if [[ -e "${FROZEN_EVENT_DICTIONARY}" ]]; then
      cmp -s "${CANONICAL_EVENT_DICTIONARY}" "${FROZEN_EVENT_DICTIONARY}" || \
        fail "run tag is already bound to a different PMU event dictionary"
    else
      cp -a "${CANONICAL_EVENT_DICTIONARY}" "${FROZEN_EVENT_DICTIONARY}"
    fi
    : >"${LOG_FILE}"
    nohup setsid bash "${BASH_SOURCE[0]}" worker \
      "${RUN_TAG}" "${TARGET_RECORDS}" \
      >"${LOG_FILE}" 2>&1 </dev/null &
    launcher_pid=$!
    printf '%s\n' "${launcher_pid}" >"${PID_FILE}"
    echo "[taotrace-c4-gate] started pid=${launcher_pid}"
    echo "[taotrace-c4-gate] status: bash ${BASH_SOURCE[0]} status"
    echo "[taotrace-c4-gate] log=${LOG_FILE}"
    ;;
  worker)
    printf '%s\n' "$$" >"${PID_FILE}"
    trap 'status=$?; printf "%s\n" "${status}" >"${EXIT_FILE}"; trap - EXIT; exit "${status}"' EXIT
    worker
    ;;
  audit)
    trap 'status=$?; printf "%s\n" "${status}" >"${EXIT_FILE}"; trap - EXIT; exit "${status}"' EXIT
    run_audits
    ;;
  status)
    show_status
    ;;
  stop)
    if pid_is_running; then
      launcher_pid=$(<"${PID_FILE}")
      kill -TERM -- "-${launcher_pid}"
      echo "[taotrace-c4-gate] stop requested pgid=${launcher_pid}"
    else
      echo "[taotrace-c4-gate] not running"
    fi
    ;;
  *)
    fail "usage: $0 [start|status|stop|worker|audit] [run-tag] [target-records]"
    ;;
esac
