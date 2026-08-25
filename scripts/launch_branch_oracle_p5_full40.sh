#!/usr/bin/env bash
set -euo pipefail

FASTSIM_ROOT=/data00/yinhaolang/FastSim
GROUP_LAUNCHER=${FASTSIM_ROOT}/scripts/launch_taotrace_fst_v7_c4_c8_formal.sh
VALIDATOR=${FASTSIM_ROOT}/tools/run_native_kernel_fastsim_validation.py
PYTHON_BIN=${PYTHON_BIN:-/data00/yinhaolang/infer/.venv/bin/python}
SCRIPT_PATH=${FASTSIM_ROOT}/scripts/launch_branch_oracle_p5_full40.sh

C4_C8_TAG=${C4_C8_TAG:-branch-oracle-p5-formal-c4-c8-10m-20260824}
C16_C32_TAG=${C16_C32_TAG:-branch-oracle-p5-formal-c16-c32-10m-20260824}
FORMAL_TAG=${FORMAL_TAG:-branch-oracle-p5-formal-full40-20260824}
C4_C8_ROOT=${FASTSIM_ROOT}/tmp/${C4_C8_TAG}
C16_C32_ROOT=${FASTSIM_ROOT}/tmp/${C16_C32_TAG}
FORMAL_ROOT=${FASTSIM_ROOT}/tmp/${FORMAL_TAG}
WATCH_PID_FILE=${FORMAL_ROOT}/watch.pid
WATCH_LOG=${FORMAL_ROOT}/watch.log
HEARTBEAT_FILE=${FORMAL_ROOT}/heartbeat
EXIT_FILE=${FORMAL_ROOT}/exit.code
COMPLETE_FILE=${FORMAL_ROOT}/complete.json
VALIDATION_ROOT=${FORMAL_ROOT}/fastsim-validation
POLL_SECONDS=${POLL_SECONDS:-60}

action=${1:-start}

fail() {
  printf '[branch-p5-full40][ERROR] %s\n' "$*" >&2
  exit 2
}

run_group() {
  local group=$1
  local group_action=$2
  case "${group}" in
    c4-c8)
      env \
        CORE_SET='4 8' EXPECTED_CASES=20 EXPECTED_FST_FILES=120 \
        BASE_JOBS=10 STOCKFISH_JOBS=2 SPH_JOBS=2 WARMTRACE_JOBS=6 \
        FUNCTIONAL_TRACE_MODE=native-kernel \
        bash "${GROUP_LAUNCHER}" "${group_action}" "${C4_C8_TAG}" \
          native-kernel
      ;;
    c16-c32)
      env \
        CORE_SET='16 32' EXPECTED_CASES=20 EXPECTED_FST_FILES=480 \
        BASE_JOBS=4 STOCKFISH_JOBS=1 SPH_JOBS=1 WARMTRACE_JOBS=2 \
        FUNCTIONAL_TRACE_MODE=native-kernel \
        bash "${GROUP_LAUNCHER}" "${group_action}" "${C16_C32_TAG}" \
          native-kernel
      ;;
    *) fail "unknown group: ${group}" ;;
  esac
}

watcher_running() {
  [[ -s "${WATCH_PID_FILE}" ]] || return 1
  local pid command_line
  pid=$(<"${WATCH_PID_FILE}")
  [[ "${pid}" =~ ^[0-9]+$ ]] || return 1
  kill -0 "${pid}" 2>/dev/null || return 1
  [[ -r "/proc/${pid}/cmdline" ]] || return 1
  command_line=$(tr '\0' ' ' <"/proc/${pid}/cmdline")
  [[ " ${command_line} " == *" ${SCRIPT_PATH} watch "* ]]
}

collection_result() {
  local path=$1
  if [[ ! -s "${path}" ]]; then
    printf 'running\n'
    return
  fi
  local value
  value=$(<"${path}")
  if [[ "${value}" == 0 ]]; then
    printf 'complete\n'
  else
    printf 'failed:%s\n' "${value}"
  fi
}

