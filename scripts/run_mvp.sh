#!/usr/bin/env bash
# One-shot: chunk + rollout + train + eval on a subset of TSim raw traces.
#
# Usage:
#   bash scripts/run_mvp.sh --raw /data00/yinhaolang/TSim/data/raw_v27_ffatomic_seed0_c04 \
#     --workloads W_chase_DRAM,W_stream_seq_DRAM \
#     --out /data00/yinhaolang/TCSim/data/mvp_run
set -euo pipefail

RAW=""
WORKLOADS=""
OUT=""
DEVICE="cpu"
CONFIG="$(cd "$(dirname "$0")/.." && pwd)/configs/mvp.yaml"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --raw) RAW="$2"; shift 2;;
        --workloads) WORKLOADS="$2"; shift 2;;
        --out) OUT="$2"; shift 2;;
        --device) DEVICE="$2"; shift 2;;
        --config) CONFIG="$2"; shift 2;;
        *) echo "unknown arg: $1"; exit 2;;
    esac
done

if [[ -z "$RAW" || -z "$OUT" ]]; then
    echo "usage: $0 --raw <trace_root> --out <run_out> [--workloads W1,W2] [--device cpu|cuda]"
    exit 2
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
[[ -x "$PY" ]] || PY=python3
mkdir -p "$OUT"

ROLLOUT_DIR="$OUT/rollout"
CKPT_DIR="$OUT/ckpt"
REPORT="$OUT/eval_report.json"

echo "[run_mvp] Phase 2: chunk + epsilon scheduler + rollout"
"$PY" "$ROOT/scripts/build_rollout.py" --raw "$RAW" --workloads "$WORKLOADS" \
    --out "$ROLLOUT_DIR" --config "$CONFIG"

echo "[run_mvp] Phase 3: train"
"$PY" "$ROOT/scripts/train_mvp.py" --rollout_root "$ROLLOUT_DIR" --out "$CKPT_DIR" \
    --config "$CONFIG" --device "$DEVICE"

echo "[run_mvp] Phase 3: eval"
CKPT="$CKPT_DIR/best.pt"
[[ -f "$CKPT" ]] || CKPT="$CKPT_DIR/last.pt"
"$PY" "$ROOT/scripts/eval_mvp.py" --ckpt "$CKPT" --rollout_root "$ROLLOUT_DIR" \
    --out "$REPORT" --config "$CONFIG" --device "$DEVICE"

echo "[run_mvp] done: $REPORT"
