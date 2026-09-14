#!/usr/bin/env bash
set -euo pipefail

FASTSIM_ROOT=/data00/yinhaolang/FastSim
TCSIM_ROOT=/data00/yinhaolang/TCSim
SPEC_ROOT=${TCSIM_ROOT}/workloads/spec2026
ASTCENC_ROI_PATCH=${FASTSIM_ROOT}/patches/spec2026-heldout-astcenc-roi-v1.patch
BUILD_CONFIG_PATCH=${FASTSIM_ROOT}/patches/spec2026-heldout-build-config-v1.patch
ASTCENC_INPUT_ROOT=${FASTSIM_ROOT}/configs/spec2026-heldout-astcenc
HELDOUT_MANIFEST=${FASTSIM_ROOT}/configs/spec2026-heldout-workloads-v1.json
TCSIM_MANIFEST=${TCSIM_ROOT}/configs/gem5/spec2026_test_workloads.json
MANIFEST_INSTALLER=${FASTSIM_ROOT}/tools/install_spec2026_heldout_manifest.py
OUTPUT_IMAGE=${OUTPUT_IMAGE:-${TCSIM_ROOT}/data/spec2026_diskimg/spec2026-uarch-exploration-v1.ext4}
IMAGE_SIZE=${IMAGE_SIZE:-2G}
SKIP_REBUILD=${SKIP_REBUILD:-0}
TMP_ROOT=${FASTSIM_ROOT}/tmp

fail() {
  echo "[spec2026-astcenc][ERROR] $*" >&2
  exit 2
}

if [[ "${1:-}" == --skip-rebuild ]]; then
  SKIP_REBUILD=1
  shift
fi
(( $# == 0 )) || fail "unexpected arguments: $*"

for path in \
  "${ASTCENC_ROI_PATCH}" "${BUILD_CONFIG_PATCH}" \
  "${HELDOUT_MANIFEST}" "${TCSIM_MANIFEST}" "${MANIFEST_INSTALLER}" \
  "${SPEC_ROOT}/bin/runcpu"; do
  [[ -f "${path}" ]] || fail "missing required file: ${path}"
done
for cores in 4 8 16 32; do
  [[ -f "${ASTCENC_INPUT_ROOT}/heldout-c${cores}-inputs.txt" ]] || \
    fail "missing astcenc input list for ${cores} cores"
done

mkdir -p "${TMP_ROOT}"
export TMPDIR=${TMP_ROOT}/spec2026-astcenc-runcpu
mkdir -p "${TMPDIR}"
[[ ! -e "${OUTPUT_IMAGE}" ]] || fail "output image already exists: ${OUTPUT_IMAGE}"

astcenc_source=${SPEC_ROOT}/benchspec/CPU/731.astcenc_r/src/astcenccli_platform_dependents.cpp
if ! rg -q 'astcenc-workers-ready' "${astcenc_source}"; then
  (
    cd "${SPEC_ROOT}"
    patch --dry-run --batch -p1 <"${ASTCENC_ROI_PATCH}"
    patch --batch -p1 <"${ASTCENC_ROI_PATCH}"
  )
fi

if ! rg -q '^731\.astcenc_r:$' "${SPEC_ROOT}/config/gem5-x86-linux.cfg"; then
  (
    cd "${SPEC_ROOT}"
    patch --dry-run --batch -p1 <"${BUILD_CONFIG_PATCH}"
    patch --batch -p1 <"${BUILD_CONFIG_PATCH}"
  )
fi

python3 "${MANIFEST_INSTALLER}" \
  --temp-dir "${TMP_ROOT}" "${TCSIM_MANIFEST}" "${HELDOUT_MANIFEST}"

export LD_LIBRARY_PATH="${SPEC_ROOT}/local-libcrypt${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
(
  cd "${SPEC_ROOT}"
  if [[ "${SKIP_REBUILD}" != 1 ]]; then
    bin/runcpu --config=gem5-x86-linux.cfg --action=build --rebuild \
      --tune=base --size=test --threads=8 --define build_ncpus=64 \
      731.astcenc_r
  fi
  bin/runcpu --config=gem5-x86-linux.cfg --action=runsetup --nobuild \
    --tune=base --size=test --threads=8 --iterations=1 731.astcenc_r
)

astcenc_test=${SPEC_ROOT}/benchspec/CPU/731.astcenc_r/run/run_base_test_gem5-x86-linux.0000
[[ -x "${astcenc_test}/astcenc_r_base.gem5-x86-linux" ]] || \
  fail "rebuilt astcenc binary is missing"

stage=$(mktemp -d "${TMP_ROOT}/spec2026-astcenc-image.XXXXXX")
tmp_image=${TMP_ROOT}/spec2026-astcenc-image.$$.ext4
trap 'rm -rf -- "${stage}"; rm -f -- "${tmp_image}"' EXIT
stage_cpu=${stage}/spec2026/benchspec/CPU/731.astcenc_r/run
mkdir -p "${stage_cpu}"
cp -a "${astcenc_test}/." "${stage_cpu}/"
cp -a "${ASTCENC_INPUT_ROOT}/." "${stage_cpu}/"

mkdir -p "$(dirname -- "${OUTPUT_IMAGE}")"
truncate --size="${IMAGE_SIZE}" "${tmp_image}"
mkfs.ext4 -q -F -L SPEC2026_UA -d "${stage}" "${tmp_image}"
e2fsck -fn "${tmp_image}"
mv -- "${tmp_image}" "${OUTPUT_IMAGE}"
trap - EXIT
rm -rf -- "${stage}"
echo "[spec2026-astcenc] output=${OUTPUT_IMAGE}"
sha256sum "${OUTPUT_IMAGE}"
