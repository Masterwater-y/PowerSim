#!/usr/bin/env bash
# Reuse or collect the complete seed1 deployment corpus (16 base + 7 heldout
# workloads on c4/c8/c16/c32), then add its packed chunks to the manifest/cache.
# Core counts are serial; workloads inside one core-count group run in parallel.
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/TCSim}
TSIM_ROOT=${TSIM_ROOT:-/data00/yinhaolang/TSim}
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
WORKERS=${WORKERS:-64}
COLLECT_PARALLEL=${COLLECT_PARALLEL:-23}
CONVERT_PARALLEL=${CONVERT_PARALLEL:-23}
AUDIT_REPORT=${AUDIT_REPORT:-$ROOT/data/v28_1_business_a2_sharedzipf_seed01_raw_audit.json}
DATASET_TAG=${DATASET_TAG:-v28_1_business_a2_sharedzipf}
CORES_LIST=${CORES_LIST:-"4 8 16 32"}

cd "$ROOT"

raw_ready=$("$PY" - "$TSIM_ROOT" "$DATASET_TAG" <<'PY'
from pathlib import Path
import sys

tsim_root, tag = sys.argv[1:]
expected_workloads = {
    "W_v28_int_alu_dense", "W_v28_int_div_serial", "W_v28_fp_alu_dense",
    "W_v28_simd_sse_dense", "W_v28_cache_L1_mixed", "W_v28_cache_L2_mixed",
    "W_v28_memory_seq_moderate", "W_v28_memory_random_mlp",
    "W_v28_coh_readmostly_sparse", "W_v28_marine_base", "W_v28_gofeed_base",
    "W_v28_flink_base", "W_v28_mysql_base", "W_v28_redis_base",
    "W_v28_pytorch_base", "W_v28_bvc_encoder_base", "W_v28_marine_heldout",
    "W_v28_gofeed_heldout", "W_v28_flink_heldout", "W_v28_mysql_heldout",
    "W_v28_redis_heldout", "W_v28_pytorch_heldout", "W_v28_bvc_encoder_heldout",
}
ok = True
for cores in (4, 8, 16, 32):
    root = Path(tsim_root) / "data" / f"raw_{tag}_seed1_c{cores:02d}"
    present = {p.name for p in root.iterdir() if p.is_dir()} if root.is_dir() else set()
    n_aligned = sum(1 for _ in root.rglob("*.aligned.parquet")) if root.is_dir() else 0
    expected_files = cores * len(expected_workloads)
    cell_ok = expected_workloads <= present and n_aligned == expected_files
    print(
        f"[seed1][raw] c{cores:02d} aligned={n_aligned}/{expected_files} "
        f"workloads={len(present & expected_workloads)}/{len(expected_workloads)}",
        file=sys.stderr,
    )
    ok = ok and cell_ok
print(1 if ok else 0)
PY
)

if [[ "$raw_ready" != "1" ]]; then
  env \
    TSIM_ROOT="$TSIM_ROOT" \
    PY="$PY" \
    SEEDS=1 \
    MODE=all \
    CORES_LIST="$CORES_LIST" \
    COLLECT_PARALLEL="$COLLECT_PARALLEL" \
    CONVERT_PARALLEL="$CONVERT_PARALLEL" \
    RUN_AUDIT=1 \
    AUDIT_OUT="$AUDIT_REPORT" \
    DATASET_TAG="$DATASET_TAG" \
    bash scripts/tmp/run_v28_business_serial_cores_collect.sh
elif [[ ! -f "$AUDIT_REPORT" ]]; then
  "$PY" scripts/audit_v28_raw_dataset.py \
    --root-glob "$TSIM_ROOT/data/raw_${DATASET_TAG}_seed*_c*" \
    --sample-regions 9 \
    --sample-uops-per-core 8192 \
    --out "$AUDIT_REPORT"
else
  echo "[seed1] complete aligned raw already exists; skip collection"
fi

"$PY" scripts/build_v28_dataset.py \
  --out "$ROOT/data/v28_1_business_a2_sharedzipf_dataset" \
  --config "$ROOT/configs/mvp_100m.yaml" \
  --input-format aligned \
  --audit-report "$AUDIT_REPORT" \
  --train-seeds 0 \
  --deployment-seeds 1 \
  --build \
  --workers "$WORKERS" \
  --skip-existing

"$PY" - "$ROOT/data/v28_1_business_a2_sharedzipf_dataset/manifest.json" <<'PY'
import json, sys
path = sys.argv[1]
manifest = json.load(open(path, "r", encoding="utf-8"))
rows = manifest.get("splits", {}).get("deployment_inference", [])
counts = {}
for row in rows:
    key = (int(row["seed"]), int(row["n_cores"]))
    counts[key] = counts.get(key, 0) + 1
expected = {(1, cores): 23 for cores in (4, 8, 16, 32)}
if len(rows) != 92 or counts != expected:
    raise SystemExit(
        f"expected 92 seed1 deployment traces (23 each at c4/c8/c16/c32), "
        f"found {len(rows)} with {counts}: {path}"
    )
print(f"[seed1] deployment_inference=92 distribution={counts} manifest={path}")
PY
