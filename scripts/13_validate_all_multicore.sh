#!/usr/bin/env bash
set -euo pipefail

THIS_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
TAO_ROOT="$( dirname "${THIS_DIR}" )"

PYTHON_BIN="${PYTHON_BIN:-/root/miniconda3/envs/yinhaolang/bin/python}"
NUM_CORES="${NUM_CORES:-8}"
ROWS_PER_CORE="${ROWS_PER_CORE:-100000}"
EVAL_WARMUP_ROWS_PER_CORE="${EVAL_WARMUP_ROWS_PER_CORE:-20000}"
CKPT="${CKPT:-${TAO_CKPT_ROOT}/iteration_best/v10_3_fetchdecomp.best.pt}"
RUN_ROOT="${RUN_ROOT:-${TAO_ROOT}/runs/v10_3_fetchdecomp_soft15_${NUM_CORES}c_all_eval_$(date +%Y%m%d_%H%M%S)}"
W_RAW_ROOT="${W_RAW_ROOT:-${TAO_ROOT}/datagen/tmp/w11_w15_${NUM_CORES}c_raw_current/runs}"
H_RAW_ROOT="${H_RAW_ROOT:-${TAO_ROOT}/tmp/holdout_mixed_${NUM_CORES}c_raw_current}"
DATA_ROOT="${DATA_ROOT:-${TAO_INFER_ROOT}/data}"
BATCH="${BATCH:-1024}"
FETCH_GATE_MODE="${FETCH_GATE_MODE:-soft}"
FETCH_GATE_TEMP="${FETCH_GATE_TEMP:-1.5}"
REF_SIM_BACKEND="${REF_SIM_BACKEND:-timing-functional}"
TAO_INFER_DEVICE="${TAO_INFER_DEVICE:-cuda}"

mkdir -p "${RUN_ROOT}"

echo "[validate-all-mc] run_root=${RUN_ROOT}"
echo "[validate-all-mc] num_cores=${NUM_CORES}"
echo "[validate-all-mc] rows_per_core=${ROWS_PER_CORE}"
echo "[validate-all-mc] ckpt=${CKPT}"
echo "[validate-all-mc] w_raw_root=${W_RAW_ROOT}"
echo "[validate-all-mc] h_raw_root=${H_RAW_ROOT}"

NUM_CORES="${NUM_CORES}" RAW_ROOT="${W_RAW_ROOT}" \
  bash "${THIS_DIR}/12_build_w11_w15_multicore_raw.sh"

CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}" \
TAO_INFER_DEVICE="${TAO_INFER_DEVICE}" \
NUM_CORES="${NUM_CORES}" RAW_ROOT="${W_RAW_ROOT}" DATA_ROOT="${DATA_ROOT}" \
RUN_ROOT="${RUN_ROOT}/w11_w15" CKPT="${CKPT}" \
ROWS_PER_CORE="${ROWS_PER_CORE}" EVAL_WARMUP_ROWS_PER_CORE="${EVAL_WARMUP_ROWS_PER_CORE}" \
BATCH="${BATCH}" FETCH_GATE_MODE="${FETCH_GATE_MODE}" FETCH_GATE_TEMP="${FETCH_GATE_TEMP}" \
REF_SIM_BACKEND="${REF_SIM_BACKEND}" \
  bash "${THIS_DIR}/07_validate_w11_w15_100k.sh"

CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}" \
TAO_INFER_DEVICE="${TAO_INFER_DEVICE}" \
NUM_CORES="${NUM_CORES}" RAW_ROOT="${H_RAW_ROOT}" DATA_ROOT="${DATA_ROOT}" \
RUN_ROOT="${RUN_ROOT}/holdout" CKPT="${CKPT}" \
ROWS_PER_CORE="${ROWS_PER_CORE}" EVAL_WARMUP_ROWS_PER_CORE="${EVAL_WARMUP_ROWS_PER_CORE}" \
BATCH="${BATCH}" FETCH_GATE_MODE="${FETCH_GATE_MODE}" FETCH_GATE_TEMP="${FETCH_GATE_TEMP}" \
REF_SIM_BACKEND="${REF_SIM_BACKEND}" \
  bash "${THIS_DIR}/09_validate_holdout_mixed_100k.sh"

{
  cat "${RUN_ROOT}/w11_w15/summary.tsv"
  tail -n +2 "${RUN_ROOT}/holdout/summary.tsv"
} > "${RUN_ROOT}/summary_all_raw.tsv"

"${PYTHON_BIN}" - "${RUN_ROOT}" <<'PY'
import json
import sys
from pathlib import Path

base = Path(sys.argv[1])
rows = []
for sub in ("w11_w15", "holdout"):
    p = base / sub / "summary.json"
    if not p.is_file():
        continue
    data = json.loads(p.read_text())
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

summary = {
    "rows": rows,
    "mean_abs_cpi_err_pct": (
        sum(abs(float(r["cpi_err_pct"])) for r in rows if r.get("cpi_err_pct") is not None)
        / max(1, sum(1 for r in rows if r.get("cpi_err_pct") is not None))
    ),
    "mean_rows_per_s": (
        sum(float(r["rows_per_s"]) for r in rows if r.get("rows_per_s") is not None)
        / max(1, sum(1 for r in rows if r.get("rows_per_s") is not None))
    ),
}
(base / "summary_all.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
PY

echo "[validate-all-mc] summary=${RUN_ROOT}/summary_all_common.tsv"
echo "[validate-all-mc] raw_summary=${RUN_ROOT}/summary_all_raw.tsv"
echo "[validate-all-mc] json=${RUN_ROOT}/summary_all.json"
