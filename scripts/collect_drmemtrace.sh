#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  collect_drmemtrace.sh [options] -- <program> [args...]

Options:
  -o, --output-root DIR     output root, default: ./drmemtrace_out
  -n, --name NAME           run name, default: program timestamp
      --subdir-prefix STR   drmemtrace subdir prefix
      --raw-compress TYPE   raw trace compression: snappy,snappy_nocrc,gzip,zlib,lz4,none
      --dr-timeout SEC      kill the traced process after SEC seconds, default: $DR_TIMEOUT_SECONDS or 0
      --dr-debug            pass -dr_debug to drcachesim
  -h, --help                show help
EOF
}

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
DR_ROOT="$SCRIPT_DIR"
DRRUN="$DR_ROOT/bin64/drrun"
DRRAW2TRACE="$DR_ROOT/tools/bin64/drraw2trace"

OUTPUT_ROOT="$PWD/drmemtrace_out"
RUN_NAME=""
SUBDIR_PREFIX=""
RAW_COMPRESS="lz4"
USE_DR_DEBUG=0
DR_TIMEOUT_SECONDS="${DR_TIMEOUT_SECONDS:-0}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        -o|--output-root)
            [[ $# -ge 2 ]] || { echo "error: $1 needs an argument" >&2; exit 1; }
            OUTPUT_ROOT="$2"
            shift 2
            ;;
        -n|--name)
            [[ $# -ge 2 ]] || { echo "error: $1 needs an argument" >&2; exit 1; }
            RUN_NAME="$2"
            shift 2
            ;;
        --subdir-prefix)
            [[ $# -ge 2 ]] || { echo "error: $1 needs an argument" >&2; exit 1; }
            SUBDIR_PREFIX="$2"
            shift 2
            ;;
        --raw-compress)
            [[ $# -ge 2 ]] || { echo "error: $1 needs an argument" >&2; exit 1; }
            RAW_COMPRESS="$2"
            shift 2
            ;;
        --dr-timeout)
            [[ $# -ge 2 ]] || { echo "error: $1 needs an argument" >&2; exit 1; }
            DR_TIMEOUT_SECONDS="$2"
            shift 2
            ;;
        --dr-debug)
            USE_DR_DEBUG=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            break
            ;;
        *)
            echo "error: unknown argument: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

if [[ $# -eq 0 ]]; then
    echo "error: missing program after --" >&2
    usage >&2
    exit 1
fi

if [[ ! -x "$DRRUN" ]]; then
    echo "error: missing executable drrun: $DRRUN" >&2
    exit 1
fi

if [[ ! -x "$DRRAW2TRACE" ]]; then
    echo "error: missing executable drraw2trace: $DRRAW2TRACE" >&2
    exit 1
fi

case "$RAW_COMPRESS" in
    snappy|snappy_nocrc|gzip|zlib|lz4|none) ;;
    *) echo "error: unsupported --raw-compress value: $RAW_COMPRESS" >&2; exit 1 ;;
esac

CMD=("$@")
PROGRAM_BASENAME=$(basename -- "${CMD[0]}")
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

if [[ -z "$RUN_NAME" ]]; then
    RUN_NAME="${PROGRAM_BASENAME}_${TIMESTAMP}"
fi

RUN_ROOT="$OUTPUT_ROOT/$RUN_NAME"
RAW_ROOT="$RUN_ROOT/raw"
TRACE_ROOT="$RUN_ROOT/trace"

mkdir -p "$RAW_ROOT" "$TRACE_ROOT"

echo "[1/2] collecting raw trace"
echo "      output: $RAW_ROOT"
echo "      compression: $RAW_COMPRESS"
printf '      command: '
printf '%q ' "${CMD[@]}"
printf '\n'

DRRUN_ARGS=(
    -64
    -root "$DR_ROOT"
    -t drcachesim
    -offline
    -outdir "$RAW_ROOT"
    -raw_compress "$RAW_COMPRESS"
)

if [[ "$DR_TIMEOUT_SECONDS" != "0" ]]; then
    DRRUN_ARGS=( -s "$DR_TIMEOUT_SECONDS" "${DRRUN_ARGS[@]}" )
fi

if [[ -n "$SUBDIR_PREFIX" ]]; then
    DRRUN_ARGS+=( -subdir_prefix "$SUBDIR_PREFIX" )
fi

if [[ $USE_DR_DEBUG -eq 1 ]]; then
    DRRUN_ARGS+=( -dr_debug )
fi

"$DRRUN" "${DRRUN_ARGS[@]}" -- "${CMD[@]}"

shopt -s nullglob
RAW_DIRS=("$RAW_ROOT"/*.dir/raw)
shopt -u nullglob

if [[ ${#RAW_DIRS[@]} -eq 0 ]]; then
    echo "error: no raw trace directory found under $RAW_ROOT" >&2
    exit 1
fi

echo "[2/2] converting raw trace to .trace.gz"
for raw_dir in "${RAW_DIRS[@]}"; do
    echo "      converting: $raw_dir"
    "$DRRAW2TRACE" -indir "$raw_dir" -out "$TRACE_ROOT" -compress gzip
done

shopt -s nullglob
TRACE_FILES=("$TRACE_ROOT"/*.trace.gz)
shopt -u nullglob

if [[ ${#TRACE_FILES[@]} -eq 0 ]]; then
    echo "error: conversion finished but no .trace.gz found under $TRACE_ROOT" >&2
    exit 1
fi

echo
echo "done:"
echo "  raw root:   $RAW_ROOT"
echo "  trace root: $TRACE_ROOT"
for trace_file in "${TRACE_FILES[@]}"; do
    echo "  trace:      $trace_file"
done
