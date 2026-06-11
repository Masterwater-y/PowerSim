#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON:-$(command -v python3.11 || command -v python3)}"
JOBS="${JOBS:-32}"
GCC11_LIB="${GCC11_LIB:-/opt/gcc-11/lib64}"

echo "[infer-bundle] ROOT=$ROOT"
echo "[infer-bundle] PYTHON=$PYTHON_BIN"
echo "[infer-bundle] JOBS=$JOBS"

for tool in cmake make g++ "$PYTHON_BIN"; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "[infer-bundle][FATAL] missing tool: $tool" >&2
    exit 2
  fi
done

"$PYTHON_BIN" -m pip install -r "$ROOT/requirements.txt"

if [[ -d "$GCC11_LIB" ]]; then
  export LD_LIBRARY_PATH="$GCC11_LIB:${LD_LIBRARY_PATH:-}"
fi

cmake -S "$ROOT/mesi_ref_sim" -B "$ROOT/mesi_ref_sim/build" \
  -DPython3_EXECUTABLE="$PYTHON_BIN"
cmake --build "$ROOT/mesi_ref_sim/build" -j"$JOBS"

"$PYTHON_BIN" -m py_compile \
  "$ROOT/tools/derive_mem_events.py" \
  "$ROOT/tools/build_inference_input.py" \
  "$ROOT/tools/compare_pred_vs_truth.py" \
  "$ROOT/tools/synthesize_cpi.py" \
  "$ROOT/ml/dataset.py" \
  "$ROOT/ml/model.py" \
  "$ROOT/ml/infer.py" \
  "$ROOT/functional_trace/schema.py" \
  "$ROOT/functional_trace/extract_from_records.py" \
  "$ROOT/driver/ref_sim_client.py" \
  "$ROOT/driver/windowed_features.py" \
  "$ROOT/driver/reference_clock.py" \
  "$ROOT/driver/inference_driver.py" \
  "$ROOT/mesi_ref_sim/scripts/compare_oracle.py" \
  "$ROOT/mesi_ref_sim/scripts/compare_ifetch.py" \
  "$ROOT/mesi_ref_sim/scripts/pmu_report.py"

echo "[infer-bundle] install OK"
echo "  ref_sim : $ROOT/mesi_ref_sim/build/mesi_ref_sim"
echo "  ref_sim_py : $ROOT/mesi_ref_sim/build/ref_sim_py*.so"
