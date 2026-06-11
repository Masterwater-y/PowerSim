#!/usr/bin/env bash
# scripts/06_persistent_infer.sh
# P0 runner: 封装 inference_driver.py --persistent-session，
# 自动生成 job JSONL、输出路径与 session 日志，避免手动喂 stdin。
#
# 用法示例：
#   bash scripts/06_persistent_infer.sh \
#     --functional-dir infer/data/W12_stencil2d_4c_tiny100/functional_parquet \
#     --uarch-profile  infer/data/W12_stencil2d_4c_u20k/uarch_profile.json \
#     --ckpt /path/to/model.pt \
#     --repeat 2
#
#   bash scripts/06_persistent_infer.sh \
#     --job-list /tmp/functional_dirs.txt \
#     --uarch-profile infer/data/W12_stencil2d_4c_u20k/uarch_profile.json \
#     --ckpt /path/to/model.pt
#
# job-list 格式：
#   - 每行一个 functional_dir
#   - 或者：<tag><TAB><functional_dir>

set -euo pipefail

THIS_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
source "${THIS_DIR}/env.sh"

PYTHON_BIN="${PYTHON_BIN:-/root/miniconda3/envs/yinhaolang/bin/python}"
DRIVER_PY="${TAO_ROOT}/infer/driver/inference_driver.py"

CKPT=""
UARCH_PROFILE=""
REF_SIM_MODULE_DIR="${TAO_REF_SIM_BUILD_DIR}"
OUT_ROOT="${TAO_ROOT}/runs/persistent_infer_$(date +%Y%m%d_%H%M%S)"
QUANTUM_CYCLES="${TAO_QUANTUM_CYCLES}"
K_MAX="128"
BATCH="512"
REPEAT="1"
JOB_LIST=""
DRY_RUN=0

declare -a FUNCTIONAL_DIRS=()

usage() {
  cat <<EOF
usage: $0 --ckpt <model.pt> --uarch-profile <uarch_profile.json>
          [--functional-dir <dir>]...
          [--job-list <file>]
          [--out-root <dir>]
          [--quantum-cycles <n>]
          [--k-max <n>]
          [--batch <n>]
          [--repeat <n>]
          [--ref-sim-module-dir <dir>]
          [--dry-run]

说明：
  1. 至少提供一个 --functional-dir，或通过 --job-list 提供若干 functional_dir。
  2. runner 会自动生成：
       - <out-root>/jobs.jsonl
       - <out-root>/jobs.tsv
       - <out-root>/session.stdout.log
       - <out-root>/session.stderr.log
       - <out-root>/session.time.txt
       - <out-root>/<job>/infer.jsonl
       - <out-root>/<job>/report.json
  3. --repeat 可重复同一批 job，多用于 warm benchmark。
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ckpt) CKPT="$2"; shift 2 ;;
    --uarch-profile) UARCH_PROFILE="$2"; shift 2 ;;
    --functional-dir) FUNCTIONAL_DIRS+=("$2"); shift 2 ;;
    --job-list) JOB_LIST="$2"; shift 2 ;;
    --out-root) OUT_ROOT="$2"; shift 2 ;;
    --quantum-cycles) QUANTUM_CYCLES="$2"; shift 2 ;;
    --k-max) K_MAX="$2"; shift 2 ;;
    --batch) BATCH="$2"; shift 2 ;;
    --repeat) REPEAT="$2"; shift 2 ;;
    --ref-sim-module-dir) REF_SIM_MODULE_DIR="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 1 ;;
  esac
done

if [[ -z "${CKPT}" || -z "${UARCH_PROFILE}" ]]; then
  echo "[persistent-runner][FATAL] --ckpt and --uarch-profile are required" >&2
  usage
  exit 2
fi

