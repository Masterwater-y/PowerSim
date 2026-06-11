#!/usr/bin/env bash
set -euo pipefail

THIS_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
source "${THIS_DIR}/env.sh"

PYTHON_BIN="${PYTHON_BIN:-/root/miniconda3/envs/yinhaolang/bin/python}"
RAW_ROOT="${RAW_ROOT:-${TAO_ROOT}/datagen/tmp/exp_w11_w15_parallel_20260604_215144/runs}"
DATA_ROOT="${DATA_ROOT:-${TAO_ROOT}/infer/data}"
RUN_ROOT="${RUN_ROOT:-${TAO_ROOT}/runs/w11_w15_smoke_$(date +%Y%m%d_%H%M%S)}"
ROWS_PER_CORE="${ROWS_PER_CORE:-1000}"
FORCE=0

WORKLOADS=(
  W11_stream_mix
  W12_stencil2d
  W13_graph_walk
  W14_branch_state
  W15_indirect
)

usage() {
  cat <<EOF
usage: $0 [--raw-root <dir>] [--data-root <dir>] [--run-root <dir>]
          [--rows-per-core <n>]
          [--workloads "W11_stream_mix W12_stencil2d ..."]
          [--force]

说明：
  1. 用小样本（默认每核 1000 条）对 W11-W15 五个负载做 smoke。
  2. 只验证数据采集流程：slice -> functional/labels parquet -> cut_baseline。
  3. 不跑 driver 推理；只用于判断五个负载能否正常采集并生成验证数据。
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --raw-root) RAW_ROOT="$2"; shift 2 ;;
    --data-root) DATA_ROOT="$2"; shift 2 ;;
    --run-root) RUN_ROOT="$2"; shift 2 ;;
    --rows-per-core) ROWS_PER_CORE="$2"; shift 2 ;;
    --workloads) IFS=' ' read -r -a WORKLOADS <<< "$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 2 ;;
  esac
done

SLICE_PY="${TAO_ROOT}/scripts/_slice_trace_prefix.py"
EXTRACT_PY="${TAO_ROOT}/infer/functional_trace/extract_from_records.py"
CUT_BASE_PY="${TAO_ROOT}/scripts/_build_cut_baseline.py"

for p in "${SLICE_PY}" "${EXTRACT_PY}" "${CUT_BASE_PY}"; do
  if [[ ! -e "${p}" ]]; then
    echo "[smoke][FATAL] missing required path: ${p}" >&2
    exit 2
  fi
done

mkdir -p "${RUN_ROOT}"
echo "[smoke] run_root=${RUN_ROOT}"
echo "[smoke] rows_per_core=${ROWS_PER_CORE}"

run_one() {
  local wl="$1"
  local raw_dir="${RAW_ROOT}/${wl}"
  local tag="${wl}_4c_u${ROWS_PER_CORE}"
  local dataset_dir="${DATA_ROOT}/${tag}"
  local functional_dir="${dataset_dir}/functional_parquet"
  local labels_dir="${dataset_dir}/labels_parquet"

  if [[ ! -d "${raw_dir}/tao_trace" ]]; then
    echo "[smoke][FATAL] raw run missing tao_trace: ${raw_dir}" >&2
    exit 2
  fi

  if [[ "${FORCE}" -eq 1 ]]; then
    rm -rf "${dataset_dir}"
  fi

  if [[ ! -f "${dataset_dir}/slice_summary.json" ]]; then
    echo "[smoke] slicing ${wl} -> ${dataset_dir}"
    "${PYTHON_BIN}" "${SLICE_PY}" \
      --in-run-dir "${raw_dir}" \
      --out-run-dir "${dataset_dir}" \
      --max-records-per-core "${ROWS_PER_CORE}" \
      > "${RUN_ROOT}/${wl}.slice.log"
  else
    echo "[smoke] reuse sliced dataset ${dataset_dir}"
  fi

  if [[ ! -f "${functional_dir}/manifest.json" ]]; then
    echo "[smoke] extracting functional/labels for ${wl}"
    "${PYTHON_BIN}" "${EXTRACT_PY}" \
      --trace-dir "${dataset_dir}/tao_trace" \
      --out-dir "${functional_dir}" \
      --labels-trace-dir "${dataset_dir}/tao_trace" \
      --labels-out-dir "${labels_dir}" \
      --format parquet \
      > "${RUN_ROOT}/${wl}.extract.log"
  else
    echo "[smoke] reuse functional/labels ${functional_dir}"
  fi

  if [[ ! -f "${dataset_dir}/cut_baseline.json" ]]; then
    echo "[smoke] build cut baseline ${wl}"
    "${PYTHON_BIN}" "${CUT_BASE_PY}" \
      --dataset-dir "${dataset_dir}" \
      --out "${dataset_dir}/cut_baseline.json" \
      > "${RUN_ROOT}/${wl}.baseline.log"
  else
    echo "[smoke] reuse cut baseline ${dataset_dir}/cut_baseline.json"
  fi
}

