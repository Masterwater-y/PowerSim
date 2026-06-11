#!/usr/bin/env bash
set -euo pipefail

THIS_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
source "${THIS_DIR}/env.sh"

PYTHON_BIN="${PYTHON_BIN:-/root/miniconda3/envs/yinhaolang/bin/python}"
RAW_ROOT="${RAW_ROOT:-}"
DATA_ROOT="${DATA_ROOT:-${TAO_ROOT}/infer/data}"
RUN_ROOT="${RUN_ROOT:-${TAO_ROOT}/runs/validate_datasets_$(date +%Y%m%d_%H%M%S)}"
CKPT="${CKPT:-${TAO_CKPT_ROOT}/iteration_best/v10_3_fetchdecomp.best.pt}"
NUM_CORES="${NUM_CORES:-4}"
ROWS_PER_CORE="${ROWS_PER_CORE:-100000}"
EVAL_WARMUP_ROWS_PER_CORE="${EVAL_WARMUP_ROWS_PER_CORE:-20000}"
REFSIM_WARMUP_ROWS_PER_CORE="${REFSIM_WARMUP_ROWS_PER_CORE:-${EVAL_WARMUP_ROWS_PER_CORE}}"
QUANTUM_CYCLES="${QUANTUM_CYCLES:-4096}"
K_MAX="${K_MAX:-128}"
BATCH="${BATCH:-1024}"
FETCH_GATE_MODE="${FETCH_GATE_MODE:-soft}"
FETCH_GATE_TEMP="${FETCH_GATE_TEMP:-1.5}"
REF_SIM_BACKEND="${REF_SIM_BACKEND:-timing-functional}"
INFER_DEVICE="${TAO_INFER_DEVICE:-}"
FORCE=0
DRY_RUN=0
LOG_PREFIX="${LOG_PREFIX:-validate-datasets}"

if [[ -n "${WORKLOADS:-}" ]]; then
  IFS=' ' read -r -a WORKLOAD_ARR <<< "${WORKLOADS}"
else
  WORKLOAD_ARR=()
fi

usage() {
  cat <<EOF
usage: $0 --workloads "W1 W2 ..." [options]

Common validation pipeline:
  existing dataset or raw trace -> functional/labels parquet -> cut baseline
  -> inference_driver.py -> _driver_eval_against_trace.py -> summary.tsv/json.

Options:
  --raw-root <dir>              raw gem5 run root, optional if datasets already exist
  --data-root <dir>             dataset root, default: ${DATA_ROOT}
  --run-root <dir>              output run root
  --ckpt <pt>                   model checkpoint
  --num-cores <n>               default: ${NUM_CORES}
  --rows-per-core <n>           default: ${ROWS_PER_CORE}
  --eval-warmup-rows-per-core <n> default: ${EVAL_WARMUP_ROWS_PER_CORE}
  --refsim-warmup-rows-per-core <n> default: ${REFSIM_WARMUP_ROWS_PER_CORE}
  --quantum-cycles <n>          default: ${QUANTUM_CYCLES}
  --k-max <n>                   default: ${K_MAX}
  --batch <n>                   default: ${BATCH}
  --fetch-gate-mode <mode>      hard|soft|direct, default: ${FETCH_GATE_MODE}
  --fetch-gate-temp <t>         default: ${FETCH_GATE_TEMP}
  --infer-device <cpu|cuda>     if cuda is requested and unavailable, fail
  --ref-sim-backend <backend>   timing-functional|coordinator|legacy
  --workloads "A B C"           workload names
  --force                       remove matching dataset/run dirs before running
  --dry-run                     print resolved paths and exit without inference
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --raw-root) RAW_ROOT="$2"; shift 2 ;;
    --data-root) DATA_ROOT="$2"; shift 2 ;;
    --run-root) RUN_ROOT="$2"; shift 2 ;;
    --ckpt) CKPT="$2"; shift 2 ;;
    --num-cores) NUM_CORES="$2"; shift 2 ;;
    --rows-per-core) ROWS_PER_CORE="$2"; shift 2 ;;
    --eval-warmup-rows-per-core) EVAL_WARMUP_ROWS_PER_CORE="$2"; shift 2 ;;
    --refsim-warmup-rows-per-core) REFSIM_WARMUP_ROWS_PER_CORE="$2"; shift 2 ;;
    --quantum-cycles) QUANTUM_CYCLES="$2"; shift 2 ;;
    --k-max) K_MAX="$2"; shift 2 ;;
    --batch) BATCH="$2"; shift 2 ;;
    --fetch-gate-mode) FETCH_GATE_MODE="$2"; shift 2 ;;
    --fetch-gate-temp) FETCH_GATE_TEMP="$2"; shift 2 ;;
    --ref-sim-backend) REF_SIM_BACKEND="$2"; shift 2 ;;
    --infer-device) INFER_DEVICE="$2"; shift 2 ;;
    --workloads) IFS=' ' read -r -a WORKLOAD_ARR <<< "$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "[${LOG_PREFIX}][FATAL] unknown arg: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ "${#WORKLOAD_ARR[@]}" -eq 0 ]]; then
  echo "[${LOG_PREFIX}][FATAL] no workloads specified" >&2
  usage
  exit 2
