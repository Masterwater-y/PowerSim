#!/usr/bin/env bash
# Formal-only offline entrypoint: skips the 136-case smoke gate and runs the
# formal phase directly, reusing the prepared checkpoints in an existing output
# directory. All other collector gates (identity, common-end, native sideband,
# frozen inputs) still apply.
set -euo pipefail
FASTSIM_COLLECTION_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
FASTSIM_COLLECTION_PYTHON="${FASTSIM_COLLECTION_PYTHON:-/data00/yinhaolang/infer/.venv/bin/python}"
if [[ ! -x "$FASTSIM_COLLECTION_PYTHON" ]]; then
    FASTSIM_COLLECTION_PYTHON="$(command -v python3)"
fi
"$FASTSIM_COLLECTION_PYTHON" -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ required; set FASTSIM_COLLECTION_PYTHON"'
cd -- "$FASTSIM_COLLECTION_ROOT"

action="${1:-run}"
if [[ $# -ge 1 ]]; then shift; fi
case "$action" in
  start)
    out="tmp/uarch-generalization-c4-c8-20260917"
    for ((i=1; i<=$#; i++)); do
      if [[ "${!i}" == "--output" ]]; then j=$((i+1)); out="${!j}"; fi
    done
    mkdir -p "$out"
    nohup "$FASTSIM_COLLECTION_PYTHON" scripts/collect_uarch_formal_only.py "$@" \
      >> "$out/formal-only.log" 2>&1 &
    echo "started pid=$! log=$out/formal-only.log"
    ;;
  run)
    exec "$FASTSIM_COLLECTION_PYTHON" scripts/collect_uarch_formal_only.py "$@"
    ;;
  status|stop)
    exec bash scripts/collect_uarch_c4_c8_offline.sh "$action" "$@"
    ;;
  *)
    echo "usage: $0 {start|run|status|stop} [collector options, e.g. --jobs N]" >&2
    exit 2
    ;;
esac
