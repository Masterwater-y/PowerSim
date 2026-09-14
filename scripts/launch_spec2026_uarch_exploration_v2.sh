#!/usr/bin/env bash
set -euo pipefail

ROOT=/data00/yinhaolang/FastSim
SCRIPT=${ROOT}/scripts/launch_spec2026_uarch_exploration_v2.sh
PYTHON=${PYTHON:-/data00/yinhaolang/infer/.venv/bin/python}
MATRIX=${ROOT}/configs/spec2026-uarch-exploration-v2.json
COLLECTOR=${ROOT}/tools/collect_spec2026_uarch_fs.py
MATERIALIZER=${ROOT}/tools/materialize_spec2026_uarch_dataset.py
RUN_ROOT=${RUN_ROOT:-${ROOT}/tmp/spec2026-uarch-exploration-v2-native-v28_6-20260907}
SMOKE_ROOT=${RUN_ROOT}-smoke
CHECKPOINT_ROOT=${ROOT}/tmp/spec2026-uarch-exploration-checkpoints-v2
FASTSIM_CONFIG=${FASTSIM_CONFIG:-${ROOT}/configs/gem5-fs-native-kernel.cfg}
FASTSIM_ROOT=${RUN_ROOT}/fastsim-v28_6-maintained
EVALUATION_ROOT=${RUN_ROOT}/evaluation-v28_6-maintained
SUMMARY_ROOT=${RUN_ROOT}/result-summary
DISK_BUDGET_ROOT=${RUN_ROOT}/disk-budget
DISK_REFERENCE_ROOT=${ROOT}/tmp/spec2026-uarch-exploration-v1-native-v28_2
DISK_HARD_RESERVE_BYTES=${DISK_HARD_RESERVE_BYTES:-1099511627776}
DISK_STOP_BYTES=${DISK_STOP_BYTES:-1319413953331}
DISK_WARN_BYTES=${DISK_WARN_BYTES:-1649267441664}
DISK_GUARD_INTERVAL_SECONDS=${DISK_GUARD_INTERVAL_SECONDS:-60}
COLLECT_JOBS=${COLLECT_JOBS:-20}
FASTSIM_JOBS=${FASTSIM_JOBS:-96}
SMOKE_TIMEOUT_SECONDS=${SMOKE_TIMEOUT_SECONDS:-3600}
FORMAL_TIMEOUT_SECONDS=${FORMAL_TIMEOUT_SECONDS:-28800}
SMOKE_TASK_TIMEOUT_SECONDS=${SMOKE_TASK_TIMEOUT_SECONDS:-4500}
FORMAL_TASK_TIMEOUT_SECONDS=${FORMAL_TASK_TIMEOUT_SECONDS:-29700}
MAX_PHASE_ATTEMPTS=${MAX_PHASE_ATTEMPTS:-6}
RETRY_DELAY_SECONDS=${RETRY_DELAY_SECONDS:-120}
LOG=${RUN_ROOT}/pipeline.log
PID_FILE=${RUN_ROOT}/pipeline.pid
STATE_FILE=${RUN_ROOT}/pipeline.state
EXIT_FILE=${RUN_ROOT}/pipeline.exit.code
DISK_GUARD_PID_FILE=${RUN_ROOT}/disk-guard.pid
DISK_GUARD_STATE=${RUN_ROOT}/disk-guard.state
DISK_GUARD_LOG=${RUN_ROOT}/disk-guard.log
ACTION=${1:-status}

mkdir -p "${RUN_ROOT}" "${CHECKPOINT_ROOT}" "${RUN_ROOT}/process-tmp"
export TMPDIR=${RUN_ROOT}/process-tmp

write_state() {
  local phase=$1
  local state=$2
  local attempt=${3:-0}
  local detail=${4:-}
  printf 'phase=%s\nstate=%s\nattempt=%s\npid=%s\nupdated_at=%s\ndetail=%s\nlog=%s\n' \
    "${phase}" "${state}" "${attempt}" "$$" "$(date -Is)" "${detail}" "${LOG}" \
    >"${STATE_FILE}.tmp"
  mv "${STATE_FILE}.tmp" "${STATE_FILE}"
}

