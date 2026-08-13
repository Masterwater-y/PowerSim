#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -z "${JOBS:-}" ]]; then
  AVAILABLE_JOBS=$(nproc)
  JOBS=$(( AVAILABLE_JOBS < 24 ? AVAILABLE_JOBS : 24 ))
fi
MODE=${MODE:-apply-and-build}
TARGET=${TARGET:-mesi}
GEM5_ROOT=${GEM5_ROOT:-"$ROOT/../gem5_fastsim"}
DYNAMORIO_ROOT=${DYNAMORIO_ROOT:-"$ROOT/../DynamoRIO-Linux-11.3.0-1"}
GEM5_TOOLCHAIN_VENV=${GEM5_TOOLCHAIN_VENV:-"$ROOT/.gem5_build_env/venv"}
PYTHON=${PYTHON:-"$GEM5_TOOLCHAIN_VENV/bin/python"}
SCONS=${SCONS:-"$GEM5_TOOLCHAIN_VENV/bin/scons"}

if [[ ! -x "$PYTHON" || ! -x "$SCONS" ]]; then
  echo "[fastsim-gem5][ERROR] Python/SCons toolchain is missing: $GEM5_TOOLCHAIN_VENV" >&2
  exit 2
fi

if [[ ! -d "$GEM5_ROOT/.git" ]]; then
  echo "[fastsim-gem5][ERROR] not a gem5 checkout: $GEM5_ROOT" >&2
  exit 2
fi

# The overlays and every gem5 variant share one mutable checkout.  In
# particular, an interrupted SCons run can leave a compiler child writing a
# generated Python object after its parent has gone away.  Serialize complete
# apply/configure/build sessions and retain an owner PID so a detached child
# cannot be mistaken for a stale lock.
LOCK_PATH="$GEM5_ROOT/.fastsim-build.lock"
OWNER_PATH="$GEM5_ROOT/.fastsim-build.owner"
exec {BUILD_LOCK_FD}>"$LOCK_PATH"
echo "[fastsim-gem5] waiting for checkout build lock: $LOCK_PATH"
flock "$BUILD_LOCK_FD"

if [[ -s "$OWNER_PATH" ]]; then
  read -r OWNER_PID < "$OWNER_PATH" || OWNER_PID=""
  if [[ "$OWNER_PID" =~ ^[0-9]+$ ]] && kill -0 "$OWNER_PID" 2>/dev/null; then
    echo "[fastsim-gem5][ERROR] active build session PID=$OWNER_PID owns $GEM5_ROOT" >&2
    echo "[fastsim-gem5][ERROR] wait for it or terminate that process group before retrying" >&2
    exit 3
  fi
  rm -f "$OWNER_PATH"
fi

BUILD_CHILD_PID=""
cleanup_build_session() {
  local status=$?
  trap - EXIT INT TERM
  if [[ -n "$BUILD_CHILD_PID" ]] && kill -0 "$BUILD_CHILD_PID" 2>/dev/null; then
    echo "[fastsim-gem5] stopping build process group $BUILD_CHILD_PID" >&2
    kill -TERM -- "-$BUILD_CHILD_PID" 2>/dev/null || true
    wait "$BUILD_CHILD_PID" 2>/dev/null || true
  fi
  rm -f "$OWNER_PATH"
  exit "$status"
}
trap 'exit 130' INT
trap 'exit 143' TERM
trap cleanup_build_session EXIT
readarray -t python_paths < <("$PYTHON" -c '
import sys
import sysconfig
print(f"{sys.version_info.major}.{sys.version_info.minor}")
print(sysconfig.get_config_var("BINDIR") or "")
print(sysconfig.get_config_var("LIBDIR") or "")
')
if [[ "${python_paths[0]}" != "3.11" ]]; then
  echo "[fastsim-gem5][ERROR] gem5 toolchain must use Python 3.11" >&2
  exit 2
fi
PYTHON_CONFIG="${python_paths[1]}/python3.11-config"
PYTHON_LIBDIR="${python_paths[2]}"
if [[ ! -x "$PYTHON_CONFIG" || ! -d "$PYTHON_LIBDIR" ]]; then
  echo "[fastsim-gem5][ERROR] incomplete Python 3.11 toolchain" >&2
  exit 2
fi

export PYTHON SCONS PYTHON_CONFIG JOBS MODE TARGET
export FASTSIM_DYNAMORIO_ROOT="$DYNAMORIO_ROOT"
GCC_RUNTIME_LIBDIR=${GCC_RUNTIME_LIBDIR:-/opt/gcc-11.5.0/lib64}
export LD_LIBRARY_PATH="$PYTHON_LIBDIR:$GCC_RUNTIME_LIBDIR"

setsid bash "$ROOT/vendor/gem5_patch/apply_and_build.sh" "$GEM5_ROOT" &
BUILD_CHILD_PID=$!
printf '%s\n' "$BUILD_CHILD_PID" > "$OWNER_PATH"
echo "[fastsim-gem5] build session PID=$BUILD_CHILD_PID JOBS=$JOBS"
wait "$BUILD_CHILD_PID" || {
  status=$?
  exit "$status"
}