watch_main() {
  printf '%s\n' "$$" >"${WATCH_PID_FILE}"
  trap 'rc=$?; printf "%s\n" "${rc}" >"${EXIT_FILE}"; trap - EXIT; exit "${rc}"' EXIT
  while true; do
    local c4_state c16_state
    c4_state=$(collection_result "${C4_C8_ROOT}/exit.code")
    c16_state=$(collection_result "${C16_C32_ROOT}/exit.code")
    printf 'at=%s c4_c8=%s c16_c32=%s\n' \
      "$(date -Is)" "${c4_state}" "${c16_state}" >"${HEARTBEAT_FILE}"
    [[ "${c4_state}" == failed:* ]] && \
      fail "C4/C8 collection ${c4_state}"
    [[ "${c16_state}" == failed:* ]] && \
      fail "C16/C32 collection ${c16_state}"
    if [[ "${c4_state}" == complete && "${c16_state}" == complete ]]; then
      break
    fi
    sleep "${POLL_SECONDS}"
  done

  "${PYTHON_BIN}" "${VALIDATOR}" \
    --matrix "${C4_C8_ROOT}/matrix-base" \
    --matrix "${C4_C8_ROOT}/matrix-stockfish" \
    --matrix "${C4_C8_ROOT}/matrix-sph" \
    --matrix "${C4_C8_ROOT}/matrix-warmtrace" \
    --matrix "${C16_C32_ROOT}/matrix-base" \
    --matrix "${C16_C32_ROOT}/matrix-stockfish" \
    --matrix "${C16_C32_ROOT}/matrix-sph" \
    --matrix "${C16_C32_ROOT}/matrix-warmtrace" \
    --output-dir "${VALIDATION_ROOT}" \
    --fastsim "${FASTSIM_ROOT}/build/fastsim" \
    --config "${FASTSIM_ROOT}/configs/gem5-fs-native-kernel.cfg" \
    --event-dictionary "${FASTSIM_ROOT}/configs/pmu-event-dictionary-v1.json" \
    --repo-root "${FASTSIM_ROOT}" \
    --jobs 8 \
    --expected-cases 40

  jq '{
    schema: "fastsim-branch-oracle-p5-formal-full40-v1",
    completed_at: .updated_at_utc,
    expected_cases,
    validated_cases,
    passed_cases,
    failed_cases,
    pmu_accuracy_status,
    branch_misses: .pmu_accuracy.branch_misses
  }' "${VALIDATION_ROOT}/summary.json" >"${COMPLETE_FILE}.tmp"
  mv "${COMPLETE_FILE}.tmp" "${COMPLETE_FILE}"
}

show_status() {
  printf '[branch-p5-full40] C4/C8\n'
  run_group c4-c8 status
  printf '[branch-p5-full40] C16/C32\n'
  run_group c16-c32 status
  if watcher_running; then
    printf '[branch-p5-full40] watcher running pid=%s\n' \
      "$(<"${WATCH_PID_FILE}")"
  else
    printf '[branch-p5-full40] watcher not running\n'
  fi
  [[ -f "${HEARTBEAT_FILE}" ]] && sed 's/^/[full40] /' "${HEARTBEAT_FILE}"
  [[ -f "${COMPLETE_FILE}" ]] && jq . "${COMPLETE_FILE}"
  printf '[branch-p5-full40] root=%s\n' "${FORMAL_ROOT}"
}

mkdir -p "${FORMAL_ROOT}"

case "${action}" in
  start)
    watcher_running && fail "combined watcher is already running"
    [[ -e "${COMPLETE_FILE}" ]] && fail "formal 40-case result already exists"
    run_group c4-c8 start
    run_group c16-c32 start
    : >"${WATCH_LOG}"
    nohup setsid bash "${SCRIPT_PATH}" watch \
      >>"${WATCH_LOG}" 2>&1 </dev/null &
    watch_pid=$!
    printf '%s\n' "${watch_pid}" >"${WATCH_PID_FILE}"
    printf '[branch-p5-full40] started watcher pid=%s\n' "${watch_pid}"
    printf '[branch-p5-full40] status: bash %s status\n' "${SCRIPT_PATH}"
    ;;
  watch)
    watch_main
    ;;
  status)
    show_status
    ;;
  stop)
    run_group c4-c8 stop
    run_group c16-c32 stop
    if watcher_running; then
      kill -TERM -- "-$(<"${WATCH_PID_FILE}")"
      printf '[branch-p5-full40] stopped watcher pgid=%s\n' \
        "$(<"${WATCH_PID_FILE}")"
    fi
    ;;
  *) fail "usage: $0 [start|watch|status|stop]" ;;
esac
