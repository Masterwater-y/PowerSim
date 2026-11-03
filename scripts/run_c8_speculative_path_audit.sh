#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo=$(cd -- "${script_dir}/.." && pwd)
dataset=${DATASET:-${repo}/tmp/taotrace-fst-v7-c4-c8-formal-v3-semantic-20260816}
python_bin=${PYTHON:-python3}
workloads=${WORKLOADS_ROOT:-/data00/yinhaolang/TCSim/workloads/spec2026/benchspec/CPU}
cores=${CORES:-8}
speculative_dtlb_state=${SPECULATIVE_DTLB_STATE:-true}
l1i_enabled=${L1I_ENABLED:-true}
l1i_speculative_path_state=${L1I_SPECULATIVE_PATH_STATE:-true}
report_name=${REPORT_NAME:-fastsim.json}

case "${cores}" in
  4)
    case_prefix=04c
    accuracy_split=calibration-c4
    ;;
  8)
    case_prefix=08c
    accuracy_split=held-out-c8
    ;;
  *)
    echo "CORES must be 4 or 8" >&2
    exit 2
    ;;
esac

output=${OUTPUT:-${repo}/tmp/c${cores}-speculative-path-audit-20260816}
mkdir -p "${output}"

pairs=(
  "706.stockfish_r stockfish_base.gem5-x86-linux"
  "710.omnetpp_r omnetpp_r_base.gem5-x86-linux"
  "777.zstd_r zstd_base.gem5-x86-linux"
  "782.lbm_r lbm_r_base.gem5-x86-linux"
  "803.sph_exa_s sph_exa_base.gem5-x86-linux"
  "811.tealeaf_s tealeaf_base.gem5-x86-linux"
  "816.nab_s nab_s_base.gem5-x86-linux"
  "854.graph500_s graph500_s_base.gem5-x86-linux"
  "857.namd_s namd_s_base.gem5-x86-linux"
  "881.neutron_s neutron_base.gem5-x86-linux"
)

run_case() {
  local workload=$1
  local binary=$2
  local case_output=${output}/${workload}
  local decoded=${case_output}/static-instructions.jsonl
  local pilot=${case_output}/pilot
  mkdir -p "${case_output}"
  if [[ ! -f "${pilot}/pilot.json" ]]; then
    "${python_bin}" "${repo}/tools/decode_elf_static_instructions.py" \
      --binary "${workloads}/${workload}/exe/${binary}" \
      --output "${decoded}" --require-complete
    "${python_bin}" "${repo}/tools/materialize_fst_static_map_pilot.py" \
      --case "${dataset}/fst-v7/cases/${case_prefix}-${workload}" \
      --decoded "${decoded}" --output "${pilot}" --complete
  fi
  "${repo}/build/fastsim" simulate \
    --config "${dataset}/accuracy/${accuracy_split}/user-cache-state.cfg" \
    --manifest "${pilot}/tao_trace/manifest.txt" --cores "${cores}" \
    --l1i-enabled "${l1i_enabled}" \
    --l1i-speculative-path-state "${l1i_speculative_path_state}" \
    --l1i-miss-penalty 6 \
    --dtlb-speculative-path-state "${speculative_dtlb_state}" \
    --output "${case_output}/${report_name}"
}

pids=()
for pair in "${pairs[@]}"; do
  read -r workload binary <<<"${pair}"
  run_case "${workload}" "${binary}" >"${output}/${workload}.log" 2>&1 &
  pids+=("$!")
  if (( ${#pids[@]} == 4 )); then
    wait "${pids[0]}"
    pids=("${pids[@]:1}")
  fi
done
for pid in "${pids[@]}"; do
  wait "${pid}"
done

echo "completed C${cores} speculative-path audit: ${output}"
