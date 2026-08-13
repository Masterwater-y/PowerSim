#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATASET_ROOT="${DATASET_ROOT:-${PROJECT_ROOT}/tmp/business-excitation-c4}"
OUTPUT_ROOT="${OUTPUT_ROOT:-${DATASET_ROOT}/diagnostics/committed-pipeline-audit-12}"
REFERENCE_ROOT="${REFERENCE_ROOT:-${DATASET_ROOT}/diagnostics/response-closure-b3/fastsim-fill8-directed}"
JOBS="${JOBS:-8}"

python3 "${PROJECT_ROOT}/tools/run_uarch_fastsim.py" \
  --root "${DATASET_ROOT}" \
  --out "${OUTPUT_ROOT}" \
  --matrix "${PROJECT_ROOT}/configs/workloads/business_excitation.json" \
  --config "${PROJECT_ROOT}/configs/gem5/v28_1-time-epoch.cfg" \
  --fastsim "${PROJECT_ROOT}/build/fastsim" \
  --jobs "${JOBS}" \
  --uarch baseline \
  --uarch core_width4 \
  --uarch rob96 \
  --uarch rob256 \
  --uarch iq32 \
  --uarch iq96 \
  --workload gofeed_fanout_wide \
  --workload pytorch_dense_batch \
  --committed-pipeline-audit

python3 "${PROJECT_ROOT}/tools/audit_committed_pipeline.py" \
  --fastsim-root "${OUTPUT_ROOT}" \
  --gem5-root "${DATASET_ROOT}/labels" \
  --reference-root "${REFERENCE_ROOT}" \
  --out "${OUTPUT_ROOT}"
