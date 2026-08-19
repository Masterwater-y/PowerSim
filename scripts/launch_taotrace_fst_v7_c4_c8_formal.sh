#!/usr/bin/env bash
set -euo pipefail

FASTSIM_ROOT=/data00/yinhaolang/FastSim
SCRIPT_PATH=${FASTSIM_ROOT}/scripts/launch_taotrace_fst_v7_c4_c8_formal.sh
TCSIM_ROOT=/data00/yinhaolang/TCSim
GEM5_ROOT=/data00/yinhaolang/gem5-fs
PYTHON_BIN=${PYTHON_BIN:-/data00/yinhaolang/infer/.venv/bin/python}
export FASTSIM_EFFECTIVE_TARGET_GENERATOR=${FASTSIM_ROOT}/tools/generate_fs_effective_target.py
export FASTSIM_EFFECTIVE_TARGET_PYTHON=${PYTHON_BIN}
TAOGEN_SHARED_ROOT=${TAOGEN_SHARED:-/data00/yinhaolang/taogen/shared}
MATRIX_RUNNER=${TCSIM_ROOT}/scripts/run_gem5_fs_cpi_matrix.py
STRICT_VALIDATOR=${TCSIM_ROOT}/scripts/validate_gem5_usergate_result.py

RUN_TAG=${2:-${RUN_TAG:-taotrace-native-summary-c4-c8-10m-20260819}}
RUN_ROOT=${FASTSIM_ROOT}/tmp/${RUN_TAG}
CANONICAL_EVENT_DICTIONARY=${FASTSIM_ROOT}/configs/pmu-event-dictionary-v1.json
FROZEN_EVENT_DICTIONARY=${RUN_ROOT}/pmu-event-dictionary-v1.json
export FASTSIM_EVENT_DICTIONARY=${FROZEN_EVENT_DICTIONARY}
RESULT_ROOT=${RUN_ROOT}/source
TRACE_TMP_ROOT=${RUN_ROOT}/trace-scratch
DRIVER_TMP_ROOT=${RUN_ROOT}/driver-tmp
MATRIX_BASE=${RUN_ROOT}/matrix-base
MATRIX_STOCKFISH=${RUN_ROOT}/matrix-stockfish
MATRIX_SPH=${RUN_ROOT}/matrix-sph
MATRIX_WARMTRACE=${RUN_ROOT}/matrix-warmtrace
AUDIT_ROOT=${RUN_ROOT}/audit
DATASET_ROOT=${RUN_ROOT}/fst-v7
ACCURACY_ROOT=${RUN_ROOT}/accuracy
LOG_FILE=${RUN_ROOT}/launch.log
PID_FILE=${RUN_ROOT}/launcher.pid
EXIT_FILE=${RUN_ROOT}/exit.code
WATCHDOG_PID_FILE=${RUN_ROOT}/watchdog.pid
WORKER_EXIT_FILE=${RUN_ROOT}/worker.exit.code
WATCHDOG_LOG=${RUN_ROOT}/watchdog.log
WATCHDOG_STATE_FILE=${RUN_ROOT}/watchdog.state
TARGET_RECORDS=${TARGET_RECORDS:-10000000}
SAMPLE_TIMEOUT_SECONDS=${SAMPLE_TIMEOUT_SECONDS:-28800}
ROI_SAFETY_MULTIPLIER=${ROI_SAFETY_MULTIPLIER:-100}
MAX_HIERARCHY_GAP_RATIO=${MAX_HIERARCHY_GAP_RATIO:-0.0002}
NATIVE_ANOMALY_LIMIT=${NATIVE_ANOMALY_LIMIT:-32}
MAX_ATTEMPTS=${MAX_ATTEMPTS:-20}
RETRY_DELAY_SECONDS=${RETRY_DELAY_SECONDS:-60}
POSTPROCESS_AFTER_COLLECTION=${POSTPROCESS_AFTER_COLLECTION:-0}

