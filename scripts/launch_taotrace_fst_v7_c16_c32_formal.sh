#!/usr/bin/env bash
set -euo pipefail

# Collect a formal gem5 FS functional-trace corpus for a configurable core
# matrix (C16/C32 by default).  Every
# generated artifact, log, scratch file, and watchdog state file stays below
# FastSim/tmp.  The watchdog restarts the resumable matrix worker after a
# failure; run_gem5_fs_cpi_matrix.py validates and skips successful cases.

FASTSIM_ROOT=/data00/yinhaolang/FastSim
TCSIM_ROOT=/data00/yinhaolang/TCSim
GEM5_ROOT=/data00/yinhaolang/gem5-fs
PYTHON_BIN=${PYTHON_BIN:-/data00/yinhaolang/infer/.venv/bin/python}
export FASTSIM_EFFECTIVE_TARGET_GENERATOR=${FASTSIM_ROOT}/tools/generate_fs_effective_target.py
export FASTSIM_EFFECTIVE_TARGET_PYTHON=${PYTHON_BIN}
TAOGEN_SHARED_ROOT=${TAOGEN_SHARED:-/data00/yinhaolang/taogen/shared}
MATRIX_RUNNER=${TCSIM_ROOT}/scripts/run_gem5_fs_cpi_matrix.py
STRICT_VALIDATOR=${TCSIM_ROOT}/scripts/validate_gem5_usergate_result.py
TAO_CONFIG=${TCSIM_ROOT}/configs/gem5/x86_fs_kvm_boot_checkpoint_tao.py
SCRIPT_PATH=$(readlink -f "${BASH_SOURCE[0]}")

RUN_TAG=${RUN_TAG:-taotrace-fst-v7-c16-c32-formal-20260818}
RUN_ROOT=${FASTSIM_ROOT}/tmp/${RUN_TAG}
RESULT_ROOT=${RUN_ROOT}/source
TRACE_TMP_ROOT=${RUN_ROOT}/trace-scratch
DRIVER_TMP_ROOT=${RUN_ROOT}/driver-tmp
MATRIX_BASE=${RUN_ROOT}/matrix-base
MATRIX_STOCKFISH=${RUN_ROOT}/matrix-stockfish
MATRIX_SPH=${RUN_ROOT}/matrix-sph
MATRIX_WARMTRACE=${RUN_ROOT}/matrix-warmtrace
AUDIT_ROOT=${RUN_ROOT}/audit
DATASET_ROOT=${RUN_ROOT}/fst-v7
LOG_FILE=${RUN_ROOT}/launch.log
WATCHDOG_LOG=${RUN_ROOT}/watchdog.log
WATCHDOG_PID_FILE=${RUN_ROOT}/watchdog.pid
WORKER_PID_FILE=${RUN_ROOT}/worker.pid
WORKER_EXIT_FILE=${RUN_ROOT}/worker.exit.code
WORKER_PHASE_FILE=${RUN_ROOT}/worker.phase
WATCHDOG_STATE_FILE=${RUN_ROOT}/watchdog.state
WATCHDOG_HEARTBEAT_FILE=${RUN_ROOT}/watchdog.heartbeat
ATTEMPT_FILE=${RUN_ROOT}/watchdog.attempt
COMPLETE_FILE=${RUN_ROOT}/complete.json
LOCK_FILE=${RUN_ROOT}/watchdog.lock