for wl in "${WORKLOADS[@]}"; do
  run_one "${wl}"
done

SUMMARY_TSV="${RUN_ROOT}/summary.tsv"
SUMMARY_JSON="${RUN_ROOT}/summary.json"

"${PYTHON_BIN}" - "${DATA_ROOT}" "${ROWS_PER_CORE}" "${SUMMARY_TSV}" "${SUMMARY_JSON}" "${WORKLOADS[@]}" <<'PY'
import json
import sys
from pathlib import Path

data_root = Path(sys.argv[1])
rows_per_core = int(sys.argv[2])
summary_tsv = Path(sys.argv[3])
summary_json = Path(sys.argv[4])
workloads = sys.argv[5:]

rows = []
for wl in workloads:
    dataset_dir = data_root / f"{wl}_4c_u{rows_per_core}"
    manifest_path = dataset_dir / "functional_parquet" / "manifest.json"
    baseline_path = dataset_dir / "cut_baseline.json"
    slice_path = dataset_dir / "slice_summary.json"
    if not (manifest_path.is_file() and baseline_path.is_file() and slice_path.is_file()):
        rows.append({
            "workload": wl,
            "dataset_dir": str(dataset_dir),
            "ok": False,
            "reason": "missing_artifact",
        })
        continue
    manifest = json.loads(manifest_path.read_text())
    baseline = json.loads(baseline_path.read_text())
    slice_summary = json.loads(slice_path.read_text())
    rows.append({
        "workload": wl,
        "dataset_dir": str(dataset_dir),
        "ok": True,
        "records_rows": baseline["aggregate"].get("records_rows"),
        "labels_rows": baseline["aggregate"].get("labels_rows"),
        "macro_count": baseline["aggregate"].get("macro_count"),
        "branch_committed": baseline["aggregate"].get("branch_committed"),
        "branch_mispred": baseline["aggregate"].get("branch_mispred"),
        "functional_total_rows": manifest.get("total_rows"),
        "cores": len(manifest.get("files", [])),
        "merged_mem_events_rows": slice_summary.get("merged_mem_events_rows"),
    })

with open(summary_tsv, "w") as f:
    f.write("workload\tok\tfunctional_total_rows\trecords_rows\tlabels_rows\tmacro_count\tbranch_committed\tbranch_mispred\tcores\tmerged_mem_events_rows\tdataset_dir\n")
    for r in rows:
        f.write(
            f"{r.get('workload')}\t{r.get('ok')}\t{r.get('functional_total_rows')}\t"
            f"{r.get('records_rows')}\t{r.get('labels_rows')}\t{r.get('macro_count')}\t"
            f"{r.get('branch_committed')}\t{r.get('branch_mispred')}\t"
            f"{r.get('cores')}\t{r.get('merged_mem_events_rows')}\t{r.get('dataset_dir')}\n"
        )

summary_json.write_text(json.dumps(rows, indent=2, sort_keys=True))
print(f"summary_tsv={summary_tsv}")
print(f"summary_json={summary_json}")
PY

echo "[smoke] summary:"
cat "${SUMMARY_TSV}"
echo "[smoke] done -> ${RUN_ROOT}"
