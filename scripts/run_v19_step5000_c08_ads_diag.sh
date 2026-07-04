#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
cd "$ROOT"

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
CKPT=${CKPT:-ckpt/v19_local_core_delta_8gpu_8000/step_005000}
RAW=${RAW:-data/raw_trace_pool/activecore_eval/c08_seedB_infer17}
WORKLOAD=${WORKLOAD:-W_ads_ranking_proxy}
MAX_LEN=${MAX_LEN:-32768}
MAX_WINDOWS=${MAX_WINDOWS:-0}
HIDDEN_MAX_SAMPLES=${HIDDEN_MAX_SAMPLES:-128}
GPUS=${GPUS:-0,1,2}
TS=${TS:-$(date +%Y%m%d_%H%M%S)}
RUN_ROOT=${RUN_ROOT:-logs/v19_step5000_c08_ads_diag_${TS}}

IFS=',' read -r -a GPU_ARR <<< "$GPUS"
if [[ ${#GPU_ARR[@]} -lt 3 ]]; then
  echo "[err] GPUS must contain at least 3 comma-separated GPU ids, got: $GPUS" >&2
  exit 2
fi

mkdir -p "$RUN_ROOT"

echo "[diag] root=$ROOT"
echo "[diag] ckpt=$CKPT"
echo "[diag] raw=$RAW"
echo "[diag] workload=$WORKLOAD"
echo "[diag] run_root=$RUN_ROOT"
echo "[diag] max_windows=$MAX_WINDOWS hidden_max_samples=$HIDDEN_MAX_SAMPLES"
echo "[diag] gpus=$GPUS"

CKPT="$CKPT" RAW="$RAW" WORKLOAD="$WORKLOAD" GPU="${GPU_ARR[0]}" \
  MAX_LEN="$MAX_LEN" MAX_WINDOWS="$MAX_WINDOWS" QUERY_PLACEMENT=tail_local \
  PLANNER_STATE_SOURCE=pred TAG=v19_step5000_ads_pred \
  OUTDIR="$RUN_ROOT/align_pred" \
  bash scripts/run_v16_ads_oracle_cut_eval.sh \
  > "$RUN_ROOT/align_pred.driver.log" 2>&1 &
p_pred=$!

CKPT="$CKPT" RAW="$RAW" WORKLOAD="$WORKLOAD" GPU="${GPU_ARR[1]}" \
  MAX_LEN="$MAX_LEN" MAX_WINDOWS="$MAX_WINDOWS" QUERY_PLACEMENT=tail_local \
  PLANNER_STATE_SOURCE=label TAG=v19_step5000_ads_label \
  OUTDIR="$RUN_ROOT/align_label" \
  bash scripts/run_v16_ads_oracle_cut_eval.sh \
  > "$RUN_ROOT/align_label.driver.log" 2>&1 &
p_label=$!

HF_HUB_OFFLINE=1 CUDA_VISIBLE_DEVICES="${GPU_ARR[2]}" "$PY" \
  scripts/analyze_core_hidden_similarity.py \
  --ckpt "$CKPT" \
  --data data/windows_v17_bc_split_heads_nophase_all/windows.jsonl \
  --workload "$WORKLOAD" \
  --n-core 8 \
  --max-len "$MAX_LEN" \
  --max-samples "$HIDDEN_MAX_SAMPLES" \
  --batch-size 1 \
  --device cuda \
  --out "$RUN_ROOT/hidden_cpi.json" \
  > "$RUN_ROOT/hidden_cpi.log" 2>&1 &
p_hidden=$!

status=0
for pid in "$p_pred" "$p_label" "$p_hidden"; do
  if ! wait "$pid"; then
    status=1
  fi
done

if [[ "$status" != "0" ]]; then
  echo "[err] at least one job failed. Logs:"
  echo "  $RUN_ROOT/align_pred.driver.log"
  echo "  $RUN_ROOT/align_label.driver.log"
  echo "  $RUN_ROOT/hidden_cpi.log"
  exit "$status"
fi

echo
echo "===== pred alignment ====="
cat "$RUN_ROOT/align_pred/alignment_analysis.txt"

echo
echo "===== label alignment ====="
cat "$RUN_ROOT/align_label/alignment_analysis.txt"

echo
echo "===== hidden/cpi ====="
"$PY" - "$RUN_ROOT/hidden_cpi.json" <<'PY'
import json
import sys

path = sys.argv[1]
summary = json.load(open(path))["summary"]
metrics = summary["metrics"]
keys = [
    "query_pair_cos_mean",
    "pre_adapter_pair_cos_mean",
    "post_adapter_pair_cos_mean",
    "query_center_rel_norm",
    "pre_adapter_center_rel_norm",
    "post_adapter_center_rel_norm",
    "label_cpi_cv",
    "pred_cpi_cv",
    "label_cpi_range_rel",
    "pred_cpi_range_rel",
    "cpi_abs_relerr_mean",
    "pred_label_cpi_corr",
]
for key in keys:
    print(key, metrics.get(key))
print("slowest_hit_rate", summary.get("slowest_hit_rate"))
print("fastest_hit_rate", summary.get("fastest_hit_rate"))
PY

echo
echo "[done] $RUN_ROOT"
