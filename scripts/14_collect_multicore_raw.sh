#!/usr/bin/env bash
set -euo pipefail

THIS_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
source "${THIS_DIR}/env.sh"

TAOGEN_ROOT="${TAOGEN_ROOT:-${TAO_ROOT}/datagen}"
GEM5="${GEM5:-${TAO_GEM5_ROOT}/build/X86_MESI_Three_Level/gem5.opt}"
GEM5_CFG="${GEM5_CFG:-${TAOGEN_ROOT}/configs/run_mt_mvp.py}"
NUM_CORES="${NUM_CORES:-4}"
GROUP="${GROUP:-all}"
RAW_ROOT="${RAW_ROOT:-${TAOGEN_ROOT}/tmp/${GROUP}_${NUM_CORES}c_raw_$(date +%Y%m%d_%H%M%S)/runs}"
WORKLOADS="${WORKLOADS:-}"
FORCE=0
DRY_RUN=0
BUILD=1

WL="${TAOGEN_ROOT}/workloads"
declare -A WL_BIN
declare -A WL_ARGS
declare -A WL_BUILD_DIR

WL_BIN[W11_stream_mix]="${WL}/mt_stream_mix/mt_stream_mix"
WL_BIN[W12_stencil2d]="${WL}/mt_stencil2d/mt_stencil2d"
WL_BIN[W13_graph_walk]="${WL}/mt_graph_walk/mt_graph_walk"
WL_BIN[W14_branch_state]="${WL}/mt_branch_state_machine/mt_branch_state_machine"
WL_BIN[W15_indirect]="${WL}/mt_indirect_dispatch/mt_indirect_dispatch"

WL_ARGS[W11_stream_mix]="${WL_ARGS_W11:-4 47 256 1 11}"
WL_ARGS[W12_stencil2d]="${WL_ARGS_W12:-4 11 256 1 12}"
WL_ARGS[W13_graph_walk]="${WL_ARGS_W13:-4 1 640 1 13}"
WL_ARGS[W14_branch_state]="${WL_ARGS_W14:-4 11 64 1 14}"
WL_ARGS[W15_indirect]="${WL_ARGS_W15:-4 11 256 1 15}"

WL_BUILD_DIR[W11_stream_mix]="${WL}/mt_stream_mix"
WL_BUILD_DIR[W12_stencil2d]="${WL}/mt_stencil2d"
WL_BUILD_DIR[W13_graph_walk]="${WL}/mt_graph_walk"
WL_BUILD_DIR[W14_branch_state]="${WL}/mt_branch_state_machine"
WL_BUILD_DIR[W15_indirect]="${WL}/mt_indirect_dispatch"

WL_BIN[H01_mixed_service]="${WL}/holdout_mixed_service/holdout_mixed_service"
WL_BIN[H02_sharded_kv]="${WL}/holdout_sharded_kv/holdout_sharded_kv"
WL_BIN[H03_analytics_scan]="${WL}/holdout_analytics_scan/holdout_analytics_scan"

WL_ARGS[H01_mixed_service]="${WL_ARGS_H01:-4 3 256 1 101}"
WL_ARGS[H02_sharded_kv]="${WL_ARGS_H02:-4 2 512 2 102}"
WL_ARGS[H03_analytics_scan]="${WL_ARGS_H03:-4 3 512 3 103}"

WL_BUILD_DIR[H01_mixed_service]="${WL}/holdout_mixed_service"
WL_BUILD_DIR[H02_sharded_kv]="${WL}/holdout_sharded_kv"
WL_BUILD_DIR[H03_analytics_scan]="${WL}/holdout_analytics_scan"

W_WORKLOADS="W11_stream_mix W12_stencil2d W13_graph_walk W14_branch_state W15_indirect"
H_WORKLOADS="H01_mixed_service H02_sharded_kv H03_analytics_scan"

