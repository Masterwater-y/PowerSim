#!/usr/bin/env bash
# Offline entrypoint; the default action only prints usage.
set -euo pipefail
FASTSIM_COLLECTION_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
FASTSIM_COLLECTION_PYTHON="${FASTSIM_COLLECTION_PYTHON:-/data00/yinhaolang/infer/.venv/bin/python}"
if [[ ! -x "$FASTSIM_COLLECTION_PYTHON" ]]; then
    FASTSIM_COLLECTION_PYTHON="$(command -v python3)"
fi
"$FASTSIM_COLLECTION_PYTHON" -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ required; set FASTSIM_COLLECTION_PYTHON"'
cd -- "$FASTSIM_COLLECTION_ROOT"
if [[ $# -eq 0 ]]; then
    set -- --help
fi
exec "$FASTSIM_COLLECTION_PYTHON" tools/collect_uarch_common_end.py "$@"