if [[ ${#FUNCTIONAL_DIRS[@]} -eq 0 && -z "${JOB_LIST}" ]]; then
  echo "[persistent-runner][FATAL] need at least one --functional-dir or --job-list" >&2
  usage
  exit 2
fi

if [[ ! -f "${CKPT}" ]]; then
  echo "[persistent-runner][FATAL] ckpt not found: ${CKPT}" >&2
  exit 2
fi
if [[ ! -f "${UARCH_PROFILE}" ]]; then
  echo "[persistent-runner][FATAL] uarch profile not found: ${UARCH_PROFILE}" >&2
  exit 2
fi
if [[ ! -f "${DRIVER_PY}" ]]; then
  echo "[persistent-runner][FATAL] driver not found: ${DRIVER_PY}" >&2
  exit 2
fi

mkdir -p "${OUT_ROOT}"
JOB_JSONL="${OUT_ROOT}/jobs.jsonl"
JOB_TSV="${OUT_ROOT}/jobs.tsv"
: > "${JOB_JSONL}"
printf 'job_id\ttag\trepeat_idx\tfunctional_dir\tout_dir\n' > "${JOB_TSV}"

sanitize_tag() {
  local raw="$1"
  raw="${raw// /_}"
  raw="$(printf '%s' "${raw}" | sed 's/[^A-Za-z0-9._-]/_/g')"
  raw="$(printf '%s' "${raw}" | sed 's/_\+/_/g')"
  raw="${raw##_}"
  raw="${raw%%_}"
  if [[ -z "${raw}" ]]; then
    raw="job"
  fi
  printf '%s' "${raw}"
}

default_tag_from_dir() {
  local dir="$1"
  local base parent
  base="$(basename "${dir}")"
  if [[ "${base}" == "functional_parquet" || "${base}" == "functional" ]]; then
    parent="$(basename "$(dirname "${dir}")")"
    printf '%s' "${parent}"
  else
    printf '%s' "${base}"
  fi
}

append_job() {
  local tag="$1"
  local functional_dir="$2"
  local repeat_idx="$3"
  local job_id="$4"
  local out_dir="${OUT_ROOT}/$(printf '%02d' "${job_id}")_${tag}_r$(printf '%02d' "${repeat_idx}")"
  local infer_jsonl="${out_dir}/infer.jsonl"
  local report_json="${out_dir}/report.json"

  if [[ ! -d "${functional_dir}" ]]; then
    echo "[persistent-runner][FATAL] functional dir not found: ${functional_dir}" >&2
    exit 2
  fi
  mkdir -p "${out_dir}"
  printf '%s\t%s\t%s\t%s\t%s\n' \
    "${job_id}" "${tag}" "${repeat_idx}" "${functional_dir}" "${out_dir}" >> "${JOB_TSV}"

  "${PYTHON_BIN}" - "${functional_dir}" "${UARCH_PROFILE}" "${REF_SIM_MODULE_DIR}" \
    "${infer_jsonl}" "${report_json}" "${QUANTUM_CYCLES}" "${K_MAX}" "${BATCH}" >> "${JOB_JSONL}" <<'PY'
import json
import os
import sys

functional_dir, uarch, ref_sim, out_jsonl, report_json, quantum, kmax, batch = sys.argv[1:]
payload = {
    "functional_dir": os.path.abspath(functional_dir),
    "uarch_profile": os.path.abspath(uarch),
    "ref_sim_module_dir": os.path.abspath(ref_sim),
    "out_jsonl": os.path.abspath(out_jsonl),
    "report_json": os.path.abspath(report_json),
    "quantum_cycles": int(quantum),
    "k_max": int(kmax),
    "model_batch_size": int(batch),
}
print(json.dumps(payload, separators=(",", ":")))
PY
}

job_id=0
for functional_dir in "${FUNCTIONAL_DIRS[@]}"; do
  tag="$(sanitize_tag "$(default_tag_from_dir "${functional_dir}")")"
  for ((rep = 1; rep <= REPEAT; rep++)); do
    job_id=$((job_id + 1))
    append_job "${tag}" "${functional_dir}" "${rep}" "${job_id}"
  done
done

if [[ -n "${JOB_LIST}" ]]; then
  if [[ ! -f "${JOB_LIST}" ]]; then
    echo "[persistent-runner][FATAL] job list not found: ${JOB_LIST}" >&2
    exit 2
  fi
  while IFS= read -r line || [[ -n "${line}" ]]; do
    [[ -z "${line}" ]] && continue
    [[ "${line}" =~ ^# ]] && continue
    tag=""
    functional_dir=""
    if [[ "${line}" == *$'\t'* ]]; then
      IFS=$'\t' read -r tag functional_dir <<< "${line}"
    else
      functional_dir="${line}"
      tag="$(default_tag_from_dir "${functional_dir}")"
    fi
    tag="$(sanitize_tag "${tag}")"
    for ((rep = 1; rep <= REPEAT; rep++)); do
      job_id=$((job_id + 1))
      append_job "${tag}" "${functional_dir}" "${rep}" "${job_id}"
    done
  done < "${JOB_LIST}"
fi

if [[ "${job_id}" -eq 0 ]]; then
  echo "[persistent-runner][FATAL] no jobs generated" >&2
  exit 2
fi

echo "[persistent-runner] jobs=${job_id}"
echo "[persistent-runner] job_jsonl=${JOB_JSONL}"
echo "[persistent-runner] out_root=${OUT_ROOT}"

if [[ "${DRY_RUN}" -eq 1 ]]; then
  echo "[persistent-runner] dry run only; generated jobs below:"
  cat "${JOB_JSONL}"
  exit 0
fi

{
  cat "${JOB_JSONL}"
  echo "exit"
} | /usr/bin/time -f 'elapsed=%e' -o "${OUT_ROOT}/session.time.txt" \
  "${PYTHON_BIN}" "${DRIVER_PY}" \
    --persistent-session \
    --ckpt "${CKPT}" \
    > "${OUT_ROOT}/session.stdout.log" \
    2> "${OUT_ROOT}/session.stderr.log"

echo "[persistent-runner] session stdout: ${OUT_ROOT}/session.stdout.log"
echo "[persistent-runner] session stderr: ${OUT_ROOT}/session.stderr.log"
echo "[persistent-runner] session time:   ${OUT_ROOT}/session.time.txt"
echo "[persistent-runner] jobs manifest:  ${JOB_TSV}"
