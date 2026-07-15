#!/usr/bin/env bash
# One command: prepare seed1 packed traces if necessary, then evaluate them.
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
MANIFEST=${MANIFEST:-$ROOT/data/v28_business_a1_sharedzipf_dataset/manifest.json}
cd "$ROOT"

ready=0
if [[ -f "$MANIFEST" ]]; then
  ready=$("$PY" - "$MANIFEST" <<'PY'
import json, os, sys
path = sys.argv[1]
manifest = json.load(open(path, "r", encoding="utf-8"))
rows = manifest.get("splits", {}).get("deployment_inference", [])
ok = len(rows) == 80
for row in rows:
    out = row.get("rollout_dir") if isinstance(row, dict) else row
    if not out:
        ok = False
        break
    if not os.path.isabs(out):
        out = os.path.join(os.path.dirname(os.path.abspath(path)), out)
    if not os.path.isfile(os.path.join(out, "meta.json")):
        ok = False
        break
print(1 if ok else 0)
PY
  )
fi

if [[ "$ready" != "1" ]]; then
  bash scripts/prepare_v28_seed1_deployment_cache.sh
fi
bash scripts/run_v28_seed1_deployment_eval.sh
