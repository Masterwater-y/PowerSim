#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
cd "$ROOT"

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
CKPT=${CKPT:-ckpt/v16_v9core_tail_local_delta_rank_8gpu_8000/step_005000}
RAW=${RAW:-data/raw_trace_pool/activecore_eval/c08_seedB_infer17}
WORKLOAD=${WORKLOAD:-W_ads_ranking_proxy}
GPU=${GPU:-0}
MAX_LEN=${MAX_LEN:-32768}
TRAIN_MAX_LEN=${TRAIN_MAX_LEN:-32768}
MAX_WINDOWS=${MAX_WINDOWS:-0}
QUERY_PLACEMENT=${QUERY_PLACEMENT:-tail_local}
PLANNER_STATE_SOURCE=${PLANNER_STATE_SOURCE:-label}
TAG=${TAG:-v16_ads_oracle_cut}
TS=${TS:-$(date +%Y%m%d_%H%M%S)}
OUTDIR=${OUTDIR:-logs/${TAG}_${WORKLOAD}_${TS}}

mkdir -p "$OUTDIR"

echo "[ads-oracle] root=$ROOT"
echo "[ads-oracle] ckpt=$CKPT"
echo "[ads-oracle] raw=$RAW"
echo "[ads-oracle] workload=$WORKLOAD"
echo "[ads-oracle] gpu=$GPU max_len=$MAX_LEN max_windows=$MAX_WINDOWS"
echo "[ads-oracle] query_placement=$QUERY_PLACEMENT"
echo "[ads-oracle] planner_state_source=$PLANNER_STATE_SOURCE"
echo "[ads-oracle] outdir=$OUTDIR"

HF_HUB_OFFLINE=1 CUDA_VISIBLE_DEVICES="$GPU" "$PY" eval/eval_quota_cycles.py \
  --raw-root "$RAW" \
  --workload "$WORKLOAD" \
  --ckpt "$CKPT" \
  --max-len "$MAX_LEN" \
  --train-max-len "$TRAIN_MAX_LEN" \
  --max-windows "$MAX_WINDOWS" \
  --query-placement "$QUERY_PLACEMENT" \
  --planner-state-source "$PLANNER_STATE_SOURCE" \
  --dump-window-jsonl-dir "$OUTDIR" \
  > "$OUTDIR/run.log" 2>&1

DUMP="$OUTDIR/${WORKLOAD}.windows.jsonl"
ANALYSIS="$OUTDIR/alignment_analysis.txt"
if [[ ! -s "$DUMP" ]]; then
  echo "[ads-oracle][error] missing or empty dump: $DUMP" >&2
  tail -80 "$OUTDIR/run.log" >&2 || true
  exit 2
fi

"$PY" scripts/analyze_alignment_dump.py "$DUMP" > "$ANALYSIS"

echo "[ads-oracle] run_log=$OUTDIR/run.log"
echo "[ads-oracle] dump=$DUMP"
echo "[ads-oracle] analysis=$ANALYSIS"
echo
tail -80 "$OUTDIR/run.log" || true
echo
cat "$ANALYSIS"