fi

SLICE_PY="${TAO_ROOT}/scripts/_slice_trace_prefix.py"
EXTRACT_PY="${TAO_ROOT}/infer/functional_trace/extract_from_records.py"
DRIVER_PY="${TAO_ROOT}/infer/driver/inference_driver.py"
EVAL_PY="${TAO_ROOT}/scripts/_driver_eval_against_trace.py"
CUT_BASE_PY="${TAO_ROOT}/scripts/_build_cut_baseline.py"

for p in "${PYTHON_BIN}" "${SLICE_PY}" "${EXTRACT_PY}" "${DRIVER_PY}" "${EVAL_PY}" "${CUT_BASE_PY}" "${CKPT}"; do
  if [[ ! -e "${p}" ]]; then
    echo "[${LOG_PREFIX}][FATAL] missing required path: ${p}" >&2
    exit 2
  fi
done

CUDA_OK=0
if "${PYTHON_BIN}" - <<'PY' >/dev/null 2>&1
import torch
raise SystemExit(0 if torch.cuda.is_available() else 1)
PY
then
  CUDA_OK=1
fi

if [[ -z "${INFER_DEVICE}" ]]; then
  if [[ "${CUDA_OK}" -eq 1 ]]; then
    INFER_DEVICE="cuda"
  else
    INFER_DEVICE="cpu"
  fi
fi
if [[ "${INFER_DEVICE}" == "cuda" && "${CUDA_OK}" -ne 1 ]]; then
  echo "[${LOG_PREFIX}][FATAL] TAO_INFER_DEVICE=cuda requested but CUDA is unavailable" >&2
  exit 2
fi
export TAO_INFER_DEVICE="${INFER_DEVICE}"

mkdir -p "${DATA_ROOT}" "${RUN_ROOT}"

echo "[${LOG_PREFIX}] workloads=${WORKLOAD_ARR[*]}"
echo "[${LOG_PREFIX}] data_root=${DATA_ROOT}"
echo "[${LOG_PREFIX}] raw_root=${RAW_ROOT:-<optional>}"
echo "[${LOG_PREFIX}] run_root=${RUN_ROOT}"
echo "[${LOG_PREFIX}] ckpt=${CKPT}"
echo "[${LOG_PREFIX}] infer_device=${TAO_INFER_DEVICE}"
echo "[${LOG_PREFIX}] fetch_gate=${FETCH_GATE_MODE} temp=${FETCH_GATE_TEMP}"
echo "[${LOG_PREFIX}] eval_warmup_rows_per_core=${EVAL_WARMUP_ROWS_PER_CORE}"
echo "[${LOG_PREFIX}] refsim_warmup_rows_per_core=${REFSIM_WARMUP_ROWS_PER_CORE}"