BASE_DISK=${TCSIM_ROOT}/data/spec2026_diskimg/spec2026.ext4
STOCKFISH_DISK=${TCSIM_ROOT}/data/spec2026_diskimg/spec2026-usergate-original.ext4
SPH_DISK=${TCSIM_ROOT}/data/spec2026_diskimg/spec2026-usergate-extended-v2.ext4
WARMTRACE_DISK=${TCSIM_ROOT}/data/spec2026_diskimg/spec2026-native-multicore-warmtrace.ext4

action=${1:-start}

fail() {
  echo "[taotrace-formal][ERROR] $*" >&2
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
    fail "P2 native Ruby hierarchy schema is not applied; see patches/README.md"
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

pid_file_is_running() {
  local file=$1
  local action_name=$2
  [[ -s "${file}" ]] || return 1
  local pid command
  pid=$(<"${file}")
  [[ "${pid}" =~ ^[0-9]+$ ]] || return 1
  kill -0 "${pid}" 2>/dev/null || return 1
  command=$(ps -p "${pid}" -o args= 2>/dev/null) || return 1
  [[ "${command}" == *"${SCRIPT_PATH} ${action_name}"* ]]
}

show_status() {
  if pid_file_is_running "${WATCHDOG_PID_FILE}" watchdog; then
    local watchdog_pid
    watchdog_pid=$(<"${WATCHDOG_PID_FILE}")
    echo "[taotrace-formal] watchdog running pid=${watchdog_pid}"
    ps -p "${watchdog_pid}" -o pid,ppid,pgid,stat,pcpu,pmem,etime,args
  else
    echo "[taotrace-formal] watchdog not running"
  fi
  if pid_file_is_running "${PID_FILE}" worker; then
    local pid
    pid=$(<"${PID_FILE}")
    echo "[taotrace-formal] worker running pid=${pid}"
    ps -p "${pid}" -o pid,ppid,pgid,stat,pcpu,pmem,etime,args
  else
    echo "[taotrace-formal] worker not running"
  fi
  [[ -f "${WATCHDOG_STATE_FILE}" ]] && \
    echo "[taotrace-formal] watchdog_state=$(<"${WATCHDOG_STATE_FILE}")"
  local matrix
  for matrix in \
    "${MATRIX_BASE}" "${MATRIX_STOCKFISH}" \
    "${MATRIX_SPH}" "${MATRIX_WARMTRACE}"; do
    if [[ -f "${matrix}/status.json" ]]; then
      jq -c '
        {matrix:input_filename, summary,
         sample_states:(.tasks | to_entries | group_by(.value.sample.status)
           | map({status:.[0].value.sample.status,count:length}))}
      ' "${matrix}/status.json"
    fi
  done
  if [[ -f "${EXIT_FILE}" ]]; then
    echo "[taotrace-formal] exit=$(<"${EXIT_FILE}")"
  fi
  echo "[taotrace-formal] log=${LOG_FILE}"
  echo "[taotrace-formal] watchdog_log=${WATCHDOG_LOG}"
  echo "[taotrace-formal] audit=${AUDIT_ROOT}/matrix-integrity.json"
  echo "[taotrace-formal] accuracy=${ACCURACY_ROOT}"
}

run_matrix() {
  local matrix_root=$1
  local aux_disk=$2
  local jobs=$3
  shift 3
  "${PYTHON_BIN}" "${MATRIX_RUNNER}" \
    --stage sample \
    --workloads "$@" \
    --cores 4 8 \
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

validate_matrix_cases() {
  local matrix=$1
  while IFS=$'\t' read -r key result; do
    [[ -n "${key}" && -n "${result}" ]] || \
      fail "incomplete task in ${matrix}: ${key}"
    local core_text=${key%%/*}
    local cores=${core_text%c}
    local workload=${key#*/}
    local binary_sha aux_sha
    binary_sha=$(jq -r '.workload_binary_sha256' "${result}/request.json")
    aux_sha=$(jq -r '.aux_disk.sha256' "${result}/request.json")
    "${PYTHON_BIN}" "${STRICT_VALIDATOR}" \
      "${matrix}" "${workload}" "${cores}" "${TARGET_RECORDS}" \
      --expected-binary-sha256 "${binary_sha}" \
      --expected-aux-sha256 "${aux_sha}" \
      --result-root "${RESULT_ROOT}/sample"
    [[ -s "${result}/effective-target.json" ]] || \
      fail "runtime did not emit effective-target.json for ${key}"
    "${PYTHON_BIN}" \
      "${FASTSIM_ROOT}/tools/validate_fs_oracle_identity.py" \
      --result "${result}" \
      --event-dictionary "${FROZEN_EVENT_DICTIONARY}" \
      --output "${AUDIT_ROOT}/target-${cores}c-${workload}.json"
    "${PYTHON_BIN}" \
      "${FASTSIM_ROOT}/tools/merge_kernel_events_oracle_v3.py" \
      "${result}/oracle" \
      --output "${result}/oracle/kernel_events.json"
    "${PYTHON_BIN}" "${FASTSIM_ROOT}/tools/validate_kernel_events_oracle.py" \
      "${result}/oracle/kernel_events.json" \
      >"${AUDIT_ROOT}/kernel-${cores}c-${workload}.json"
  done < <(
    jq -r \
      '.tasks | to_entries[] | [.key, .value.sample.result_dir] | @tsv' \
      "${matrix}/status.json"
  )
}

run_audits() {
  mkdir -p "${AUDIT_ROOT}"
  local matrices=(
    --matrix "${MATRIX_BASE}"
    --matrix "${MATRIX_STOCKFISH}"
    --matrix "${MATRIX_SPH}"
    --matrix "${MATRIX_WARMTRACE}"
  )
  "${PYTHON_BIN}" "${FASTSIM_ROOT}/tools/audit_functional_warmup_matrix.py" \
    "${matrices[@]}" \
    --output "${AUDIT_ROOT}/matrix-integrity.json" \
    --expected-cases 20 \
    --expected-fst-files 120 \
    --require-destination-classes

  validate_matrix_cases "${MATRIX_BASE}"
  validate_matrix_cases "${MATRIX_STOCKFISH}"
  validate_matrix_cases "${MATRIX_SPH}"
  validate_matrix_cases "${MATRIX_WARMTRACE}"

  "${PYTHON_BIN}" "${FASTSIM_ROOT}/tools/validate_fs_oracle_identity.py" \
    --result-root "${RESULT_ROOT}/sample/mesi-three-level-3GiB" \
    --output "${AUDIT_ROOT}/oracle-identity.json"

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

  "${PYTHON_BIN}" "${FASTSIM_ROOT}/tools/audit_fst_first_touch_recency.py" \
    --audit "${AUDIT_ROOT}/matrix-integrity.json" \
    --output "${AUDIT_ROOT}/first-touch-recency.json" \
    >"${AUDIT_ROOT}/first-touch-recency.stdout.json"

  "${PYTHON_BIN}" \
    "${FASTSIM_ROOT}/tools/audit_p1_native_response_sideband.py" \
    "${matrices[@]}" \
    --max-hierarchy-gap-ratio "${MAX_HIERARCHY_GAP_RATIO}" \
    --output "${AUDIT_ROOT}/native-summary-dual-scope.json" \
    >"${AUDIT_ROOT}/native-summary-dual-scope.stdout.json"
}

run_postprocess() {
  local matrices=(
    --matrix "${MATRIX_BASE}"
    --matrix "${MATRIX_STOCKFISH}"
    --matrix "${MATRIX_SPH}"
    --matrix "${MATRIX_WARMTRACE}"
  )
  "${PYTHON_BIN}" "${FASTSIM_ROOT}/tools/build_fst_v7_formal_dataset.py" \
    --audit "${AUDIT_ROOT}/matrix-integrity.json" \
    --out "${DATASET_ROOT}" \
    --jobs 16

  "${PYTHON_BIN}" "${FASTSIM_ROOT}/tools/run_kernel_event_accuracy_pipeline.py" \
    "${matrices[@]}" \
    --include-cores 4 \
    --split calibration \
    --page-fault-cache-state-model \
    --page-fault-syscall-semantic-model \
    --output-dir "${ACCURACY_ROOT}/calibration-c4"

  "${PYTHON_BIN}" "${FASTSIM_ROOT}/tools/run_kernel_event_accuracy_pipeline.py" \
    "${matrices[@]}" \
    --include-cores 8 \
    --split held-out \
    --page-fault-cache-state-model \
    --page-fault-syscall-semantic-model \
    --kernel-config "${ACCURACY_ROOT}/calibration-c4/kernel-events.cfg" \
    --output-dir "${ACCURACY_ROOT}/held-out-c8"

  "${PYTHON_BIN}" "${FASTSIM_ROOT}/tools/audit_fst_warmup_cachelines.py" \
    --audit "${AUDIT_ROOT}/matrix-integrity.json" \
    --accuracy-root "${ACCURACY_ROOT}/calibration-c4" \
    --include-cores 4 \
    --output "${AUDIT_ROOT}/warmup-cachelines-c4.json" \
    --markdown-output "${AUDIT_ROOT}/warmup-cachelines-c4.md"

  "${PYTHON_BIN}" "${FASTSIM_ROOT}/tools/audit_fst_warmup_cachelines.py" \
    --audit "${AUDIT_ROOT}/matrix-integrity.json" \
    --accuracy-root "${ACCURACY_ROOT}/held-out-c8" \
    --include-cores 8 \
    --output "${AUDIT_ROOT}/warmup-cachelines-c8.json" \
    --markdown-output "${AUDIT_ROOT}/warmup-cachelines-c8.md"
}

worker() {
  require_p0_external_contract
  unset PYTHONHOME
  export PYTHONUNBUFFERED=1
  echo "[taotrace-formal] started=$(date -Is) target=${TARGET_RECORDS}"
  echo "[taotrace-formal] run_root=${RUN_ROOT}"

  run_matrix "${MATRIX_BASE}" "${BASE_DISK}" 10 \
    710.omnetpp_r 777.zstd_r 782.lbm_r 811.tealeaf_s 854.graph500_s &
  local pid_base=$!
  run_matrix "${MATRIX_STOCKFISH}" "${STOCKFISH_DISK}" 2 \
    706.stockfish_r &
  local pid_stockfish=$!
  run_matrix "${MATRIX_SPH}" "${SPH_DISK}" 2 \
    803.sph_exa_s &
  local pid_sph=$!
  run_matrix "${MATRIX_WARMTRACE}" "${WARMTRACE_DISK}" 6 \
    816.nab_s 857.namd_s 881.neutron_s &
  local pid_warm=$!

  local failed=0
  wait "${pid_base}" || failed=1
  wait "${pid_stockfish}" || failed=1
  wait "${pid_sph}" || failed=1
  wait "${pid_warm}" || failed=1
  if [[ "${failed}" -ne 0 ]]; then
    echo "[taotrace-formal][ERROR] one or more matrices failed" >&2
    return 1
  fi

  run_audits
  if [[ "${POSTPROCESS_AFTER_COLLECTION}" == 1 ]]; then
    run_postprocess
  fi
  echo "[taotrace-formal] completed=$(date -Is)"
}

watchdog_main() {
  local attempt=0
  trap 'printf "stopped\n" >"${WATCHDOG_STATE_FILE}"; exit 143' TERM INT HUP
  while (( attempt < MAX_ATTEMPTS )); do
    attempt=$((attempt + 1))
    printf 'attempt=%s running\n' "${attempt}" >"${WATCHDOG_STATE_FILE}"
    echo "[taotrace-formal] watchdog attempt=${attempt}/${MAX_ATTEMPTS} started=$(date -Is)" \
      >>"${WATCHDOG_LOG}"
    local status
    if bash "${SCRIPT_PATH}" worker "${RUN_TAG}" >>"${LOG_FILE}" 2>&1; then
      printf 'complete attempt=%s\n' "${attempt}" >"${WATCHDOG_STATE_FILE}"
      printf '0\n' >"${EXIT_FILE}"
      echo "[taotrace-formal] watchdog completed=$(date -Is)" >>"${WATCHDOG_LOG}"
      return 0
    else
      status=$?
    fi
    printf '%s\n' "${status}" >"${WORKER_EXIT_FILE}"
    printf 'attempt=%s retry status=%s\n' "${attempt}" "${status}" \
      >"${WATCHDOG_STATE_FILE}"
    echo "[taotrace-formal] watchdog retry status=${status} at $(date -Is)" \
      >>"${WATCHDOG_LOG}"
    sleep "${RETRY_DELAY_SECONDS}"
  done
  printf 'exhausted attempts=%s\n' "${MAX_ATTEMPTS}" >"${WATCHDOG_STATE_FILE}"
  printf '1\n' >"${EXIT_FILE}"
  return 1
}

mkdir -p "${RUN_ROOT}"

case "${action}" in
  start)
    pid_file_is_running "${WATCHDOG_PID_FILE}" watchdog && \
      fail "watchdog already running: $(<"${WATCHDOG_PID_FILE}")"
    if [[ -e "${FROZEN_EVENT_DICTIONARY}" ]]; then
      cmp -s "${CANONICAL_EVENT_DICTIONARY}" "${FROZEN_EVENT_DICTIONARY}" || \
        fail "run tag is already bound to a different PMU event dictionary"
    else
      cp -a "${CANONICAL_EVENT_DICTIONARY}" "${FROZEN_EVENT_DICTIONARY}"
    fi
    : >"${LOG_FILE}"
    : >"${WATCHDOG_LOG}"
    nohup setsid bash "${SCRIPT_PATH}" watchdog "${RUN_TAG}" \
      >>"${WATCHDOG_LOG}" 2>&1 </dev/null &
    watchdog_pid=$!
    printf '%s\n' "${watchdog_pid}" >"${WATCHDOG_PID_FILE}"
    echo "[taotrace-formal] started watchdog pid=${watchdog_pid}"
    echo "[taotrace-formal] status: bash ${BASH_SOURCE[0]} status"
    echo "[taotrace-formal] log=${LOG_FILE}"
    ;;
  worker)
    printf '%s\n' "$$" >"${PID_FILE}"
    trap 'status=$?; printf "%s\n" "${status}" >"${WORKER_EXIT_FILE}"; trap - EXIT; exit "${status}"' EXIT
    worker
    ;;
  watchdog)
    printf '%s\n' "$$" >"${WATCHDOG_PID_FILE}"
    watchdog_main
    ;;
  audit)
    trap 'status=$?; printf "%s\n" "${status}" >"${EXIT_FILE}"; trap - EXIT; exit "${status}"' EXIT
    run_audits
    ;;
  postprocess)
    run_postprocess
    ;;
  status)
    show_status
    ;;
  stop)
    if pid_file_is_running "${WATCHDOG_PID_FILE}" watchdog; then
      watchdog_pid=$(<"${WATCHDOG_PID_FILE}")
      kill -TERM -- "-${watchdog_pid}"
      echo "[taotrace-formal] stop requested pgid=${watchdog_pid}"
    else
      echo "[taotrace-formal] not running"
    fi
    ;;
  *)
    fail "usage: $0 [start|status|stop|watchdog|worker|audit|postprocess] [run-tag]"
    ;;
esac
