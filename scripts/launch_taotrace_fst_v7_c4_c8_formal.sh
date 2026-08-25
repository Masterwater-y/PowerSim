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
FASTSIM_VALIDATION_TOOL=${FASTSIM_ROOT}/tools/run_native_kernel_fastsim_validation.py
FASTSIM_VALIDATION_ROOT=${RUN_ROOT}/fastsim-native-validation
FASTSIM_VALIDATION_LOG=${FASTSIM_VALIDATION_ROOT}/watch.log
FASTSIM_VALIDATION_PID_FILE=${FASTSIM_VALIDATION_ROOT}/watch.pid
FASTSIM_VALIDATION_EXIT_FILE=${FASTSIM_VALIDATION_ROOT}/exit.code
FASTSIM_VALIDATION_JOBS=${FASTSIM_VALIDATION_JOBS:-4}
LOG_FILE=${RUN_ROOT}/launch.log
PID_FILE=${RUN_ROOT}/launcher.pid
EXIT_FILE=${RUN_ROOT}/exit.code
WATCHDOG_PID_FILE=${RUN_ROOT}/watchdog.pid
WORKER_EXIT_FILE=${RUN_ROOT}/worker.exit.code
WATCHDOG_LOG=${RUN_ROOT}/watchdog.log
WATCHDOG_STATE_FILE=${RUN_ROOT}/watchdog.state
WATCHDOG_HEARTBEAT_FILE=${RUN_ROOT}/watchdog.heartbeat
TARGET_RECORDS=${TARGET_RECORDS:-10000000}
CORE_SET=${CORE_SET:-4 8}
FUNCTIONAL_TRACE_MODE=${3:-${FUNCTIONAL_TRACE_MODE:-user}}
export FUNCTIONAL_TRACE_MODE
read -r -a CORE_ARGS <<<"${CORE_SET}"
CALIBRATION_CORE=${CALIBRATION_CORE:-${CORE_ARGS[0]:-}}
HELDOUT_CORE=${HELDOUT_CORE:-${CORE_ARGS[1]:-}}
CALIBRATION_ACCURACY_ROOT=${ACCURACY_ROOT}/calibration-c${CALIBRATION_CORE}
HELDOUT_ACCURACY_ROOT=${ACCURACY_ROOT}/held-out-c${HELDOUT_CORE}
EXPECTED_CASES=${EXPECTED_CASES:-$((10 * ${#CORE_ARGS[@]}))}
EXPECTED_FST_FILES_DEFAULT=0
for core_count in "${CORE_ARGS[@]}"; do
  EXPECTED_FST_FILES_DEFAULT=$((EXPECTED_FST_FILES_DEFAULT + 10 * core_count))
done
EXPECTED_FST_FILES=${EXPECTED_FST_FILES:-${EXPECTED_FST_FILES_DEFAULT}}
SAMPLE_TIMEOUT_SECONDS=${SAMPLE_TIMEOUT_SECONDS:-28800}
ROI_SAFETY_MULTIPLIER=${ROI_SAFETY_MULTIPLIER:-100}
MAX_HIERARCHY_GAP_RATIO=${MAX_HIERARCHY_GAP_RATIO:-0.0002}
NATIVE_ANOMALY_LIMIT=${NATIVE_ANOMALY_LIMIT:-32}
MAX_ATTEMPTS=${MAX_ATTEMPTS:-20}
RETRY_DELAY_SECONDS=${RETRY_DELAY_SECONDS:-60}
WATCHDOG_HEARTBEAT_SECONDS=${WATCHDOG_HEARTBEAT_SECONDS:-30}
WATCHDOG_STALE_SECONDS=${WATCHDOG_STALE_SECONDS:-120}
POSTPROCESS_AFTER_COLLECTION=${POSTPROCESS_AFTER_COLLECTION:-0}
BASE_JOBS=${BASE_JOBS:-10}
STOCKFISH_JOBS=${STOCKFISH_JOBS:-2}
SPH_JOBS=${SPH_JOBS:-2}
WARMTRACE_JOBS=${WARMTRACE_JOBS:-6}

BASE_DISK=${TCSIM_ROOT}/data/spec2026_diskimg/spec2026.ext4
STOCKFISH_DISK=${TCSIM_ROOT}/data/spec2026_diskimg/spec2026-usergate-original.ext4
SPH_DISK=${TCSIM_ROOT}/data/spec2026_diskimg/spec2026-usergate-extended-v2.ext4
WARMTRACE_DISK=${TCSIM_ROOT}/data/spec2026_diskimg/spec2026-native-multicore-warmtrace.ext4

action=${1:-start}

fail() {
  echo "[taotrace-formal][ERROR] $*" >&2
  exit 2
}

case "${FUNCTIONAL_TRACE_MODE}" in
  user|native-kernel) ;;
  *) fail "FUNCTIONAL_TRACE_MODE must be user or native-kernel" ;;
esac

require_p0_external_contract() {
  grep -Fq 'taotrace-path-class-v3' \
    "${GEM5_ROOT}/src/cpu/o3/probe/tao_trace.cc" || \
    fail "gem5/TCSim P0 patch is not applied; see patches/README.md"
  grep -Fq 'taotrace-retired-bpred-v1' \
    "${GEM5_ROOT}/src/cpu/o3/probe/tao_trace.cc" || \
    fail "gem5 retired-BPred oracle patch is not applied; see patches/README.md"
  grep -Fq 'taotrace-retired-bpred-v1' \
    "${TCSIM_ROOT}/scripts/gem5_fs_roi.py" || \
    fail "TCSim retired-BPred source gate is not applied; see patches/README.md"
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
  if [[ "${FUNCTIONAL_TRACE_MODE}" == native-kernel ]]; then
    grep -Fq 'functional_include_kernel = Param.Bool(False' \
      "${GEM5_ROOT}/src/cpu/o3/probe/TaoTrace.py" || \
      fail "gem5 native-kernel FST producer patch is not applied"
    grep -Fq 'fst_static_unsupported_pcs_' \
      "${GEM5_ROOT}/src/cpu/o3/probe/tao_trace.hh" || \
      fail "gem5 native-kernel optional-imap repair is not applied"
    grep -Fq 'functional_include_kernel=args.functional_include_kernel' \
      "${TCSIM_ROOT}/scripts/gem5_fs_roi.py" || \
      fail "TCSim native-kernel FST plumbing patch is not applied"
    grep -Fq 'measurement_user_records' \
      "${TCSIM_ROOT}/scripts/summarize_gem5_fs_cpi.py" || \
      fail "TCSim mixed-trace user-target fix is not applied"
  fi
  grep -Fq 'emit_native_response_jsonl = Param.Bool(False' \
    "${GEM5_ROOT}/src/cpu/o3/probe/TaoTrace.py" || \
    fail "full native JSONL is not default-off; see patches/README.md"
  grep -Fq 'readMemAccPredicate())' \
    "${GEM5_ROOT}/src/cpu/o3/probe/tao_trace.cc" || \
    fail "O3 memory-predicate drain terminal fix is not applied; see patches/README.md"
  grep -Fq '!fallback_source && attr.native_response_count != 0' \
    "${GEM5_ROOT}/src/cpu/o3/probe/tao_trace.cc" || \
    fail "native fallback identity fix is not applied; see patches/README.md"
  grep -Fq 'nativeHierarchyReady(native)' \
    "${GEM5_ROOT}/src/cpu/o3/probe/tao_trace.cc" || \
    fail "native hierarchy-retention fix is not applied; see patches/README.md"
  grep -Fq 'aggregate->merge(*main)' \
    "${GEM5_ROOT}/src/cpu/o3/lsq.cc" || \
    fail "split-request closure fix is not applied; see patches/README.md"
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
  [[ -r "/proc/${pid}/cmdline" ]] || return 1
  command=$(tr '\0' ' ' <"/proc/${pid}/cmdline") || return 1
  [[ "${command}" == *"${SCRIPT_PATH} ${action_name}"* ]]
}

fastsim_validation_is_running() {
  [[ -s "${FASTSIM_VALIDATION_PID_FILE}" ]] || return 1
  local pid command
  pid=$(<"${FASTSIM_VALIDATION_PID_FILE}")
  [[ "${pid}" =~ ^[0-9]+$ ]] || return 1
  kill -0 "${pid}" 2>/dev/null || return 1
  [[ -r "/proc/${pid}/cmdline" ]] || return 1
  command=$(tr '\0' ' ' <"/proc/${pid}/cmdline") || return 1
  [[ "${command}" == *"${FASTSIM_VALIDATION_TOOL}"* ]] && \
    [[ "${command}" == *"--watch"* ]]
}

show_status() {
  local watchdog_running=0
  local watchdog_state=''
  if pid_file_is_running "${WATCHDOG_PID_FILE}" watchdog; then
    watchdog_running=1
    local watchdog_pid
    watchdog_pid=$(<"${WATCHDOG_PID_FILE}")
    echo "[taotrace-formal] watchdog running pid=${watchdog_pid}"
    ps -ww -p "${watchdog_pid}" -o pid,ppid,pgid,stat,pcpu,pmem,etime,args
  else
    echo "[taotrace-formal] watchdog not running"
  fi
  if pid_file_is_running "${PID_FILE}" worker; then
    local pid
    pid=$(<"${PID_FILE}")
    echo "[taotrace-formal] worker running pid=${pid}"
    ps -ww -p "${pid}" -o pid,ppid,pgid,stat,pcpu,pmem,etime,args
  else
    echo "[taotrace-formal] worker not running"
  fi
  if [[ -f "${WATCHDOG_STATE_FILE}" ]]; then
    watchdog_state=$(<"${WATCHDOG_STATE_FILE}")
    echo "[taotrace-formal] watchdog_state=${watchdog_state}"
  fi
  if [[ -f "${WATCHDOG_HEARTBEAT_FILE}" ]]; then
    local heartbeat_epoch now_epoch heartbeat_age
    heartbeat_epoch=$(stat -c %Y "${WATCHDOG_HEARTBEAT_FILE}")
    now_epoch=$(date +%s)
    heartbeat_age=$((now_epoch - heartbeat_epoch))
    echo "[taotrace-formal] watchdog_heartbeat=$(<"${WATCHDOG_HEARTBEAT_FILE}") age_seconds=${heartbeat_age}"
    if (( watchdog_running == 1 && heartbeat_age > WATCHDOG_STALE_SECONDS )); then
      echo "[taotrace-formal][WARN] watchdog state is stale; progress is not running"
    elif (( watchdog_running == 0 )) && \
      [[ "${watchdog_state}" == *running* || "${watchdog_state}" == *retry* ]]; then
      echo "[taotrace-formal][WARN] watchdog state is stale; progress is not running"
    fi
  elif (( watchdog_running == 1 )) || \
    [[ "${watchdog_state}" == *running* || "${watchdog_state}" == *retry* ]]; then
    echo "[taotrace-formal][WARN] watchdog heartbeat is missing; progress state is stale"
  fi
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
  if fastsim_validation_is_running; then
    local validation_pid
    validation_pid=$(<"${FASTSIM_VALIDATION_PID_FILE}")
    echo "[taotrace-formal] FastSim validation running pid=${validation_pid}"
    ps -ww -p "${validation_pid}" -o pid,ppid,pgid,stat,pcpu,pmem,etime,args
  else
    echo "[taotrace-formal] FastSim validation not running"
  fi
  if [[ -f "${FASTSIM_VALIDATION_ROOT}/summary.json" ]]; then
    jq -c '{expected_cases,discovered_completed_cases,validated_cases,
      passed_cases,failed_cases,terminal_failed_cases,pending_cases,
      mean_absolute_cpi_relative_error,max_absolute_cpi_relative_error}' \
      "${FASTSIM_VALIDATION_ROOT}/summary.json"
  fi
  echo "[taotrace-formal] log=${LOG_FILE}"
  echo "[taotrace-formal] watchdog_log=${WATCHDOG_LOG}"
  echo "[taotrace-formal] audit=${AUDIT_ROOT}/matrix-integrity.json"
  echo "[taotrace-formal] accuracy=${ACCURACY_ROOT}"
  echo "[taotrace-formal] fastsim_validation=${FASTSIM_VALIDATION_ROOT}"
}

run_matrix() {
  local matrix_root=$1
  local aux_disk=$2
  local jobs=$3
  shift 3
  local functional_trace_arg=--functional-user-only
  if [[ "${FUNCTIONAL_TRACE_MODE}" == native-kernel ]]; then
    functional_trace_arg=--functional-include-kernel
  fi
  "${PYTHON_BIN}" "${MATRIX_RUNNER}" \
    --stage sample \
    --workloads "$@" \
    --cores "${CORE_ARGS[@]}" \
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
    "${functional_trace_arg}" \
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
    if [[ "${FUNCTIONAL_TRACE_MODE}" == user ]]; then
      "${PYTHON_BIN}" "${STRICT_VALIDATOR}" \
        "${matrix}" "${workload}" "${cores}" "${TARGET_RECORDS}" \
        --expected-binary-sha256 "${binary_sha}" \
        --expected-aux-sha256 "${aux_sha}" \
        --result-root "${RESULT_ROOT}/sample"
    else
      jq -e \
        --argjson target "${TARGET_RECORDS}" \
        '(.trace_scope == "user-plus-kernel") and
         (.functional_warmup_enabled == true) and
         ([.functional_boundaries[] |
           (.trace_scope == "user-plus-kernel") and
           (.measurement_user_records >= $target)] | all)' \
        "${result}/tao_trace/trace.json" >/dev/null
      "${PYTHON_BIN}" "${FASTSIM_ROOT}/tools/audit_fst_privilege.py" \
        --trace-dir "${result}/tao_trace" \
        --require-user --require-kernel \
        --output "${AUDIT_ROOT}/privilege-${cores}c-${workload}.json"
    fi
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
  local integrity_args=(
    "${matrices[@]}"
    --output "${AUDIT_ROOT}/matrix-integrity.json"
    --expected-cases "${EXPECTED_CASES}"
    --expected-fst-files "${EXPECTED_FST_FILES}"
  )
  if [[ "${FUNCTIONAL_TRACE_MODE}" == user ]]; then
    integrity_args+=(--require-destination-classes)
  fi
  "${PYTHON_BIN}" "${FASTSIM_ROOT}/tools/audit_functional_warmup_matrix.py" \
    "${integrity_args[@]}"

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
  [[ "${FUNCTIONAL_TRACE_MODE}" == user ]] || \
    fail "postprocess is not defined for native-kernel mixed traces"
  [[ "${CALIBRATION_CORE}" =~ ^[0-9]+$ ]] || \
    fail "postprocess requires a numeric CALIBRATION_CORE"
  [[ "${HELDOUT_CORE}" =~ ^[0-9]+$ ]] || \
    fail "postprocess requires a numeric HELDOUT_CORE"
  [[ "${CALIBRATION_CORE}" != "${HELDOUT_CORE}" ]] || \
    fail "calibration and held-out core counts must differ"
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
    --include-cores "${CALIBRATION_CORE}" \
    --split calibration \
    --page-fault-cache-state-model \
    --page-fault-syscall-semantic-model \
    --output-dir "${CALIBRATION_ACCURACY_ROOT}"

  "${PYTHON_BIN}" "${FASTSIM_ROOT}/tools/run_kernel_event_accuracy_pipeline.py" \
    "${matrices[@]}" \
    --include-cores "${HELDOUT_CORE}" \
    --split held-out \
    --page-fault-cache-state-model \
    --page-fault-syscall-semantic-model \
    --kernel-config "${CALIBRATION_ACCURACY_ROOT}/kernel-events.cfg" \
    --output-dir "${HELDOUT_ACCURACY_ROOT}"

  "${PYTHON_BIN}" "${FASTSIM_ROOT}/tools/audit_fst_warmup_cachelines.py" \
    --audit "${AUDIT_ROOT}/matrix-integrity.json" \
    --accuracy-root "${CALIBRATION_ACCURACY_ROOT}" \
    --include-cores "${CALIBRATION_CORE}" \
    --output "${AUDIT_ROOT}/warmup-cachelines-c${CALIBRATION_CORE}.json" \
    --markdown-output "${AUDIT_ROOT}/warmup-cachelines-c${CALIBRATION_CORE}.md"

  "${PYTHON_BIN}" "${FASTSIM_ROOT}/tools/audit_fst_warmup_cachelines.py" \
    --audit "${AUDIT_ROOT}/matrix-integrity.json" \
    --accuracy-root "${HELDOUT_ACCURACY_ROOT}" \
    --include-cores "${HELDOUT_CORE}" \
    --output "${AUDIT_ROOT}/warmup-cachelines-c${HELDOUT_CORE}.json" \
    --markdown-output "${AUDIT_ROOT}/warmup-cachelines-c${HELDOUT_CORE}.md"
}

worker() {
  require_p0_external_contract
  unset PYTHONHOME
  export PYTHONUNBUFFERED=1
  echo "[taotrace-formal] started=$(date -Is) target=${TARGET_RECORDS} cores=${CORE_SET} mode=${FUNCTIONAL_TRACE_MODE}"
  echo "[taotrace-formal] run_root=${RUN_ROOT}"

  run_matrix "${MATRIX_BASE}" "${BASE_DISK}" "${BASE_JOBS}" \
    710.omnetpp_r 777.zstd_r 782.lbm_r 811.tealeaf_s 854.graph500_s &
  local pid_base=$!
  run_matrix "${MATRIX_STOCKFISH}" "${STOCKFISH_DISK}" \
    "${STOCKFISH_JOBS}" \
    706.stockfish_r &
  local pid_stockfish=$!
  run_matrix "${MATRIX_SPH}" "${SPH_DISK}" "${SPH_JOBS}" \
    803.sph_exa_s &
  local pid_sph=$!
  run_matrix "${MATRIX_WARMTRACE}" "${WARMTRACE_DISK}" \
    "${WARMTRACE_JOBS}" \
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
  trap 'printf "stopped at=%s\n" "$(date -Is)" >"${WATCHDOG_STATE_FILE}"; exit 143' TERM INT HUP
  while (( attempt < MAX_ATTEMPTS )); do
    attempt=$((attempt + 1))
    printf 'attempt=%s running started=%s\n' "${attempt}" "$(date -Is)" \
      >"${WATCHDOG_STATE_FILE}"
    echo "[taotrace-formal] watchdog attempt=${attempt}/${MAX_ATTEMPTS} started=$(date -Is)" \
      >>"${WATCHDOG_LOG}"
    local status worker_pid
    bash "${SCRIPT_PATH}" worker "${RUN_TAG}" >>"${LOG_FILE}" 2>&1 &
    worker_pid=$!
    while kill -0 "${worker_pid}" 2>/dev/null; do
      printf 'attempt=%s worker_pid=%s at=%s\n' \
        "${attempt}" "${worker_pid}" "$(date -Is)" \
        >"${WATCHDOG_HEARTBEAT_FILE}"
      sleep "${WATCHDOG_HEARTBEAT_SECONDS}" &
      wait $! || true
    done
    if wait "${worker_pid}"; then
      printf 'attempt=%s worker_pid=%s at=%s\n' \
        "${attempt}" "${worker_pid}" "$(date -Is)" \
        >"${WATCHDOG_HEARTBEAT_FILE}"
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
    echo "[taotrace-formal] mode=${FUNCTIONAL_TRACE_MODE}"
    echo "[taotrace-formal] status: bash ${BASH_SOURCE[0]} status ${RUN_TAG} ${FUNCTIONAL_TRACE_MODE}"
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
  infer-start)
    [[ "${FUNCTIONAL_TRACE_MODE}" == native-kernel ]] || \
      fail "FastSim native inference requires native-kernel mode"
    fastsim_validation_is_running && \
      fail "FastSim validation already running: $(<"${FASTSIM_VALIDATION_PID_FILE}")"
    [[ -x "${FASTSIM_ROOT}/build/fastsim" ]] || \
      fail "FastSim binary is missing; build it before inference"
    mkdir -p "${FASTSIM_VALIDATION_ROOT}"
    : >"${FASTSIM_VALIDATION_LOG}"
    nohup setsid "${PYTHON_BIN}" "${FASTSIM_VALIDATION_TOOL}" \
      --matrix "${MATRIX_BASE}" \
      --matrix "${MATRIX_STOCKFISH}" \
      --matrix "${MATRIX_SPH}" \
      --matrix "${MATRIX_WARMTRACE}" \
      --output-dir "${FASTSIM_VALIDATION_ROOT}" \
      --fastsim "${FASTSIM_ROOT}/build/fastsim" \
      --config "${FASTSIM_ROOT}/configs/gem5-fs-native-kernel.cfg" \
      --repo-root "${FASTSIM_ROOT}" \
      --jobs "${FASTSIM_VALIDATION_JOBS}" \
      --expected-cases "${EXPECTED_CASES}" \
      --collection-exit-file "${EXIT_FILE}" \
      --watch >>"${FASTSIM_VALIDATION_LOG}" 2>&1 </dev/null &
    validation_pid=$!
    printf '%s\n' "${validation_pid}" >"${FASTSIM_VALIDATION_PID_FILE}"
    echo "[taotrace-formal] started FastSim validation pid=${validation_pid}"
    echo "[taotrace-formal] validation_log=${FASTSIM_VALIDATION_LOG}"
    ;;
  infer-status)
    show_status
    ;;
  infer-stop)
    if fastsim_validation_is_running; then
      validation_pid=$(<"${FASTSIM_VALIDATION_PID_FILE}")
      kill -TERM -- "-${validation_pid}"
      echo "[taotrace-formal] FastSim validation stop requested pgid=${validation_pid}"
    else
      echo "[taotrace-formal] FastSim validation not running"
    fi
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
    fail "usage: $0 [start|status|stop|watchdog|worker|audit|postprocess|infer-start|infer-status|infer-stop] [run-tag] [user|native-kernel]"
    ;;
esac
