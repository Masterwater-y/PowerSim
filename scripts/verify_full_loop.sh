#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
MODE=smoke
OUT=""

usage() {
    cat <<'EOF'
Usage: scripts/verify_full_loop.sh [--smoke|--full] [--out DIR]

--smoke runs branch_dense only.
--full runs the validated 5-workload suite.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --smoke) MODE=smoke; shift ;;
        --full) MODE=full; shift ;;
        --out)
            [[ $# -ge 2 ]] || { echo "error: --out needs an argument" >&2; exit 1; }
            OUT="$2"
            shift 2
            ;;
        -h|--help) usage; exit 0 ;;
        *) echo "error: unknown argument: $1" >&2; usage >&2; exit 1 ;;
    esac
done

if [[ ! -f "$ROOT/env.sh" ]]; then
    echo "error: env.sh is missing; run scripts/bootstrap.sh first" >&2
    exit 1
fi

# shellcheck disable=SC1091
source "$ROOT/env.sh"
export DR_TIMEOUT_SECONDS="${DR_TIMEOUT_SECONDS:-300}"

if [[ "$MODE" == "full" ]]; then
    WORKLOADS="log_state,graph_walk,codec_pipeline,branch_dense,cache_bench"
    OUT=${OUT:-"$ROOT/global/out/verify_full_loop"}
else
    WORKLOADS="branch_dense"
    OUT=${OUT:-"$ROOT/global/out/verify_smoke"}
fi

"${PYTHON:-python3}" "$ROOT/global/scripts/run_single_core_workload_suite.py" \
    --workloads "$WORKLOADS" \
    --out "$OUT"

python3 - "$OUT" <<'PY'
import csv
import json
import math
import sys
from pathlib import Path

out = Path(sys.argv[1])
summary = json.loads((out / "suite_summary.json").read_text())
rows = summary.get("rows", [])

def mean_abs(name):
    vals = []
    key = f"{name}_relative_error_pct"
    for row in rows:
        value = row.get(key)
        if value is None:
            continue
        value = float(value)
        if math.isfinite(value):
            vals.append(abs(value))
    return None if not vals else sum(vals) / len(vals)

print("verification output:", out)
print("workloads:", ",".join(summary.get("workloads", {}).keys()))
print("mean_abs_rel_error_pct.minesim:", mean_abs("minesim"))
print("mean_abs_rel_error_pct.sniper:", mean_abs("sniper"))

for name, meta in summary.get("workloads", {}).items():
    cp = meta.get("minesim_counterpoint")
    if not cp:
        continue
    diag = Path(cp) / "diagnosis.json"
    if diag.exists():
        ranked = json.loads(diag.read_text()).get("ranked_components", [])
        print(f"counterpoint.top_components.{name}:", ranked[:5])
PY
