#!/usr/bin/env bash
# Apply the TCSim v29 gem5 overlay to an exact gem5 v25.1.0.1 checkout and build it.
set -euo pipefail

PATCH_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GEM5_ROOT=${1:-"$(cd "$PATCH_ROOT/../../.." && pwd)/../gem5"}
JOBS=${JOBS:-$(nproc)}
PYTHON_BIN=${PYTHON:-$(command -v python3.11 || command -v python3)}
SCONS_BIN=${SCONS:-$(command -v scons)}
PYTHON_BINDIR=$(cd "$(dirname "$PYTHON_BIN")" && pwd)
if [[ -x "$PYTHON_BINDIR/python3-config" ]]; then
  DEFAULT_PYTHON_CONFIG="$PYTHON_BINDIR/python3-config"
elif [[ -x "$PYTHON_BINDIR/python-config" ]]; then
  DEFAULT_PYTHON_CONFIG="$PYTHON_BINDIR/python-config"
else
  DEFAULT_PYTHON_CONFIG=$(command -v python3-config || command -v python-config)
fi
PYTHON_CONFIG_BIN=${PYTHON_CONFIG:-$DEFAULT_PYTHON_CONFIG}
MODE=${MODE:-apply-and-build}

if [[ ! -d "$GEM5_ROOT/.git" ]]; then
  echo "[tcsim-v29-gem5][ERROR] not a gem5 checkout: $GEM5_ROOT" >&2
  echo "Clone tag v25.1.0.1 first, then rerun this script." >&2
  exit 2
fi

base_commit=$(git -C "$GEM5_ROOT" rev-parse HEAD)
if [[ "$base_commit" != "c8222cc67a399bfc01e8658dd14b30d5bfd634f9" ]]; then
  echo "[tcsim-v29-gem5][ERROR] expected gem5 v25.1.0.1 commit c8222cc67a399bfc01e8658dd14b30d5bfd634f9" >&2
  echo "[tcsim-v29-gem5][ERROR] observed $base_commit" >&2
  exit 2
fi

(
  cd "$PATCH_ROOT"
  sha256sum -c SHA256SUMS
)

if [[ "$MODE" == "apply" || "$MODE" == "apply-and-build" ]]; then
  echo "[tcsim-v29-gem5] applying overlay to $GEM5_ROOT"
  cp -a "$PATCH_ROOT/overlay/." "$GEM5_ROOT/"
fi

if [[ "$MODE" == "apply" ]]; then
  exit 0
fi
if [[ "$MODE" != "build" && "$MODE" != "apply-and-build" ]]; then
  echo "[tcsim-v29-gem5][ERROR] MODE must be apply, build, or apply-and-build" >&2
  exit 2
fi

echo "[tcsim-v29-gem5] configuring X86_MESI_Three_Level"
(
  cd "$GEM5_ROOT"
  PYTHON_CONFIG="$PYTHON_CONFIG_BIN" \
    "$PYTHON_BIN" "$SCONS_BIN" defconfig \
    build/X86_MESI_Three_Level \
    "$PATCH_ROOT/overlay/build_opts/X86_MESI_Three_Level"
)

echo "[tcsim-v29-gem5] building with JOBS=$JOBS"
(
  cd "$GEM5_ROOT"
  TAOGEN_SHARED="$PATCH_ROOT/shared" \
  PYTHON_CONFIG="$PYTHON_CONFIG_BIN" \
    "$PYTHON_BIN" "$SCONS_BIN" \
    build/X86_MESI_Three_Level/gem5.opt \
    PROTOCOL=MESI_Three_Level -j"$JOBS"
)

echo "[tcsim-v29-gem5] OK: $GEM5_ROOT/build/X86_MESI_Three_Level/gem5.opt"
