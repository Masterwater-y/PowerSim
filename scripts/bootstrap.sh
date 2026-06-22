#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
JOBS=${JOBS:-$(nproc 2>/dev/null || echo 4)}
DYNAMORIO_REF=${DYNAMORIO_REF:-master}
SNIPER_REF=${SNIPER_REF:-9ccff91}

WITH_DYNAMORIO=1
WITH_SNIPER=1
WITH_MINESIM=1
WITH_WORKLOADS=1

usage() {
    cat <<EOF
Usage: scripts/bootstrap.sh [options]

Options:
  --skip-dynamorio   do not download/build DynamoRIO
  --skip-sniper      do not download/build Sniper
  --skip-minesim     do not build MineSim
  --skip-workloads   do not build workloads
  -j, --jobs N       parallel build jobs, default: $JOBS
  -h, --help         show help

Environment:
  DYNAMORIO_REF      DynamoRIO git ref, default: $DYNAMORIO_REF
  SNIPER_REF         Sniper git ref, default: $SNIPER_REF
  CC/CXX             compiler commands, default prefers gcc-11/g++-11
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-dynamorio) WITH_DYNAMORIO=0; shift ;;
        --skip-sniper) WITH_SNIPER=0; shift ;;
        --skip-minesim) WITH_MINESIM=0; shift ;;
        --skip-workloads) WITH_WORKLOADS=0; shift ;;
        -j|--jobs)
            [[ $# -ge 2 ]] || { echo "error: $1 needs an argument" >&2; exit 1; }
            JOBS="$2"
            shift 2
            ;;
        -h|--help) usage; exit 0 ;;
        *) echo "error: unknown argument: $1" >&2; usage >&2; exit 1 ;;
    esac
done

log() {
    printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2
}

need_cmd() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "error: missing required command: $1" >&2
        exit 1
    }
}

choose_compilers() {
    if [[ -z "${CC:-}" ]]; then
        if command -v gcc-11 >/dev/null 2>&1; then
            export CC=gcc-11
        else
            export CC=gcc
        fi
    fi
    if [[ -z "${CXX:-}" ]]; then
        if command -v g++-11 >/dev/null 2>&1; then
            export CXX=g++-11
        else
            export CXX=g++
        fi
    fi
}

choose_python() {
    local candidate
    for candidate in "${PYTHON:-}" python3.13 python3.12 python3.11 python3.10 python3.9 python3.8 python3 /root/miniconda3/bin/python3; do
        [[ -n "$candidate" ]] || continue
        if ! command -v "$candidate" >/dev/null 2>&1; then
            continue
        fi
        if "$candidate" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 8) else 1)
PY
        then
            export PYTHON=$(command -v "$candidate")
            export PATH="$(dirname "$PYTHON"):$PATH"
            return 0
        fi
    done
    echo "error: Python >= 3.8 is required for Sniper/XED; set PYTHON=/path/to/python3.8+" >&2
    exit 1
}

check_prereqs() {
    for cmd in git cmake make "$PYTHON" python3-config wget tar "$CC" "$CXX"; do
        need_cmd "$cmd"
    done
    if ! command -v perf >/dev/null 2>&1; then
        echo "warning: perf is not in PATH; full validation will fail until perf is installed" >&2
    fi
}

clone_or_update() {
    local url=$1
    local dir=$2
    local ref=$3
    if [[ ! -d "$dir/.git" ]]; then
        rm -rf "$dir"
        git clone --recursive "$url" "$dir"
    fi
    git -C "$dir" fetch --tags --prune
    git -C "$dir" checkout "$ref"
    git -C "$dir" reset --hard "$ref"
    git -C "$dir" submodule update --init --recursive
}

build_dynamorio() {
    local src="$ROOT/_deps/dynamorio-src"
    local build="$ROOT/_build/dynamorio"
    local install="$ROOT/dynamorio"
    log "downloading DynamoRIO ($DYNAMORIO_REF)"
    clone_or_update https://github.com/DynamoRIO/dynamorio.git "$src" "$DYNAMORIO_REF"
    log "building DynamoRIO"
    cmake -S "$src" -B "$build" \
        -DCMAKE_BUILD_TYPE=RelWithDebInfo \
        -DCMAKE_INSTALL_PREFIX="$install" \
        -DBUILD_DOCS=OFF
    cmake --build "$build" -j "$JOBS"
    cmake --install "$build"
    if [[ -d "$install/lib64/release" && ! -e "$install/lib64/debug" ]]; then
        ln -s release "$install/lib64/debug"
    fi
    if [[ -d "$install/lib64" && ! -e "$install/lib32" ]]; then
        # Some drrun versions validate both bitness directories even for -64 runs.
        # The environment only uses 64-bit workloads, so this is a compatibility link.
        ln -s lib64 "$install/lib32"
    fi
    install -m 0755 "$ROOT/scripts/collect_drmemtrace.sh" "$install/collect_drmemtrace.sh"
    "$install/bin64/drrun" -version || true
}

build_sniper() {
    local dir="$ROOT/snipersim"
    log "downloading Sniper ($SNIPER_REF)"
    clone_or_update https://github.com/snipersim/snipersim.git "$dir" "$SNIPER_REF"
    log "applying Sniper local build patch"
    if ! git -C "$dir" apply --check "$ROOT/patches/snipersim/0001-local-build-fixes.patch" >/dev/null 2>&1; then
        echo "warning: Sniper patch is already applied or does not apply cleanly; continuing" >&2
    else
        git -C "$dir" apply "$ROOT/patches/snipersim/0001-local-build-fixes.patch"
    fi
    if [[ -d "$ROOT/patches/snipersim/config" ]]; then
        install -d "$dir/config"
        install -m 0644 "$ROOT"/patches/snipersim/config/*.cfg "$dir/config/"
    fi
    log "building Sniper"
    make -C "$dir" -j "$JOBS"
}

write_env() {
    cat > "$ROOT/env.sh" <<EOF
export SIM_ROOT="$ROOT"
export CC="${CC}"
export CXX="${CXX}"
export PYTHON="${PYTHON}"
export SNIPER_ROOT="\$SIM_ROOT/snipersim"
export LD_LIBRARY_PATH="/opt/gcc-11/lib64:\$SIM_ROOT/dynamorio/lib64/release:\$SIM_ROOT/dynamorio/lib64/debug:\$SIM_ROOT/dynamorio/lib64:\$SNIPER_ROOT/xed_kit/lib:\$SNIPER_ROOT/lib:\$SNIPER_ROOT/libtorch/lib:\${LD_LIBRARY_PATH:-}"
EOF
}

build_minesim() {
    log "building MineSim"
    make -C "$ROOT/minesim" clean minesim
}

build_workloads() {
    log "building workloads"
    find "$ROOT/workloads" -mindepth 2 -maxdepth 2 -name Makefile -print0 |
        while IFS= read -r -d '' mf; do
            make -C "$(dirname "$mf")" clean all
        done
}

main() {
    choose_compilers
    choose_python
    check_prereqs
    mkdir -p "$ROOT/_deps" "$ROOT/_build"
    if [[ $WITH_DYNAMORIO -eq 1 ]]; then build_dynamorio; fi
    if [[ $WITH_SNIPER -eq 1 ]]; then build_sniper; fi
    write_env
    # shellcheck disable=SC1091
    source "$ROOT/env.sh"
    if [[ $WITH_MINESIM -eq 1 ]]; then build_minesim; fi
    if [[ $WITH_WORKLOADS -eq 1 ]]; then build_workloads; fi
    log "bootstrap complete"
    echo "Run: source '$ROOT/env.sh'"
}

main "$@"