run_one() {
  local wl="$1"
  local raw_dir="${RAW_ROOT:+${RAW_ROOT}/${wl}}"
  local tag="${wl}_${NUM_CORES}c_u${ROWS_PER_CORE}"
  local dataset_dir="${DATA_ROOT}/${tag}"
  local functional_dir="${dataset_dir}/functional_parquet"
  local labels_dir="${dataset_dir}/labels_parquet"
  local run_dir="${RUN_ROOT}/${wl}"

  if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "[${LOG_PREFIX}][dry-run] ${wl}: dataset=${dataset_dir} raw=${raw_dir:-<none>} run=${run_dir}"
    return
  fi

  if [[ "${FORCE}" -eq 1 ]]; then
    rm -rf "${dataset_dir}" "${run_dir}"
  fi

  if [[ ! -f "${dataset_dir}/slice_summary.json" && ! -f "${functional_dir}/manifest.json" ]]; then
    if [[ -z "${raw_dir}" || ! -d "${raw_dir}/tao_trace" ]]; then
      echo "[${LOG_PREFIX}][FATAL] dataset missing and raw run missing tao_trace: ${dataset_dir} raw=${raw_dir:-<none>}" >&2
      exit 2
    fi
    echo "[${LOG_PREFIX}] slicing ${wl} -> ${dataset_dir}"
    "${PYTHON_BIN}" "${SLICE_PY}" \
      --in-run-dir "${raw_dir}" \
      --out-run-dir "${dataset_dir}" \
      --max-records-per-core "${ROWS_PER_CORE}"
  else
    echo "[${LOG_PREFIX}] reuse sliced/existing dataset ${dataset_dir}"
  fi

  if [[ ! -f "${functional_dir}/manifest.json" ]]; then
    if [[ ! -d "${dataset_dir}/tao_trace" ]]; then
      echo "[${LOG_PREFIX}][FATAL] missing tao_trace for extraction: ${dataset_dir}" >&2
      exit 2
    fi
    echo "[${LOG_PREFIX}] extracting functional/labels for ${wl}"
    "${PYTHON_BIN}" "${EXTRACT_PY}" \
      --trace-dir "${dataset_dir}/tao_trace" \
      --out-dir "${functional_dir}" \
      --labels-trace-dir "${dataset_dir}/tao_trace" \
      --labels-out-dir "${labels_dir}" \
      --format parquet
  else
    echo "[${LOG_PREFIX}] reuse functional/labels ${functional_dir}"
  fi

  if [[ ! -f "${dataset_dir}/cut_baseline.json" ]]; then
    echo "[${LOG_PREFIX}] build cut baseline ${wl}"
    "${PYTHON_BIN}" "${CUT_BASE_PY}" \
      --dataset-dir "${dataset_dir}" \
      --out "${dataset_dir}/cut_baseline.json" \
      > "${dataset_dir}/cut_baseline.build.log"
  else
    echo "[${LOG_PREFIX}] reuse cut baseline ${dataset_dir}/cut_baseline.json"
  fi

  mkdir -p "${run_dir}"
  echo "[${LOG_PREFIX}] driver ${wl}"
  /usr/bin/time -f 'elapsed=%e' -o "${run_dir}/time.txt" \
    "${PYTHON_BIN}" "${DRIVER_PY}" \
      --functional-dir "${functional_dir}" \
      --uarch-profile "${dataset_dir}/uarch_profile.json" \
      --ref-sim-module-dir "${TAO_REF_SIM_BUILD_DIR}" \
      --ref-sim-backend "${REF_SIM_BACKEND}" \
      --stats "${dataset_dir}/stats.txt" \
      --ckpt "${CKPT}" \
      --out-jsonl "${run_dir}/infer.jsonl" \
      --report-json "${run_dir}/report.json" \
      --quantum-cycles "${QUANTUM_CYCLES}" \
      --k-max "${K_MAX}" \
      --model-batch-size "${BATCH}" \
      --fetch-gate-mode "${FETCH_GATE_MODE}" \
      --fetch-gate-temp "${FETCH_GATE_TEMP}" \
      --refsim-warmup-records-per-core "${REFSIM_WARMUP_ROWS_PER_CORE}" \
    > "${run_dir}/stdout.log" 2> "${run_dir}/stderr.log"

  echo "[${LOG_PREFIX}] evaluate ${wl}"
  "${PYTHON_BIN}" "${EVAL_PY}" \
    --run-dir "${run_dir}" \
    --dataset-dir "${dataset_dir}" \
    --eval-warmup-records-per-core "${EVAL_WARMUP_ROWS_PER_CORE}" \
    --summary-out "${run_dir}/driver_validation_summary.json" \
    > "${run_dir}/eval.log"
}

for wl in "${WORKLOAD_ARR[@]}"; do
  run_one "${wl}"
done

if [[ "${DRY_RUN}" -eq 1 ]]; then
  echo "[${LOG_PREFIX}] dry-run done"
  exit 0
fi

SUMMARY_TSV="${RUN_ROOT}/summary.tsv"
SUMMARY_JSON="${RUN_ROOT}/summary.json"

"${PYTHON_BIN}" - "${RUN_ROOT}" "${SUMMARY_TSV}" "${SUMMARY_JSON}" "${WORKLOAD_ARR[@]}" <<'PY'
import json
import sys
from pathlib import Path

run_root = Path(sys.argv[1])
summary_tsv = Path(sys.argv[2])
summary_json = Path(sys.argv[3])
workloads = sys.argv[4:]

