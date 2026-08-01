#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT=${ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
cd "$PROJECT_ROOT"

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
CKPT=${CKPT:-$PROJECT_ROOT/ckpt/tcsim_v29_latent32_scratch_100m_8gpu_60k/best.pt}
BASE_MANIFEST=${BASE_MANIFEST:-$PROJECT_ROOT/data/v29_global_time_dataset/manifest.json}
GSS_ROOT=${GSS_ROOT:-$PROJECT_ROOT/data/v30_gss_commit_sidecars}
MANIFEST=${MANIFEST:-$PROJECT_ROOT/data/v30_gss_commit_dataset/manifest.json}
OUT=${OUT:-$PROJECT_ROOT/logs/v29_latent32_best55k_gss_seed1_plus_heldout120}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
SIDECAR_WORKERS=${SIDECAR_WORKERS:-64}

# Only the 120 traces not evaluated by the preceding seed0-base run:
#   development_heldout: seed0, 7 workloads x 4 core counts = 28
#   deployment_inference: seed1, 23 workloads x 4 core counts = 92
SPLITS=${SPLITS:-development_heldout,deployment_inference}
CORE_COUNTS=${CORE_COUNTS:-4,8,16,32}
SEEDS=${SEEDS:-0,1}
EXPECTED_TRACES=${EXPECTED_TRACES:-120}

[[ -x "$PY" ]] || { echo "[latent-gss-120][ERROR] missing Python: $PY" >&2; exit 2; }
[[ -f "$CKPT" ]] || { echo "[latent-gss-120][ERROR] missing checkpoint: $CKPT" >&2; exit 2; }
[[ -f "$BASE_MANIFEST" ]] || { echo "[latent-gss-120][ERROR] missing base manifest: $BASE_MANIFEST" >&2; exit 2; }
(( SIDECAR_WORKERS > 0 )) || { echo "[latent-gss-120][ERROR] SIDECAR_WORKERS must be positive" >&2; exit 2; }

echo "[latent-gss-120] phase=1/2 build/reuse commit-clock GSS sidecars"
echo "[latent-gss-120] splits=$SPLITS cores=$CORE_COUNTS workers=$SIDECAR_WORKERS"
"$PY" scripts/build_v30_gss_sidecar.py \
  --manifest "$BASE_MANIFEST" \
  --splits "$SPLITS" \
  --core-counts "$CORE_COUNTS" \
  --clock commit \
  --workers "$SIDECAR_WORKERS" \
  --out-root "$GSS_ROOT" \
  --write-manifest "$MANIFEST"

"$PY" - "$MANIFEST" "$SPLITS" "$CORE_COUNTS" "$EXPECTED_TRACES" <<'PY'
import collections
import json
import os
import sys

from tcsim.v29.inference import load_manifest_sources

manifest_path, split_csv, core_csv, expected_text = sys.argv[1:]
splits = [value.strip() for value in split_csv.split(",") if value.strip()]
cores = {int(value) for value in core_csv.split(",") if value.strip()}
expected = int(expected_text)
rows = [
    row for row in load_manifest_sources(manifest_path, splits)
    if int(row.get("n_cores", -1)) in cores
]
counts = collections.Counter(int(row.get("seed", -1)) for row in rows)
expected_counts = {0: 28, 1: 92}
if len(rows) != expected or dict(counts) != expected_counts:
    raise SystemExit(
        f"expected {expected} traces with seed counts {expected_counts}, "
        f"found {len(rows)} with {dict(counts)}"
    )
missing = []
wrong_clock = []
for row in rows:
    sidecar = str(row.get("gss_sidecar_dir", ""))
    meta_path = os.path.join(sidecar, "meta.json")
    if not sidecar or not os.path.isfile(meta_path):
        missing.append(str(row.get("trace_id", "")))
        continue
    with open(meta_path, "r", encoding="utf-8") as handle:
        meta = json.load(handle)
    if meta.get("clock_source") != "commit":
        wrong_clock.append(str(row.get("trace_id", "")))
if missing or wrong_clock:
    raise SystemExit(
        f"GSS sidecar audit failed: missing={len(missing)} "
        f"wrong_clock={len(wrong_clock)}"
    )
by_core = collections.Counter(int(row["n_cores"]) for row in rows)
print(
    f"[latent-gss-120] sidecar_audit=pass traces={len(rows)} "
    f"seed0_heldout={counts[0]} seed1={counts[1]} "
    f"by_core={dict(sorted(by_core.items()))}"
)
PY

echo "[latent-gss-120] phase=2/2 free-running inference on 120 missing traces"
env \
  ROOT="$PROJECT_ROOT" \
  PY="$PY" \
  CKPT="$CKPT" \
  MANIFEST="$MANIFEST" \
  OUT="$OUT" \
  GPUS="$GPUS" \
  SPLITS="$SPLITS" \
  SEEDS="$SEEDS" \
  MODE=free \
  CORE_COUNTS="$CORE_COUNTS" \
  MAX_ORACLE_SAMPLES=0 \
  MAX_FREE_STEPS=0 \
  MAX_TRACES=0 \
  TARGET_STRIDE="${TARGET_STRIDE:-256}" \
  MIN_STEP_CYCLES=4 \
  MAX_STEP_CYCLES=1024 \
  MAX_NO_PROGRESS_STEPS=64 \
  MAX_CORE_STALL_STEPS=256 \
  AMP_DTYPE=bf16 \
  SDPA_BACKEND=auto \
  TCSIM_CONTEXT_BACKEND=native \
  TCSIM_GSS_BACKEND=native \
  GSS_PMU_ONLY=1 \
  CROSS_ATTENTION_BACKEND=hierarchical_latent \
  QRKV_PROJECTION_BACKEND=fused \
  PROGRESS_EVERY="${PROGRESS_EVERY:-100}" \
  ORACLE_DRIFT_DIAGNOSTICS=0 \
  RESUME="${RESUME:-1}" \
  bash scripts/run_v29_eval_8gpu.sh

echo "[latent-gss-120] complete report=$OUT/report.txt"
