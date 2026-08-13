#!/usr/bin/env bash
# Apply FastSim's gem5 overlays to an exact gem5 v25.1.0.1 checkout.
set -euo pipefail

PATCH_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GEM5_ROOT=${1:?dedicated gem5 checkout path is required}
if [[ -z "${JOBS:-}" ]]; then
  AVAILABLE_JOBS=$(nproc)
  JOBS=$(( AVAILABLE_JOBS < 24 ? AVAILABLE_JOBS : 24 ))
fi
PYTHON_BIN=${PYTHON:?compile_gem5.sh must provide PYTHON}
SCONS_BIN=${SCONS:?compile_gem5.sh must provide SCONS}
PYTHON_CONFIG_BIN=${PYTHON_CONFIG:?compile_gem5.sh must provide PYTHON_CONFIG}
MODE=${MODE:-apply-and-build}
TARGET=${TARGET:-all}

if [[ ! -d "$GEM5_ROOT/.git" ]]; then
  echo "[fastsim-gem5][ERROR] not a gem5 checkout: $GEM5_ROOT" >&2
  echo "Clone tag v25.1.0.1 first, then rerun this script." >&2
  exit 2
fi

base_commit=$(git -C "$GEM5_ROOT" rev-parse HEAD)
if [[ "$base_commit" != "c8222cc67a399bfc01e8658dd14b30d5bfd634f9" ]]; then
  echo "[fastsim-gem5][ERROR] expected gem5 v25.1.0.1 commit c8222cc67a399bfc01e8658dd14b30d5bfd634f9" >&2
  echo "[fastsim-gem5][ERROR] observed $base_commit" >&2
  exit 2
fi

if [[ "$MODE" != "apply" && "$MODE" != "build" && "$MODE" != "apply-and-build" ]]; then
  echo "[fastsim-gem5][ERROR] MODE must be apply, build, or apply-and-build" >&2
  exit 2
fi
if [[ "$TARGET" != "all" && "$TARGET" != "mesi" && "$TARGET" != "dr" ]]; then
  echo "[fastsim-gem5][ERROR] TARGET must be all, mesi, or dr" >&2
  exit 2
fi

apply_overlay() {
  local name=$1
  local root=$2
  (
    cd "$root"
    sha256sum -c SHA256SUMS
  )
  if [[ "$MODE" == "apply" || "$MODE" == "apply-and-build" ]]; then
    echo "[fastsim-gem5] applying $name overlay to $GEM5_ROOT"
    rsync -a --checksum "$root/overlay/" "$GEM5_ROOT/"
  fi
}

if [[ "$TARGET" == "all" || "$TARGET" == "mesi" ]]; then
  apply_overlay "reference" "$PATCH_ROOT/reference"
fi
if [[ "$TARGET" == "all" || "$TARGET" == "dr" ]]; then
  apply_overlay "DR converter" "$PATCH_ROOT/dr_converter"
fi

if [[ "$MODE" == "apply" ]]; then
  exit 0
fi

if [[ "$TARGET" == "all" || "$TARGET" == "mesi" ]]; then
  echo "[fastsim-gem5] configuring X86_MESI_Three_Level"
  (
    cd "$GEM5_ROOT"
    PYTHON_CONFIG="$PYTHON_CONFIG_BIN" \
      "$PYTHON_BIN" "$SCONS_BIN" defconfig \
      build/X86_MESI_Three_Level \
      "$PATCH_ROOT/reference/overlay/build_opts/X86_MESI_Three_Level"
  )

  echo "[fastsim-gem5] building X86_MESI_Three_Level with JOBS=$JOBS"
  (
    cd "$GEM5_ROOT"
    TAOGEN_SHARED="$PATCH_ROOT/reference/shared" \
    PYTHON_CONFIG="$PYTHON_CONFIG_BIN" \
      "$PYTHON_BIN" "$SCONS_BIN" \
      build/X86_MESI_Three_Level/gem5.opt \
      PROTOCOL=MESI_Three_Level -j"$JOBS"
  )

  echo "[fastsim-gem5] OK: $GEM5_ROOT/build/X86_MESI_Three_Level/gem5.opt"
fi

if [[ "$TARGET" == "all" || "$TARGET" == "dr" ]]; then
  DRMEMTRACE_ROOT=${FASTSIM_DYNAMORIO_ROOT:?build_gem5.sh must provide FASTSIM_DYNAMORIO_ROOT for TARGET=$TARGET}
  if [[ ! -d "$DRMEMTRACE_ROOT/tools/include/drmemtrace" ]]; then
    echo "[fastsim-gem5][ERROR] invalid DynamoRIO root: $DRMEMTRACE_ROOT" >&2
    exit 2
  fi

  echo "[fastsim-gem5] configuring X86_DRMEMTRACE"
  (
    cd "$GEM5_ROOT"
    PYTHON_CONFIG="$PYTHON_CONFIG_BIN" \
      "$PYTHON_BIN" "$SCONS_BIN" defconfig \
      build/X86_DRMEMTRACE \
      "$PATCH_ROOT/dr_converter/overlay/build_opts/X86_DRMEMTRACE"
  )

  echo "[fastsim-gem5] building X86_DRMEMTRACE with JOBS=$JOBS"
  (
    cd "$GEM5_ROOT"
    TAOGEN_SHARED="$PATCH_ROOT/reference/shared" \
    PYTHON_CONFIG="$PYTHON_CONFIG_BIN" \
    "$PYTHON_BIN" "$SCONS_BIN" \
      build/X86_DRMEMTRACE/gem5.fast \
      DRMEMTRACE_ROOT="$DRMEMTRACE_ROOT" \
      FASTSIM_INCLUDE_ROOT="$PATCH_ROOT/../../include" -j"$JOBS"
  )

  echo "[fastsim-gem5] OK: $GEM5_ROOT/build/X86_DRMEMTRACE/gem5.fast"
fi
