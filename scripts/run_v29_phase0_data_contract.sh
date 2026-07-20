#!/usr/bin/env bash
# run_v29_phase0_data_contract.sh — one-shot Phase 0 data-contract build.
#
# Runs the four Phase 0 stages in order:
#   1. build_static_dict.py         (objdump-based static dictionary + verify)
#   2. build_v28_1_macro_chunks.py  (macro-boundary fixed chunks + labels)
#   3. build_v28_1_manifest.py      (run/window manifest with strict split)
#   4. leakage_probes.py            (allowlist + metadata GBDT + target shuffle)
#
# Exits non-zero on the first gate failure. All arguments are optional.
#
# Usage:
#   bash scripts/run_v29_phase0_data_contract.sh \
#       --raw-root /data00/yinhaolang/TSim/data \
#       --raw-prefix raw_v28_1_business_a2_sharedzipf \
#       --seeds 0,1 --cores 01,04,08,16,32 \
#       --workload-bin /data00/yinhaolang/TSim/workloads/bin \
#       --out /data00/yinhaolang/LLMSim/data/v28_1 \
#       --python /data00/yinhaolang/infer/.venv/bin/python
#
set -euo pipefail

RAW_ROOT=/data00/yinhaolang/TSim/data
RAW_PREFIX=raw_v28_1_business_a2_sharedzipf
SEEDS=0,1
CORES=01,04,08,16,32
WORKLOAD_BIN=/data00/yinhaolang/TSim/workloads/bin
OUT=/data00/yinhaolang/LLMSim/data/v28_1
PY=/data00/yinhaolang/infer/.venv/bin/python
K_MACRO=256
VERIFY_SAMPLE=1000
TOL_FRAC=0.005
GATE_R2=0.30
GATE_SHUFFLE_R2=0.05
LIMIT_RUNS=0
JOBS=1
STAGES=all

while [[ $# -gt 0 ]]; do
  case "$1" in
    --raw-root)     RAW_ROOT=$2; shift 2;;
    --raw-prefix)   RAW_PREFIX=$2; shift 2;;
    --seeds)        SEEDS=$2; shift 2;;
    --cores)        CORES=$2; shift 2;;
    --workload-bin) WORKLOAD_BIN=$2; shift 2;;
    --out)          OUT=$2; shift 2;;
    --python)       PY=$2; shift 2;;
    --k-macro)      K_MACRO=$2; shift 2;;
    --verify-sample) VERIFY_SAMPLE=$2; shift 2;;
    --tol-frac)     TOL_FRAC=$2; shift 2;;
    --gate-r2)      GATE_R2=$2; shift 2;;
    --gate-shuffle-r2) GATE_SHUFFLE_R2=$2; shift 2;;
    --limit-runs)   LIMIT_RUNS=$2; shift 2;;
    --jobs)         JOBS=$2; shift 2;;
    --stages)       STAGES=$2; shift 2;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# //'; exit 0;;
    *)
      echo "unknown arg: $1" >&2; exit 2;;
  esac
done

REPO=/data00/yinhaolang/LLMSim
LOG_DIR="$OUT/logs"
mkdir -p "$LOG_DIR"
export PYTHONPATH="$REPO:/data00/yinhaolang/TCSim:${PYTHONPATH:-}"

run_stage() {
  local stage=$1; shift
  if [[ "$STAGES" != "all" && ",$STAGES," != *",$stage,"* ]]; then
    echo "[skip] stage=$stage (not in --stages=$STAGES)"
    return 0
  fi
  echo "=== stage $stage ==="
  local log="$LOG_DIR/stage_${stage}.log"
  set +e
  "$@" 2>&1 | tee "$log"
  local rc=${PIPESTATUS[0]}
  set -e
  if [[ $rc -ne 0 ]]; then
    echo "[FAIL] stage=$stage rc=$rc (log=$log)" >&2
    exit $rc
  fi
}

echo "== Phase 0 data contract =="
echo "  raw_root=$RAW_ROOT"
echo "  raw_prefix=$RAW_PREFIX"
echo "  seeds=$SEEDS cores=$CORES"
echo "  workload_bin=$WORKLOAD_BIN"
echo "  out=$OUT"
echo "  python=$PY"
echo "  k_macro=$K_MACRO tol=$TOL_FRAC jobs=$JOBS"
echo

# ---------------------------------------------------------------------------
# Stage 1: static disassembly dictionary
# ---------------------------------------------------------------------------
run_stage static_dict \
  "$PY" "$REPO/data/build_static_dict.py" \
    --workload-bin "$WORKLOAD_BIN" \
    --out "$OUT/static_dict" \
    --verify --sample "$VERIFY_SAMPLE" \
    --gate-min-agreement 1.0

# ---------------------------------------------------------------------------
# Stage 2: fixed macro chunks + labels
# ---------------------------------------------------------------------------
CHUNKS_ARGS=(
  --raw-root "$RAW_ROOT" --raw-prefix "$RAW_PREFIX"
  --seeds "$SEEDS" --cores "$CORES"
  --out-root "$OUT/chunks" --k-macro "$K_MACRO"
  --static-dict-manifest "$OUT/static_dict/manifest.jsonl"
  --tol-frac "$TOL_FRAC"
  --jobs "$JOBS"
)
if [[ "$LIMIT_RUNS" != "0" ]]; then
  CHUNKS_ARGS+=(--limit "$LIMIT_RUNS")
fi
run_stage macro_chunks \
  "$PY" "$REPO/data/build_v28_1_macro_chunks.py" "${CHUNKS_ARGS[@]}"

# ---------------------------------------------------------------------------
# Stage 3: manifest + window manifest
# ---------------------------------------------------------------------------
run_stage manifest \
  "$PY" "$REPO/data/build_v28_1_manifest.py" \
    --raw-root "$RAW_ROOT" --raw-prefix "$RAW_PREFIX" \
    --seeds "$SEEDS" --cores "$CORES" \
    --chunks-root "$OUT/chunks" \
    --static-dict-manifest "$OUT/static_dict/manifest.jsonl" \
    --out "$OUT"

# ---------------------------------------------------------------------------
# Stage 4: leakage probes
# ---------------------------------------------------------------------------
run_stage leakage \
  "$PY" "$REPO/data/leakage_probes.py" \
    --chunks-root "$OUT/chunks" \
    --out "$OUT/leakage_report.json" \
    --gate-r2 "$GATE_R2" \
    --gate-shuffle-r2 "$GATE_SHUFFLE_R2"

echo
echo "[gate PASS] Phase 0 data contract"
echo "  static_dict=$OUT/static_dict/manifest.jsonl"
echo "  chunks=$OUT/chunks/"
echo "  manifest=$OUT/manifest.parquet"
echo "  windows=$OUT/window_manifest.parquet"
echo "  leakage_report=$OUT/leakage_report.json"