rows = []
for wl in workloads:
    path = run_root / wl / "driver_validation_summary.json"
    if not path.is_file():
        continue
    data = json.loads(path.read_text())
    pmu = data.get("driver_pmu_eval", {})
    pm = pmu.get("metrics", {})
    thr = data.get("throughput", {})
    eva = data.get("aligned_truth_eval", {})
    diag = data.get("per_core_diagnostics", {})
    worst_core = None
    worst = {}
    if diag:
        worst_core, worst = max(
            diag.items(),
            key=lambda kv: abs(kv[1].get("cycle", {}).get("deficit") or 0.0),
        )
    rows.append({
        "workload": wl,
        "rows": thr.get("rows"),
        "wall_s": thr.get("wall_s"),
        "rows_per_s": thr.get("rows_per_s"),
        "cpi_pred": eva.get("cpi_pred_sumsum"),
        "cpi_truth": eva.get("cpi_truth_sumsum"),
        "cpi_err_pct": eva.get("cpi_err_pct"),
        "branch_pred": eva.get("branch_mispred_pred"),
        "branch_truth": eva.get("branch_mispred_truth"),
        "branch_err_pct": eva.get("branch_mispred_err_pct"),
        "precision": eva.get("precision"),
        "recall": eva.get("recall"),
        "pmu_matched": pmu.get("bit_exact_metrics", {}).get("matched"),
        "pmu_total": pmu.get("bit_exact_metrics", {}).get("total"),
        "l1d_load_miss_err_pct": pm.get("l1d.load_misses", {}).get("err_pct"),
        "l1d_store_miss_err_pct": pm.get("l1d.store_misses", {}).get("err_pct"),
        "l2_miss_err_pct": pm.get("l2.misses", {}).get("err_pct"),
        "llc_load_miss_err_pct": pm.get("llc.load_misses", {}).get("err_pct"),
        "llc_store_miss_err_pct": pm.get("llc.store_misses", {}).get("err_pct"),
        "cha_reads_err_pct": pm.get("cha.requests.reads", {}).get("err_pct"),
        "cha_writes_err_pct": pm.get("cha.requests.writes", {}).get("err_pct"),
        "cha_tor_drd_err_pct": pm.get("cha.tor_inserts.ia_miss_drd", {}).get("err_pct"),
        "cha_dir_snp_err_pct": pm.get("cha.dir_lookup.snp", {}).get("err_pct"),
        "worst_core": worst_core,
        "worst_core_cycle_deficit": worst.get("cycle", {}).get("deficit"),
        "worst_core_cycle_err_pct": worst.get("cycle", {}).get("err_pct"),
        "worst_core_fetch_deficit": worst.get("fetch", {}).get("deficit"),
        "worst_core_fetch_err_pct": worst.get("fetch", {}).get("err_pct"),
        "worst_core_exec_deficit": worst.get("exec", {}).get("deficit"),
        "worst_core_exec_err_pct": worst.get("exec", {}).get("err_pct"),
        "worst_core_exec_ready_tail_deficit": worst.get("exec", {}).get("ready_tail_deficit"),
        "worst_core_exec_ready_tail_err_pct": worst.get("exec", {}).get("ready_tail_err_pct"),
    })

cols = [
    "workload", "rows", "wall_s", "rows_per_s",
    "cpi_pred", "cpi_truth", "cpi_err_pct",
    "branch_pred", "branch_truth", "branch_err_pct",
    "precision", "recall", "pmu_matched", "pmu_total",
    "l1d_load_miss_err_pct", "l1d_store_miss_err_pct",
    "l2_miss_err_pct", "llc_load_miss_err_pct", "llc_store_miss_err_pct",
    "cha_reads_err_pct", "cha_writes_err_pct", "cha_tor_drd_err_pct", "cha_dir_snp_err_pct",
    "worst_core", "worst_core_cycle_deficit", "worst_core_cycle_err_pct",
    "worst_core_fetch_deficit", "worst_core_fetch_err_pct",
    "worst_core_exec_deficit", "worst_core_exec_err_pct",
    "worst_core_exec_ready_tail_deficit", "worst_core_exec_ready_tail_err_pct",
]
with summary_tsv.open("w") as f:
    f.write("\t".join(cols) + "\n")
    for r in rows:
        f.write("\t".join("" if r.get(c) is None else str(r.get(c)) for c in cols) + "\n")
summary_json.write_text(json.dumps(rows, indent=2, sort_keys=True))
print(f"summary_tsv={summary_tsv}")
print(f"summary_json={summary_json}")
PY

echo "[${LOG_PREFIX}] done -> ${RUN_ROOT}"