pid_is_running() {
  [[ -s "${PID_FILE}" ]] || return 1
  local pid command
  pid=$(<"${PID_FILE}")
  [[ "${pid}" =~ ^[0-9]+$ ]] || return 1
  kill -0 "${pid}" 2>/dev/null || return 1
  [[ -r "/proc/${pid}/cmdline" ]] || return 1
  command=$(tr '\0' ' ' <"/proc/${pid}/cmdline")
  [[ "${command}" == *"launch_spec2026_uarch_exploration_v2.sh worker"* ]]
}

disk_guard_pid_is_running() {
  [[ -s "${DISK_GUARD_PID_FILE}" ]] || return 1
  local pid command
  pid=$(<"${DISK_GUARD_PID_FILE}")
  [[ "${pid}" =~ ^[0-9]+$ ]] || return 1
  kill -0 "${pid}" 2>/dev/null || return 1
  [[ -r "/proc/${pid}/cmdline" ]] || return 1
  command=$(tr '\0' ' ' <"/proc/${pid}/cmdline")
  [[ "${command}" == *"launch_spec2026_uarch_exploration_v2.sh disk-guard"* ]]
}

available_bytes() {
  df -B1 --output=avail "${ROOT}" | tail -1 | tr -d ' '
}

write_disk_guard_state() {
  local state=$1
  local available=$2
  printf 'state=%s\navailable_bytes=%s\nwarn_bytes=%s\nstop_bytes=%s\nhard_reserve_bytes=%s\nupdated_at=%s\n' \
    "${state}" "${available}" "${DISK_WARN_BYTES}" "${DISK_STOP_BYTES}" \
    "${DISK_HARD_RESERVE_BYTES}" "$(date -Is)" >"${DISK_GUARD_STATE}.tmp"
  mv "${DISK_GUARD_STATE}.tmp" "${DISK_GUARD_STATE}"
}

disk_guard() {
  echo $$ >"${DISK_GUARD_PID_FILE}"
  echo "[disk-guard] start=$(date -Is) pipeline_pid=$(<"${PID_FILE}")" \
    >>"${DISK_GUARD_LOG}"
  while pid_is_running; do
    local available state=ok
    available=$(available_bytes)
    if (( available <= DISK_STOP_BYTES )); then
      state=stopped
      write_disk_guard_state "${state}" "${available}"
      echo "[disk-guard][STOP] available=${available} stop=${DISK_STOP_BYTES} "
      echo "[disk-guard][STOP] preserving >=1TiB reserve; terminating pipeline group" \
        >>"${DISK_GUARD_LOG}"
      local pipeline_pid
      pipeline_pid=$(<"${PID_FILE}")
      kill -TERM -- "-${pipeline_pid}" 2>/dev/null || kill -TERM "${pipeline_pid}" 2>/dev/null || true
      exit 2
    elif (( available <= DISK_WARN_BYTES )); then
      state=warning
      echo "[disk-guard][WARN] available=${available} warn=${DISK_WARN_BYTES}" \
        >>"${DISK_GUARD_LOG}"
    fi
    write_disk_guard_state "${state}" "${available}"
    sleep "${DISK_GUARD_INTERVAL_SECONDS}"
  done
  write_disk_guard_state pipeline-not-running "$(available_bytes)"
}

start_disk_guard() {
  if disk_guard_pid_is_running; then
    echo "[uarch-v2] disk guard already running pid=$(<"${DISK_GUARD_PID_FILE}")"
    return 0
  fi
  /usr/bin/nohup /usr/bin/setsid /usr/bin/env bash "${SCRIPT}" disk-guard \
    >>"${DISK_GUARD_LOG}" 2>&1 </dev/null &
  local pid=$!
  echo "${pid}" >"${DISK_GUARD_PID_FILE}"
  sleep 1
  kill -0 "${pid}" 2>/dev/null
  echo "[uarch-v2] disk guard started pid=${pid}"
}