usage() {
  cat <<EOF
usage: $0 [--group w|h|all] [--workloads "W11_stream_mix H01_mixed_service"]
          [--num-cores <n>] [--raw-root <dir>] [--force]
          [--no-build] [--dry-run]

Collect multicore gem5 raw traces for W and/or H workloads.

Controls:
  --num-cores <n>       core count passed to gem5 and workload argv[0]
  --workloads <list>    explicit workload list; overrides --group
  --group <w|h|all>     default workload set when --workloads is omitted
  --raw-root <dir>      output root; each workload writes to raw-root/workload
  --force               remove existing raw workload dir before collecting
  --no-build            skip make -C workload dirs
  --dry-run             print commands without running make/gem5

Workload sizes are controlled by WL_ARGS_* env vars, for example:
  WL_ARGS_W13="16 2 1024 1 13" NUM_CORES=16 $0 --workloads "W13_graph_walk"
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --group) GROUP="$2"; shift 2 ;;
    --workloads) WORKLOADS="$2"; shift 2 ;;
    --num-cores) NUM_CORES="$2"; shift 2 ;;
    --raw-root) RAW_ROOT="$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --no-build) BUILD=0; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "[collect-raw][FATAL] unknown arg: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ -z "${WORKLOADS}" ]]; then
  case "${GROUP}" in
    w|W) WORKLOADS="${W_WORKLOADS}" ;;
    h|H|holdout) WORKLOADS="${H_WORKLOADS}" ;;
    all|ALL) WORKLOADS="${W_WORKLOADS} ${H_WORKLOADS}" ;;
    *) echo "[collect-raw][FATAL] unknown group: ${GROUP}" >&2; exit 2 ;;
  esac
fi

for p in "${GEM5}" "${GEM5_CFG}"; do
  if [[ ! -e "${p}" ]]; then
    echo "[collect-raw][FATAL] missing required path: ${p}" >&2
    exit 2
  fi
done

mkdir -p "${RAW_ROOT}"
echo "[collect-raw] raw_root=${RAW_ROOT}"
echo "[collect-raw] num_cores=${NUM_CORES}"
echo "[collect-raw] workloads=${WORKLOADS}"

for wl_name in ${WORKLOADS}; do
  bin="${WL_BIN[$wl_name]:-}"
  args_s="${WL_ARGS[$wl_name]:-}"
  build_dir="${WL_BUILD_DIR[$wl_name]:-}"
  if [[ -z "${bin}" || -z "${args_s}" || -z "${build_dir}" ]]; then
    echo "[collect-raw][FATAL] unknown workload: ${wl_name}" >&2
    exit 2
  fi

  if [[ "${BUILD}" -eq 1 ]]; then
    if [[ "${DRY_RUN}" -eq 1 ]]; then
      echo "[collect-raw][dry-run] make -C ${build_dir}"
    else
      make -C "${build_dir}"
    fi
  fi

  read -r -a args <<< "${args_s}"
  args[0]="${NUM_CORES}"
  out="${RAW_ROOT}/${wl_name}"

  if [[ "${FORCE}" -eq 1 && "${DRY_RUN}" -eq 0 ]]; then
    rm -rf "${out}"
  fi
  if [[ -d "${out}/tao_trace" ]]; then
    echo "[collect-raw] reuse ${wl_name}: ${out}"
    continue
  fi

  if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "[collect-raw][dry-run] ${GEM5} --outdir=${out} ${GEM5_CFG} --cmd ${bin} --workload-args ${args[*]} --num-cores ${NUM_CORES} --require-roi"
    continue
  fi

  if [[ ! -x "${bin}" ]]; then
    echo "[collect-raw][FATAL] missing executable workload: ${bin}" >&2
    exit 2
  fi

  rm -rf "${out}"
  mkdir -p "${out}"
  echo "[collect-raw] gem5 ${wl_name}: args=${args[*]} out=${out}"
  "${GEM5}" --outdir="${out}" "${GEM5_CFG}" \
    --cmd "${bin}" --workload-args "${args[@]}" \
    --num-cores "${NUM_CORES}" --require-roi \
    > "${out}/gem5.log" 2>&1
done

echo "[collect-raw] done: ${RAW_ROOT}"
