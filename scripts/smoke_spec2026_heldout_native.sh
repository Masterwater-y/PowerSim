#!/usr/bin/env bash
set -euo pipefail

FASTSIM_ROOT=/data00/yinhaolang/FastSim
IMAGE=${IMAGE:-/data00/yinhaolang/TCSim/data/spec2026_diskimg/spec2026-uarch-exploration-v1.ext4}
CORE_SET=${CORE_SET:-"4 8"}
TIMEOUT_SECONDS=${TIMEOUT_SECONDS:-60}
TMP_ROOT=${FASTSIM_ROOT}/tmp
mkdir -p "${TMP_ROOT}"
SMOKE_ROOT=${SMOKE_ROOT:-$(mktemp -d "${TMP_ROOT}/spec2026-astcenc-native.XXXXXX")}
mkdir -p "${SMOKE_ROOT}"
export TMPDIR=${SMOKE_ROOT}/process-tmp
mkdir -p "${TMPDIR}"
[[ -f "${IMAGE}" ]] || { echo "missing image: ${IMAGE}" >&2; exit 2; }

for cores in ${CORE_SET}; do
  payload=${SMOKE_ROOT}/payload-c${cores}
  mkdir -p "${payload}"
  debugfs -R "rdump /spec2026 ${payload}" "${IMAGE}" >/dev/null 2>&1
  run_dir=${payload}/spec2026/benchspec/CPU/731.astcenc_r/run
  echo "[spec2026-astcenc-native] cores=${cores}"
  (
    cd "${run_dir}"
    timeout "${TIMEOUT_SECONDS}" env GEM5_DISABLE_ROI_MARKER=1 \
      OMP_NUM_THREADS="${cores}" OMP_THREAD_LIMIT="${cores}" \
      ./astcenc_r_base.gem5-x86-linux "heldout-c${cores}-inputs.txt"
  )
  rm -rf -- "${payload}"
done
echo "[spec2026-astcenc-native] output=${SMOKE_ROOT}"
