#!/usr/bin/env bash
set -euo pipefail

THIS_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
TAO_ROOT="$( dirname "${THIS_DIR}" )"

PYTHON_BIN="${PYTHON_BIN:-/root/miniconda3/envs/yinhaolang/bin/python}"
CKPT_DIR="${CKPT_DIR:-${TAO_CKPT_ROOT}/v10_6_fetchsum_soft15_50m}"
DATA_ROOT="${DATA_ROOT:-${TAO_INFER_ROOT}/data}"
RAW_ROOT_W="${RAW_ROOT_W:-${TAO_ROOT}/datagen/tmp/exp_w11_w15_parallel_20260604_215144/runs}"
RAW_ROOT_H="${RAW_ROOT_H:-${TAO_ROOT}/tmp/holdout_mixed_raw_20260606_142227}"
RUN_ROOT="${RUN_ROOT:-${TAO_ROOT}/runs/v10_6_ckpt_quick_eval_$(date +%Y%m%d_%H%M%S)}"
ROWS_PER_CORE="${ROWS_PER_CORE:-25000}"
EVAL_WARMUP_ROWS_PER_CORE="${EVAL_WARMUP_ROWS_PER_CORE:-5000}"
NUM_CORES="${NUM_CORES:-4}"
BATCH="${BATCH:-1024}"
FETCH_GATE_MODE="${FETCH_GATE_MODE:-soft}"
FETCH_GATE_TEMP="${FETCH_GATE_TEMP:-1.5}"
REF_SIM_BACKEND="${REF_SIM_BACKEND:-timing-functional}"
W_WORKLOADS="${W_WORKLOADS:-W11_stream_mix W12_stencil2d W13_graph_walk W14_branch_state W15_indirect}"
H_WORKLOADS="${H_WORKLOADS:-H01_mixed_service H02_sharded_kv H03_analytics_scan}"
CKPTS="${CKPTS:-step34000 step36000 step38000 step40000 best}"

mkdir -p "${RUN_ROOT}"
echo "[quick-eval] run_root=${RUN_ROOT}"
echo "[quick-eval] ckpt_dir=${CKPT_DIR}"
echo "[quick-eval] ckpts=${CKPTS}"

for name in ${CKPTS}; do
  ckpt="${CKPT_DIR}/tao_v10_3_ma16.${name}.pt"
  if [[ "${name}" == "best" ]]; then
    ckpt="${CKPT_DIR}/tao_v10_3_ma16.best.pt"
  fi
  if [[ ! -f "${ckpt}" ]]; then
    echo "[quick-eval][WARN] skip missing ckpt: ${ckpt}" >&2
    continue
  fi

  out="${RUN_ROOT}/${name}"
  mkdir -p "${out}"
  echo "[quick-eval] ${name}: ${ckpt}"

  RAW_ROOT="${RAW_ROOT_W}" DATA_ROOT="${DATA_ROOT}" RUN_ROOT="${out}/w11_w15" \
  CKPT="${ckpt}" ROWS_PER_CORE="${ROWS_PER_CORE}" NUM_CORES="${NUM_CORES}" \
  EVAL_WARMUP_ROWS_PER_CORE="${EVAL_WARMUP_ROWS_PER_CORE}" BATCH="${BATCH}" \
  FETCH_GATE_MODE="${FETCH_GATE_MODE}" FETCH_GATE_TEMP="${FETCH_GATE_TEMP}" \
  REF_SIM_BACKEND="${REF_SIM_BACKEND}" TAO_INFER_DEVICE="${TAO_INFER_DEVICE:-cuda}" \
    bash "${THIS_DIR}/07_validate_w11_w15_100k.sh" --workloads "${W_WORKLOADS}"

  RAW_ROOT="${RAW_ROOT_H}" DATA_ROOT="${DATA_ROOT}" RUN_ROOT="${out}/holdout" \
  CKPT="${ckpt}" ROWS_PER_CORE="${ROWS_PER_CORE}" NUM_CORES="${NUM_CORES}" \
  EVAL_WARMUP_ROWS_PER_CORE="${EVAL_WARMUP_ROWS_PER_CORE}" BATCH="${BATCH}" \
  FETCH_GATE_MODE="${FETCH_GATE_MODE}" FETCH_GATE_TEMP="${FETCH_GATE_TEMP}" \
  REF_SIM_BACKEND="${REF_SIM_BACKEND}" TAO_INFER_DEVICE="${TAO_INFER_DEVICE:-cuda}" \
    bash "${THIS_DIR}/09_validate_holdout_mixed_100k.sh" --skip-gem5 --workloads "${H_WORKLOADS}"
done

"${PYTHON_BIN}" - "${RUN_ROOT}" <<'PY'
import csv
import json
import statistics
import sys
from pathlib import Path

root = Path(sys.argv[1])
rows = []
for ckpt_dir in sorted(p for p in root.iterdir() if p.is_dir()):
    all_rows = []
    for sub in ("w11_w15", "holdout"):
        p = ckpt_dir / sub / "summary.json"
        if not p.is_file():
            continue
        data = json.loads(p.read_text())
        all_rows.extend(data if isinstance(data, list) else data.get("rows", []))
    if not all_rows:
        continue
    abs_cpi = [abs(float(r["cpi_err_pct"])) for r in all_rows if r.get("cpi_err_pct") is not None]
    rows.append({
        "ckpt": ckpt_dir.name,
        "n": len(all_rows),
        "mean_abs_cpi_err_pct": sum(abs_cpi) / len(abs_cpi),
        "median_abs_cpi_err_pct": statistics.median(abs_cpi),
        "mean_rows_per_s": sum(float(r["rows_per_s"]) for r in all_rows) / len(all_rows),
    })

out = root / "quick_eval_summary.tsv"
with out.open("w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["ckpt", "n", "mean_abs_cpi_err_pct", "median_abs_cpi_err_pct", "mean_rows_per_s"], delimiter="\t")
    w.writeheader()
    w.writerows(rows)
print(f"[quick-eval] summary={out}")
PY
