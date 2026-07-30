#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT=${ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
cd "$PROJECT_ROOT"

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
CKPT=${CKPT:-$PROJECT_ROOT/ckpt/tcsim_v30_gss_joint_v2_60k_seed1234/last.pt}
MANIFEST=${MANIFEST:-$PROJECT_ROOT/data/v30_gss_ready_dataset/manifest.json}

if ! "$PY" -c 'import os; import tcsim.v30._gss_native as m; raise SystemExit(os.path.getmtime(m.__file__) < os.path.getmtime("tcsim/v30/native_gss.cpp"))' >/dev/null 2>&1; then
  "$PY" scripts/build_v30_gss_native.py
fi

export TCSIM_GSS_BACKEND=${TCSIM_GSS_BACKEND:-native}
export CKPT MANIFEST
export SPLITS=${SPLITS:-deployment_inference,development_heldout}
export MODES=${MODES:-speculative}
export WINDOW_SHIFT=${WINDOW_SHIFT:-64}
export WINDOW_CONTEXT_BACKEND=${WINDOW_CONTEXT_BACKEND:-process}
export ALLOW_READY_CLOCK_GSS_COMPAT=${ALLOW_READY_CLOCK_GSS_COMPAT:-1}

exec bash scripts/run_v29_window_parallel_4gpu.sh