show_collection_status() {
  local root=$1
  if [[ -f "${root}/status.json" ]]; then
    jq -c '
      {target_records_per_core, updated_at_utc, summary,
       observed_tasks:(.tasks | length),
       task_states:(.tasks | to_entries | group_by(.value.status)
         | map({status:.[0].value.status,count:length}))}
    ' "${root}/status.json"
  else
    echo "not-started"
  fi
}

status() {
  if pid_is_running; then
    echo "[uarch-v2] running pid=$(<"${PID_FILE}")"
  else
    echo "[uarch-v2] worker not running"
  fi
  if disk_guard_pid_is_running; then
    echo "[uarch-v2] disk guard running pid=$(<"${DISK_GUARD_PID_FILE}")"
  else
    echo "[uarch-v2] disk guard not running"
  fi
  [[ -f "${DISK_GUARD_STATE}" ]] && sed -n '1,12p' "${DISK_GUARD_STATE}"
  [[ -f "${DISK_BUDGET_ROOT}/disk-budget.json" ]] && \
    jq -c '{planned_cases,estimated_additional_bytes,current_free_bytes,projected_free_bytes,hard_reserve_bytes,safe_to_start}' \
      "${DISK_BUDGET_ROOT}/disk-budget.json"
  [[ -f "${STATE_FILE}" ]] && sed -n '1,20p' "${STATE_FILE}"
  echo "[uarch-v2] smoke"
  show_collection_status "${SMOKE_ROOT}"
  echo "[uarch-v2] formal"
  show_collection_status "${RUN_ROOT}"
  if [[ -f "${SUMMARY_ROOT}/summary.json" ]]; then
    jq -c '{cases,profile_counts,ranking_slices,headline}' "${SUMMARY_ROOT}/summary.json"
  fi
  [[ -f "${EXIT_FILE}" ]] && echo "[uarch-v2] exit=$(<"${EXIT_FILE}")"
  echo "[uarch-v2] log=${LOG}"
}

run_retry() {
  local phase=$1
  shift
  local attempt=1 rc=0
  while (( attempt <= MAX_PHASE_ATTEMPTS )); do
    write_state "${phase}" running "${attempt}" ""
    echo "[uarch-v2] phase=${phase} attempt=${attempt}/${MAX_PHASE_ATTEMPTS} start=$(date -Is)"
    set +e
    "$@"
    rc=$?
    set -e
    if (( rc == 0 )); then
      write_state "${phase}" complete "${attempt}" ""
      echo "[uarch-v2] phase=${phase} complete=$(date -Is)"
      return 0
    fi
    write_state "${phase}" retry "${attempt}" "exit=${rc}"
    echo "[uarch-v2] phase=${phase} exit=${rc}; retrying completed-case-aware collection"
    attempt=$((attempt + 1))
    if (( attempt <= MAX_PHASE_ATTEMPTS )); then
      sleep "${RETRY_DELAY_SECONDS}"
    fi
  done
  write_state "${phase}" failed "${MAX_PHASE_ATTEMPTS}" "exit=${rc}"
  return "${rc}"
}