TARGET_RECORDS=${TARGET_RECORDS:-10000000}
SAMPLE_TIMEOUT_SECONDS=${SAMPLE_TIMEOUT_SECONDS:-28800}
ROI_SAFETY_MULTIPLIER=${ROI_SAFETY_MULTIPLIER:-100}
MAX_ATTEMPTS=${MAX_ATTEMPTS:-12}
RETRY_BASE_SECONDS=${RETRY_BASE_SECONDS:-60}
RETRY_MAX_SECONDS=${RETRY_MAX_SECONDS:-900}
WATCHDOG_POLL_SECONDS=${WATCHDOG_POLL_SECONDS:-60}
MIN_FREE_GIB=${MIN_FREE_GIB:-512}
CORE_LIST=${CORE_LIST:-"16 32"}
read -r -a CORE_LIST_ARRAY <<<"${CORE_LIST}"
if (( ${#CORE_LIST_ARRAY[@]} == 0 )); then
  printf '[taotrace-formal] ERROR: CORE_LIST must not be empty\n' >&2
  exit 2
fi

# These are per-matrix limits.  The four matrices run concurrently, for a
# default maximum of 4 + 1 + 1 + 2 = 8 gem5 processes.  This is intentionally
# below the C4/C8 collection concurrency because each C32 trace has 32 writers.
BASE_JOBS=${BASE_JOBS:-4}
STOCKFISH_JOBS=${STOCKFISH_JOBS:-1}
SPH_JOBS=${SPH_JOBS:-1}
WARMTRACE_JOBS=${WARMTRACE_JOBS:-2}

EXPECTED_CASES=$((10 * ${#CORE_LIST_ARRAY[@]}))
EXPECTED_FST_FILES=0
for configured_cores in "${CORE_LIST_ARRAY[@]}"; do
  if [[ ! "${configured_cores}" =~ ^[1-9][0-9]*$ ]]; then
    printf '[taotrace-formal] ERROR: invalid core count: %s\n' \
      "${configured_cores}" >&2
    exit 2
  fi
  EXPECTED_FST_FILES=$((EXPECTED_FST_FILES + 10 * configured_cores))
done

BASE_DISK=${TCSIM_ROOT}/data/spec2026_diskimg/spec2026.ext4
STOCKFISH_DISK=${TCSIM_ROOT}/data/spec2026_diskimg/spec2026-usergate-original.ext4
SPH_DISK=${TCSIM_ROOT}/data/spec2026_diskimg/spec2026-usergate-extended-v2.ext4
WARMTRACE_DISK=${TCSIM_ROOT}/data/spec2026_diskimg/spec2026-native-multicore-warmtrace.ext4

action=${1:-start}

log_line() {
  printf '[taotrace-c16-c32] %s %s\n' "$(date -Is)" "$*"
}

fail() {
  log_line "ERROR: $*" >&2
  exit 2
}

require_file() {
  [[ -f "$1" ]] || fail "missing required file: $1"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"
}

validate_required_syscall_arg_count() {
  local syscall_number=$1
  local argument_count=$2
  "${PYTHON_BIN}" -c '
import ast
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
required_number = int(sys.argv[2])
required_count = int(sys.argv[3])
tree = ast.parse(path.read_text(), filename=str(path))
values = [
    ast.literal_eval(node.value)
    for node in tree.body
    if isinstance(node, ast.Assign)
    and any(
        isinstance(target, ast.Name)
        and target.id == "DEFAULT_SYSCALL_ARG_COUNTS"
        for target in node.targets
    )
]
if len(values) != 1 or not isinstance(values[0], str):
    raise SystemExit("cannot resolve DEFAULT_SYSCALL_ARG_COUNTS")
counts = {}
for item in values[0].split(","):
    number_text, count_text = item.split(":", 1)
    number = int(number_text)
    count = int(count_text)
    if number in counts:
        raise SystemExit(f"duplicate syscall argument count: {number}")
    if count < 0 or count > 6:
        raise SystemExit(f"invalid syscall argument count: {number}:{count}")
    counts[number] = count
actual = counts.get(required_number)
if actual != required_count:
    raise SystemExit(
        f"required syscall argument count missing: "
        f"{required_number}:{required_count}; actual={actual}"
    )
' "${TAO_CONFIG}" "${syscall_number}" "${argument_count}"
}

pid_matches_action() {
  local pid=$1
  local expected_action=$2
  [[ "${pid}" =~ ^[0-9]+$ ]] || return 1
  kill -0 "${pid}" 2>/dev/null || return 1
  [[ -r "/proc/${pid}/cmdline" ]] || return 1
  local command_line
  command_line=$(tr '\0' ' ' <"/proc/${pid}/cmdline")
  [[ " ${command_line} " == *" ${SCRIPT_PATH} ${expected_action} "* ]]
}

pid_file_matches_action() {
  local pid_file=$1
  local expected_action=$2
  [[ -s "${pid_file}" ]] || return 1
  local pid
  pid=$(<"${pid_file}")
  pid_matches_action "${pid}" "${expected_action}"
}

available_kib() {
  df -Pk "${RUN_ROOT}" | awk 'NR == 2 {print $4}'
}

check_free_space() {
  local free_kib minimum_kib
  free_kib=$(available_kib)
  minimum_kib=$((MIN_FREE_GIB * 1024 * 1024))
  [[ "${free_kib}" =~ ^[0-9]+$ ]] || fail "cannot read free disk space"
  if (( free_kib < minimum_kib )); then
    fail "only $((free_kib / 1024 / 1024)) GiB free; require ${MIN_FREE_GIB} GiB"
  fi
}

preflight() {
  require_command flock
  require_command jq
  require_command setsid
  require_file "${PYTHON_BIN}"
  require_file "${MATRIX_RUNNER}"
  require_file "${STRICT_VALIDATOR}"
  require_file "${TAO_CONFIG}"
  grep -Fq 'taotrace-path-class-v3' \
    "${GEM5_ROOT}/src/cpu/o3/probe/tao_trace.cc" || \
    fail "gem5/TCSim P0 patch is not applied; see patches/README.md"
  grep -Fq 'treeVictim' "${TAOGEN_SHARED_ROOT}/lru_banked.hh" || \
    fail "TaoTrace TreePLRU support is not applied; see patches/README.md"
  grep -Fq 'FASTSIM_EFFECTIVE_TARGET_GENERATOR must name' \
    "${TAO_CONFIG}" || \
    fail "final-config sidecar hook is not applied; see patches/README.md"
  validate_required_syscall_arg_count 201 1
  require_file "${GEM5_ROOT}/build/X86_MESI_Three_Level/gem5.opt"
  require_file "${BASE_DISK}"
  require_file "${STOCKFISH_DISK}"
  require_file "${SPH_DISK}"
  require_file "${WARMTRACE_DISK}"
  local cores
  for cores in "${CORE_LIST_ARRAY[@]}"; do
    require_file "${TCSIM_ROOT}/ckpt/gem5-fs-ubuntu2404/mesi-three-level-3GiB/cpt.${cores}c.booted/m5.cpt"
    require_file "${TCSIM_ROOT}/ckpt/gem5-fs-ubuntu2404/mesi-three-level-3GiB/cpt.${cores}c.booted/metadata.json"
  done
  check_free_space
}

count_complete_cases() {
  if [[ ! -d "${RESULT_ROOT}" ]]; then
    printf '0\n'
    return
  fi
  find "${RESULT_ROOT}" -type f -name request.json \
    -path '*/sample/mesi-three-level-3GiB/*c/*/*/*/request.json' \
    -printf '.\n' 2>/dev/null | wc -l
}

count_promoted_fst() {
  if [[ ! -d "${RESULT_ROOT}" ]]; then
    printf '0\n'
    return
  fi
  find "${RESULT_ROOT}" -type f -name 'core*.fst' -path '*/tao_trace/*' \
    -printf '.\n' 2>/dev/null | wc -l
}

count_promoted_asmap() {
  if [[ ! -d "${RESULT_ROOT}" ]]; then
    printf '0\n'
    return
  fi
  find "${RESULT_ROOT}" -type f -name 'core*.fst.asmap' \
    -path '*/tao_trace/*' -printf '.\n' 2>/dev/null | wc -l
}

count_scratch_fst() {
  if [[ ! -d "${TRACE_TMP_ROOT}" ]]; then
    printf '0\n'
    return
  fi
  # Direct gem5 output keeps the SimObject name until successful promotion;
  # the files are not named coreN.fst while a sample is in flight.
  find "${TRACE_TMP_ROOT}" -type f -name '*.fst' -printf '.\n' \
    2>/dev/null | wc -l
}

scratch_fst_bytes() {
  if [[ ! -d "${TRACE_TMP_ROOT}" ]]; then
    printf '0\n'
    return
  fi
  find "${TRACE_TMP_ROOT}" -type f -name '*.fst' -printf '%s\n' \
    2>/dev/null | awk '{total += $1} END {printf "%.0f\n", total}'
}

count_active_gem5() {
  ps -eo comm=,args= | awk -v root="${RUN_ROOT}" \
    '$1 == "gem5.opt" && index($0, root) {count++} END {print count + 0}'
}

write_state() {
  local phase=$1
  local attempt=$2
  local worker_rc=${3:-}
  local message=${4:-}
  local temporary=${WATCHDOG_STATE_FILE}.new
  {
    printf 'updated_at=%s\n' "$(date -Is)"
    printf 'phase=%s\n' "${phase}"
    printf 'attempt=%s\n' "${attempt}"
    printf 'worker_rc=%s\n' "${worker_rc}"
    printf 'message=%s\n' "${message}"
  } >"${temporary}"
  mv "${temporary}" "${WATCHDOG_STATE_FILE}"
}

write_worker_phase() {
  local phase=$1
  local temporary=${WORKER_PHASE_FILE}.new
  printf '%s\n' "${phase}" >"${temporary}"
  mv "${temporary}" "${WORKER_PHASE_FILE}"
}

write_heartbeat() {
  local phase=$1
  local attempt=$2
  local delay=${3:-0}
  local free_kib worker_pid_text=
  free_kib=$(available_kib)
  if [[ -s "${WORKER_PID_FILE}" ]]; then
    worker_pid_text=$(<"${WORKER_PID_FILE}")
  fi
  local temporary=${WATCHDOG_HEARTBEAT_FILE}.new
  {
    printf 'updated_at=%s\n' "$(date -Is)"
    printf 'phase=%s\n' "${phase}"
    printf 'attempt=%s\n' "${attempt}"
    printf 'watchdog_pid=%s\n' "$$"
    printf 'worker_pid=%s\n' "${worker_pid_text}"
    printf 'active_gem5=%s\n' "$(count_active_gem5)"
    printf 'complete_cases=%s/%s\n' "$(count_complete_cases)" "${EXPECTED_CASES}"
    printf 'promoted_fst=%s/%s\n' "$(count_promoted_fst)" "${EXPECTED_FST_FILES}"
    printf 'promoted_asmap=%s/%s\n' "$(count_promoted_asmap)" "${EXPECTED_FST_FILES}"
    printf 'scratch_fst=%s\n' "$(count_scratch_fst)"
    printf 'free_gib=%s\n' "$((free_kib / 1024 / 1024))"
    printf 'retry_delay_remaining=%s\n' "${delay}"
  } >"${temporary}"
  mv "${temporary}" "${WATCHDOG_HEARTBEAT_FILE}"
}

show_matrix_status() {
  local matrix=$1
  [[ -f "${matrix}/status.json" ]] || return 0
  jq -c --arg matrix "$(basename "${matrix}")" '
    {
      matrix:$matrix,
      summary,
      prepare_states:(
        [.tasks[] | .prepare.status? // empty]
        | group_by(.) | map({status:.[0], count:length})
      ),
      sample_states:(
        [.tasks[] | .sample.status? // empty]
        | group_by(.) | map({status:.[0], count:length})
      )
    }
  ' "${matrix}/status.json"
}

show_status() {
  local watchdog_state=stopped
  local worker_state=stopped
  if pid_file_matches_action "${WATCHDOG_PID_FILE}" watchdog; then
    watchdog_state=running
  fi
  if pid_file_matches_action "${WORKER_PID_FILE}" worker; then
    worker_state=running
  fi
  log_line "watchdog=${watchdog_state} worker=${worker_state}"
  if [[ "${watchdog_state}" == running ]]; then
    ps -p "$(<"${WATCHDOG_PID_FILE}")" -o pid,ppid,pgid,stat,pcpu,pmem,etime,args
  fi
  if [[ "${worker_state}" == running ]]; then
    ps -p "$(<"${WORKER_PID_FILE}")" -o pid,ppid,pgid,stat,pcpu,pmem,etime,args
  fi
  [[ -f "${WATCHDOG_STATE_FILE}" ]] && sed 's/^/[state] /' "${WATCHDOG_STATE_FILE}"
  [[ -f "${WATCHDOG_HEARTBEAT_FILE}" ]] && \
    sed 's/^/[heartbeat] /' "${WATCHDOG_HEARTBEAT_FILE}"
  show_matrix_status "${MATRIX_BASE}"
  show_matrix_status "${MATRIX_STOCKFISH}"
  show_matrix_status "${MATRIX_SPH}"
  show_matrix_status "${MATRIX_WARMTRACE}"
  log_line "live_progress active_gem5=$(count_active_gem5) complete_cases=$(count_complete_cases)/${EXPECTED_CASES} promoted_fst=$(count_promoted_fst)/${EXPECTED_FST_FILES} promoted_asmap=$(count_promoted_asmap)/${EXPECTED_FST_FILES} scratch_fst=$(count_scratch_fst) scratch_bytes=$(scratch_fst_bytes)"
  if [[ "${worker_state}" == running ]]; then
    log_line "worker_result=running"
  elif [[ -f "${WORKER_EXIT_FILE}" ]]; then
    local worker_result
    worker_result=$(<"${WORKER_EXIT_FILE}")
    if [[ "${worker_result}" =~ ^[0-9]+$ ]]; then
      log_line "last_worker_exit=${worker_result}"
    else
      log_line "worker_result=${worker_result}"
    fi
  fi
  if [[ -f "${COMPLETE_FILE}" ]]; then
    log_line "collection=complete marker=${COMPLETE_FILE}"
  fi
  log_line "run_root=${RUN_ROOT}"
  log_line "log=${LOG_FILE}"
  log_line "watchdog_log=${WATCHDOG_LOG}"
}

run_matrix() {
  local matrix_root=$1
  local aux_disk=$2
  local jobs=$3
  shift 3
  "${PYTHON_BIN}" "${MATRIX_RUNNER}" \
    --stage sample \
    --workloads "$@" \
    --cores "${CORE_LIST_ARRAY[@]}" \
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
  local actual_asmap
  actual_asmap=$(count_promoted_asmap)
  if (( actual_asmap != EXPECTED_FST_FILES )); then
    fail "address-space map count ${actual_asmap}; expected ${EXPECTED_FST_FILES}"
  fi
  local matrices=(
    --matrix "${MATRIX_BASE}"
    --matrix "${MATRIX_STOCKFISH}"
    --matrix "${MATRIX_SPH}"
    --matrix "${MATRIX_WARMTRACE}"
  )
  "${PYTHON_BIN}" "${FASTSIM_ROOT}/tools/audit_functional_warmup_matrix.py" \
    "${matrices[@]}" \
    --output "${AUDIT_ROOT}/matrix-integrity.json" \
    --expected-cases "${EXPECTED_CASES}" \
    --expected-fst-files "${EXPECTED_FST_FILES}" \
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
}

run_postprocess() {
  "${PYTHON_BIN}" "${FASTSIM_ROOT}/tools/build_fst_v7_formal_dataset.py" \
    --audit "${AUDIT_ROOT}/matrix-integrity.json" \
    --out "${DATASET_ROOT}" \
    --jobs 24
}

worker_main() {
  unset PYTHONHOME
  export PYTHONUNBUFFERED=1
  preflight
  log_line "worker started target=${TARGET_RECORDS} cores=${CORE_LIST}"
  log_line "run_root=${RUN_ROOT}"
  write_worker_phase collecting

  local matrix_pids=()
  run_matrix "${MATRIX_BASE}" "${BASE_DISK}" "${BASE_JOBS}" \
    710.omnetpp_r 777.zstd_r 782.lbm_r 811.tealeaf_s 854.graph500_s &
  matrix_pids+=("$!")
  run_matrix "${MATRIX_STOCKFISH}" "${STOCKFISH_DISK}" \
    "${STOCKFISH_JOBS}" 706.stockfish_r &
  matrix_pids+=("$!")
  run_matrix "${MATRIX_SPH}" "${SPH_DISK}" "${SPH_JOBS}" \
    803.sph_exa_s &
  matrix_pids+=("$!")
  run_matrix "${MATRIX_WARMTRACE}" "${WARMTRACE_DISK}" \
    "${WARMTRACE_JOBS}" 816.nab_s 857.namd_s 881.neutron_s &
  matrix_pids+=("$!")

  local failed=0
  local matrix_pid
  for matrix_pid in "${matrix_pids[@]}"; do
    if ! wait "${matrix_pid}"; then
      failed=1
    fi
  done
  if (( failed != 0 )); then
    fail "one or more matrices failed; watchdog will retry incomplete cases"
  fi

  log_line "all matrices complete; running integrity audits"
  write_worker_phase auditing
  run_audits
  log_line "audits complete; building canonical FST v7 dataset"
  write_worker_phase postprocessing
  run_postprocess
  write_worker_phase complete
  log_line "worker completed"
}

worker_entry() {
  printf '%s\n' "$$" >"${WORKER_PID_FILE}"
  trap 'rc=$?; printf "%s\n" "${rc}" >"${WORKER_EXIT_FILE}"; trap - EXIT; exit "${rc}"' EXIT
  worker_main
}

sleep_with_heartbeat() {
  local attempt=$1
  local remaining=$2
  while (( remaining > 0 )); do
    write_heartbeat retry-wait "${attempt}" "${remaining}"
    local step=${WATCHDOG_POLL_SECONDS}
    (( step > remaining )) && step=${remaining}
    sleep "${step}"
    remaining=$((remaining - step))
  done
}

watchdog_main() {
  exec 9>"${LOCK_FILE}"
  flock -n 9 || fail "another watchdog owns ${LOCK_FILE}"
  printf '%s\n' "$$" >"${WATCHDOG_PID_FILE}"
  preflight

  local worker_pid=
  local attempt=0
  local run_attempt=0
  local delay=${RETRY_BASE_SECONDS}
  if [[ -s "${ATTEMPT_FILE}" ]]; then
    attempt=$(<"${ATTEMPT_FILE}")
    [[ "${attempt}" =~ ^[0-9]+$ ]] || attempt=0
  fi

  stop_worker() {
    if [[ -n "${worker_pid}" ]] && pid_matches_action "${worker_pid}" worker; then
      log_line "stopping worker process group ${worker_pid}"
      kill -TERM -- "-${worker_pid}" 2>/dev/null || true
    fi
  }
  watchdog_signal() {
    write_state stopping "${attempt}" 143 signal
    stop_worker
    exit 143
  }
  trap watchdog_signal TERM INT HUP

  while (( MAX_ATTEMPTS == 0 || run_attempt < MAX_ATTEMPTS )); do
    check_free_space
    attempt=$((attempt + 1))
    run_attempt=$((run_attempt + 1))
    printf '%s\n' "${attempt}" >"${ATTEMPT_FILE}"
    write_state starting "${attempt}" '' worker-launch
    log_line "watchdog attempt=${attempt} local_attempt=${run_attempt}/${MAX_ATTEMPTS}" \
      >>"${LOG_FILE}"

    printf 'running\n' >"${WORKER_EXIT_FILE}"
    setsid bash "${SCRIPT_PATH}" worker >>"${LOG_FILE}" 2>&1 </dev/null &
    worker_pid=$!
    printf '%s\n' "${worker_pid}" >"${WORKER_PID_FILE}"
    write_state running "${attempt}" '' worker-active

    while kill -0 "${worker_pid}" 2>/dev/null; do
      write_heartbeat running "${attempt}"
      sleep "${WATCHDOG_POLL_SECONDS}"
    done

    local worker_rc
    if wait "${worker_pid}"; then
      worker_rc=0
    else
      worker_rc=$?
    fi
    printf '%s\n' "${worker_rc}" >"${WORKER_EXIT_FILE}"
    worker_pid=

    if (( worker_rc == 0 )); then
      write_state complete "${attempt}" 0 success
      write_heartbeat complete "${attempt}"
      printf '{\n  "status": "complete",\n  "completed_at": "%s",\n  "attempt": %s,\n  "cases": %s,\n  "fst_files": %s,\n  "address_space_map_files": %s\n}\n' \
        "$(date -Is)" "${attempt}" "${EXPECTED_CASES}" \
        "${EXPECTED_FST_FILES}" "${EXPECTED_FST_FILES}" \
        >"${COMPLETE_FILE}"
      log_line "watchdog observed successful completion" >>"${LOG_FILE}"
      return 0
    fi

    write_state failed "${attempt}" "${worker_rc}" retry-pending
    local failed_phase=unknown
    if [[ -s "${WORKER_PHASE_FILE}" ]]; then
      failed_phase=$(<"${WORKER_PHASE_FILE}")
    fi
    if [[ "${failed_phase}" == auditing || \
          "${failed_phase}" == postprocessing ]]; then
      write_state blocked "${attempt}" "${worker_rc}" \
        "worker-${failed_phase}-failed"
      write_heartbeat blocked "${attempt}"
      log_line \
        "worker exit=${worker_rc} phase=${failed_phase}; deterministic post-collection failure will not be retried" \
        >>"${LOG_FILE}"
      return "${worker_rc}"
    fi
    log_line "worker exit=${worker_rc} phase=${failed_phase}; retry in ${delay}s" \
      >>"${LOG_FILE}"
    if (( MAX_ATTEMPTS != 0 && run_attempt >= MAX_ATTEMPTS )); then
      break
    fi
    sleep_with_heartbeat "${attempt}" "${delay}"
    delay=$((delay * 2))
    (( delay > RETRY_MAX_SECONDS )) && delay=${RETRY_MAX_SECONDS}
  done

  write_state exhausted "${attempt}" "$(<"${WORKER_EXIT_FILE}")" max-attempts
  log_line "watchdog exhausted ${MAX_ATTEMPTS} attempts" >>"${LOG_FILE}"
  return 1
}

mkdir -p "${RUN_ROOT}"

case "${action}" in
  start)
    if pid_file_matches_action "${WATCHDOG_PID_FILE}" watchdog; then
      fail "watchdog already running: $(<"${WATCHDOG_PID_FILE}")"
    fi
    if [[ -f "${COMPLETE_FILE}" && "${FORCE_RESUME:-0}" != 1 ]]; then
      fail "collection already complete; set FORCE_RESUME=1 to re-audit"
    fi
    preflight
    touch "${LOG_FILE}" "${WATCHDOG_LOG}"
    nohup setsid bash "${SCRIPT_PATH}" watchdog \
      >>"${WATCHDOG_LOG}" 2>&1 </dev/null &
    watchdog_pid=$!
    printf '%s\n' "${watchdog_pid}" >"${WATCHDOG_PID_FILE}"
    log_line "started watchdog pid=${watchdog_pid}"
    log_line "status: bash ${SCRIPT_PATH} status"
    log_line "log=${LOG_FILE}"
    ;;
  watchdog)
    watchdog_main
    ;;
  worker)
    worker_entry
    ;;
  audit)
    preflight
    run_audits
    ;;
  postprocess)
    preflight
    run_postprocess
    ;;
  status)
    show_status
    ;;
  stop)
    if pid_file_matches_action "${WATCHDOG_PID_FILE}" watchdog; then
      watchdog_pid=$(<"${WATCHDOG_PID_FILE}")
      kill -TERM -- "-${watchdog_pid}"
      log_line "stop requested for watchdog process group ${watchdog_pid}"
    else
      log_line "watchdog not running"
    fi
    if pid_file_matches_action "${WORKER_PID_FILE}" worker; then
      worker_pid=$(<"${WORKER_PID_FILE}")
      kill -TERM -- "-${worker_pid}" 2>/dev/null || true
      log_line "stop requested for worker process group ${worker_pid}"
    fi
    ;;
  *)
    fail "usage: $0 [start|status|stop|watchdog|worker|audit|postprocess]"
    ;;
esac
