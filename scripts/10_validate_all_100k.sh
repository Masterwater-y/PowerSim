#!/usr/bin/env bash
set -euo pipefail

THIS_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
TAO_ROOT="$( dirname "${THIS_DIR}" )"

# Keep CUDA initialization clean for PyTorch inference.
unset LD_LIBRARY_PATH

CKPT="${CKPT:-${TAO_CKPT_ROOT}/iteration_best/v10_3_fetchdecomp.best.pt}"
DATA_ROOT="${DATA_ROOT:-${TAO_INFER_ROOT}/data}"
RAW_ROOT_HOLDOUT="${RAW_ROOT_HOLDOUT:-${TAO_ROOT}/tmp/holdout_mixed_raw_20260606_142227}"
RUN_ROOT="${RUN_ROOT:-${TAO_ROOT}/runs/v10_3_fetchdecomp_soft15_all_$(date +%Y%m%d_%H%M%S)}"
BASE_RUN_ROOT="${RUN_ROOT}"

export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}"
export TAO_INFER_DEVICE="${TAO_INFER_DEVICE:-cuda}"
export EVAL_WARMUP_ROWS_PER_CORE="${EVAL_WARMUP_ROWS_PER_CORE:-20000}"
export BATCH="${BATCH:-1024}"
export FETCH_GATE_MODE="${FETCH_GATE_MODE:-soft}"
export FETCH_GATE_TEMP="${FETCH_GATE_TEMP:-1.5}"
export REF_SIM_BACKEND="${REF_SIM_BACKEND:-timing-functional}"
export CKPT DATA_ROOT

mkdir -p "${RUN_ROOT}"

echo "[validate-all] run_root=${RUN_ROOT}"
echo "[validate-all] data_root=${DATA_ROOT}"
echo "[validate-all] ckpt=${CKPT}"

RUN_ROOT="${BASE_RUN_ROOT}/w11_w15" \
  bash "${THIS_DIR}/07_validate_w11_w15_100k.sh"

RAW_ROOT="${RAW_ROOT_HOLDOUT}" \
RUN_ROOT="${BASE_RUN_ROOT}/holdout" \
  bash "${THIS_DIR}/09_validate_holdout_mixed_100k.sh" --skip-gem5

{
  cat "${BASE_RUN_ROOT}/w11_w15/summary.tsv"
  tail -n +2 "${BASE_RUN_ROOT}/holdout/summary.tsv"
} > "${BASE_RUN_ROOT}/summary_all_raw.tsv"

"${PYTHON_BIN:-/root/miniconda3/envs/yinhaolang/bin/python}" - "${BASE_RUN_ROOT}" <<'PY'
import json
import sys
from pathlib import Path

base = Path(sys.argv[1])
rows = []
for sub in ("w11_w15", "holdout"):
    data = json.loads((base / sub / "summary.json").read_text())
    rows.extend(data if isinstance(data, list) else data.get("rows", []))

cols = [
    "workload", "rows", "wall_s", "rows_per_s",
    "cpi_pred", "cpi_truth", "cpi_err_pct",
    "precision", "recall",
    "worst_core", "worst_core_cycle_err_pct",
    "worst_core_fetch_err_pct", "worst_core_exec_err_pct",
]
out = base / "summary_all_common.tsv"
with out.open("w") as f:
    f.write("\t".join(cols) + "\n")
    for r in rows:
        f.write("\t".join("" if r.get(c) is None else str(r.get(c)) for c in cols) + "\n")
PY

echo "[validate-all] summary=${BASE_RUN_ROOT}/summary_all_common.tsv"
echo "[validate-all] raw_summary=${BASE_RUN_ROOT}/summary_all_raw.tsv"