worker() {
  echo $$ >"${PID_FILE}"
  rm -f "${EXIT_FILE}"
  trap 'rc=$?; echo "${rc}" >"${EXIT_FILE}"; if (( rc != 0 )); then write_state "${phase:-unknown}" failed "${attempt:-0}" "worker_exit=${rc}"; fi' EXIT
  cd "${ROOT}"

  local phase=preflight attempt=1
  write_state "${phase}" running "${attempt}" ""
  jq empty "${MATRIX}"
  "${PYTHON}" -m py_compile \
    "${COLLECTOR}" "${MATERIALIZER}" \
    "${ROOT}/tools/summarize_uarch_exploration.py" \
    "${ROOT}/tools/estimate_uarch_disk_budget.py"
  "${PYTHON}" "${ROOT}/tools/estimate_uarch_disk_budget.py" \
    --matrix "${MATRIX}" --reference-root "${DISK_REFERENCE_ROOT}" \
    --reference-cases 54 --filesystem-path "${ROOT}" \
    --hard-reserve-bytes "${DISK_HARD_RESERVE_BYTES}" --out "${DISK_BUDGET_ROOT}"
  "${PYTHON}" "${COLLECTOR}" \
    --matrix "${MATRIX}" --run-root "${SMOKE_ROOT}" \
    --checkpoint-root "${CHECKPOINT_ROOT}" --target-records 10000 \
    --jobs "${COLLECT_JOBS}" --sample-timeout-seconds "${SMOKE_TIMEOUT_SECONDS}" \
    --task-timeout-seconds "${SMOKE_TASK_TIMEOUT_SECONDS}" \
    --dry-run >"${RUN_ROOT}/planned-cases.json"
  write_state "${phase}" complete "${attempt}" "planned_cases=204"

  phase=build
  run_retry "${phase}" cmake --build "${ROOT}/build" -- -j16
  run_retry "${phase}-tests" "${ROOT}/build/fastsim_tests"

  phase=smoke
  run_retry "${phase}" \
    "${PYTHON}" "${COLLECTOR}" \
      --matrix "${MATRIX}" --run-root "${SMOKE_ROOT}" \
      --checkpoint-root "${CHECKPOINT_ROOT}" --target-records 10000 \
      --jobs "${COLLECT_JOBS}" --sample-timeout-seconds "${SMOKE_TIMEOUT_SECONDS}" \
      --task-timeout-seconds "${SMOKE_TASK_TIMEOUT_SECONDS}"

  phase=formal
  run_retry "${phase}" \
    "${PYTHON}" "${COLLECTOR}" \
      --matrix "${MATRIX}" --run-root "${RUN_ROOT}" \
      --checkpoint-root "${CHECKPOINT_ROOT}" --target-records 10000000 \
      --jobs "${COLLECT_JOBS}" --sample-timeout-seconds "${FORMAL_TIMEOUT_SECONDS}" \
      --task-timeout-seconds "${FORMAL_TASK_TIMEOUT_SECONDS}"

  phase=materialize
  run_retry "${phase}" \
    "${PYTHON}" "${MATERIALIZER}" --matrix "${MATRIX}" --run-root "${RUN_ROOT}"

  phase=replay
  run_retry "${phase}" \
    "${PYTHON}" "${ROOT}/tools/run_uarch_fastsim.py" \
      --root "${RUN_ROOT}" --matrix "${MATRIX}" \
      --config "${FASTSIM_CONFIG}" --out "${FASTSIM_ROOT}" \
      --fastsim "${ROOT}/build/fastsim" --jobs "${FASTSIM_JOBS}"

  phase=evaluate
  run_retry "${phase}" \
    "${PYTHON}" "${ROOT}/tools/evaluate_uarch_generalization.py" \
      --root "${RUN_ROOT}" --fastsim-root "${FASTSIM_ROOT}" \
      --out "${EVALUATION_ROOT}" --ranking-materiality-threshold 0.005

  phase=summarize
  run_retry "${phase}" \
    "${PYTHON}" "${ROOT}/tools/summarize_uarch_exploration.py" \
      --matrix "${MATRIX}" --evaluation "${EVALUATION_ROOT}" --out "${SUMMARY_ROOT}"

  write_state complete complete 1 "summary=${SUMMARY_ROOT}/summary.md"
}

start() {
  if pid_is_running; then
    echo "[uarch-v2] already running pid=$(<"${PID_FILE}")"
    status
    return 0
  fi
  touch "${LOG}"
  /usr/bin/nohup /usr/bin/setsid /usr/bin/env bash "${SCRIPT}" worker \
    >>"${LOG}" 2>&1 </dev/null &
  local pid=$!
  echo "${pid}" >"${PID_FILE}"
  sleep 1
  if ! kill -0 "${pid}" 2>/dev/null; then
    echo "[uarch-v2] failed to start; inspect ${LOG}" >&2
    return 1
  fi
  echo "[uarch-v2] started detached pid=${pid} log=${LOG}"
  start_disk_guard
  status
}

case "${ACTION}" in
  start) start ;;
  disk-guard-start) start_disk_guard ;;
  disk-guard) disk_guard ;;
  worker) worker ;;
  status) status ;;
  *) echo "usage: ${SCRIPT} {start|status|disk-guard-start}" >&2; exit 2 ;;
esac
