#!/usr/bin/env bash
# Build only the independent SPR checkout with its local toolchain environment.
set -euo pipefail
project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
spr_python=/root/.pyenv/versions/3.11.14/bin/python3
spr_toolchain=/home/data00/xuhaoen/toolchains/protobuf
export PATH="/opt/gcc-11/bin:${spr_toolchain}/bin:${PATH}"
export PYTHON_CONFIG="${spr_python}-config"
export CC=/opt/gcc-11/bin/gcc
export CXX=/opt/gcc-11/bin/g++
export LD_LIBRARY_PATH="/root/.pyenv/versions/3.11.14/lib:/opt/gcc-11/lib64:${spr_toolchain}/lib:${LD_LIBRARY_PATH:-}"
export PKG_CONFIG_PATH="${spr_toolchain}/lib/pkgconfig:${PKG_CONFIG_PATH:-}"
export TAOGEN_SHARED="${project_root}/gem5-spr/taogen/shared"
mkdir -p "${project_root}/tmp/spr-build"
export TMPDIR="${project_root}/tmp/spr-build"
spr_jobs=$("${spr_python}" -c 'import os; print(max(1, len(os.sched_getaffinity(0)) * 80 // 100))')
cd -- "${project_root}/gem5-spr"
if [[ ! -f build/X86/gem5.build/config ]]; then
    /root/.local/bin/scons defconfig build/X86 "${project_root}/spr-framework/configs/gem5/calibration.defconfig"
fi
exec /root/.local/bin/scons build/X86/gem5.opt -j"${spr_jobs}" "$@"
