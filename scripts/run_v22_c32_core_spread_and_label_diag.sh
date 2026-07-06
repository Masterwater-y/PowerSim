#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
cd "$ROOT"

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
CKPT=${CKPT:-ckpt/v22_v16_bind_split_direct_no_tstart_8gpu_12000}
RAW=${RAW:-data/raw_v7_seedB_c32_infer17}
MAX_LEN=${MAX_LEN:-32768}
TRAIN_MAX_LEN=${TRAIN_MAX_LEN:-32768}
MAX_WINDOWS=${MAX_WINDOWS:-80}
LOAD_MAX_ROWS_PER_CORE=${LOAD_MAX_ROWS_PER_CORE:-120000}
QUERY_PLACEMENT=${QUERY_PLACEMENT:-tail_local}
DEVICE=${DEVICE:-cuda}
GPUS_CSV=${GPUS_CSV:-0,1,2,3,4}
PLANNER_MODES=${PLANNER_MODES:-"pred label"}
WORKLOADS=${WORKLOADS:-"W_stream W_phased_mix W_chase_dram W_false_sharing W_graph_recall_proxy"}
HIGH_Q=${HIGH_Q:-0.80}
FLAT_RANGE=${FLAT_RANGE:-0.10}
TOP_N=${TOP_N:-20}
PROGRESS_EVERY=${PROGRESS_EVERY:-30}
STRICT_CUDA=${STRICT_CUDA:-0}
DUMP_LLM_HIDDEN_METRICS=${DUMP_LLM_HIDDEN_METRICS:-0}
HIDDEN_HIGH_LABEL_STD=${HIDDEN_HIGH_LABEL_STD:-0.30}
HIDDEN_LOW_LABEL_STD=${HIDDEN_LOW_LABEL_STD:-0.10}
HIDDEN_HIGH_COS=${HIDDEN_HIGH_COS:-0.97}
HIDDEN_LOW_CENTER_REL=${HIDDEN_LOW_CENTER_REL:-0.15}
HIDDEN_TOP_K=${HIDDEN_TOP_K:-12}
TS=${TS:-$(date +%Y%m%d_%H%M%S)}
OUT_ROOT=${OUT_ROOT:-logs/v22_c32_core_spread_label_diag_${TS}}

IFS=',' read -r -a GPUS <<< "$GPUS_CSV"
read -r -a MODE_ARR <<< "$PLANNER_MODES"
read -r -a WORKLOAD_ARR <<< "$WORKLOADS"

if [[ ! -x "$PY" ]]; then
  echo "[diag][error] python not executable: $PY" >&2
  exit 2
fi
if [[ ! -f "$CKPT/head_best.pt" || ! -d "$CKPT/lora_best" ]]; then
  echo "[diag][error] checkpoint missing head_best.pt or lora_best: $CKPT" >&2
  exit 2
fi
if [[ ! -d "$RAW" ]]; then
  echo "[diag][error] raw root not found: $RAW" >&2
  exit 2
fi
if [[ ${#GPUS[@]} -eq 0 ]]; then
  echo "[diag][error] GPUS_CSV is empty" >&2
  exit 2
fi

for mode in "${MODE_ARR[@]}"; do
  case "$mode" in
    pred|label|tq_forward) ;;
    *)
      echo "[diag][error] invalid planner mode: $mode" >&2
      echo "[diag][error] expected one of: pred label tq_forward" >&2
      exit 2
      ;;
  esac
done

for w in "${WORKLOAD_ARR[@]}"; do
  if [[ ! -f "$RAW/$w/stats.txt" || ! -d "$RAW/$w/tao_trace" ]]; then
    echo "[diag][error] missing raw files for workload=$w under $RAW" >&2
    exit 2
  fi
done

mkdir -p "$OUT_ROOT"

cat > "$OUT_ROOT/config.txt" <<EOF
root=$ROOT
python=$PY
ckpt=$CKPT
raw=$RAW
max_len=$MAX_LEN
train_max_len=$TRAIN_MAX_LEN
max_windows=$MAX_WINDOWS
load_max_rows_per_core=$LOAD_MAX_ROWS_PER_CORE
query_placement=$QUERY_PLACEMENT
device=$DEVICE
gpus=$GPUS_CSV
planner_modes=$PLANNER_MODES
workloads=$WORKLOADS
high_q=$HIGH_Q
flat_range=$FLAT_RANGE
top_n=$TOP_N
dump_llm_hidden_metrics=$DUMP_LLM_HIDDEN_METRICS
hidden_high_label_std=$HIDDEN_HIGH_LABEL_STD
hidden_low_label_std=$HIDDEN_LOW_LABEL_STD
hidden_high_cos=$HIDDEN_HIGH_COS
hidden_low_center_rel=$HIDDEN_LOW_CENTER_REL
hidden_top_k=$HIDDEN_TOP_K
timestamp=$TS
EOF

echo "[diag] root=$ROOT"
echo "[diag] ckpt=$CKPT"
echo "[diag] raw=$RAW"
echo "[diag] out_root=$OUT_ROOT"
echo "[diag] modes=$PLANNER_MODES"
echo "[diag] workloads=$WORKLOADS"
echo "[diag] max_windows=$MAX_WINDOWS load_max_rows_per_core=$LOAD_MAX_ROWS_PER_CORE gpus=$GPUS_CSV device=$DEVICE"
echo "[diag] dump_llm_hidden_metrics=$DUMP_LLM_HIDDEN_METRICS"
echo

if [[ "$DEVICE" == cuda* && "$STRICT_CUDA" == "1" ]]; then
  echo "[diag] strict CUDA preflight: CUDA_VISIBLE_DEVICES=${GPUS[0]}"
  if ! CUDA_VISIBLE_DEVICES="${GPUS[0]}" "$PY" -c 'import sys, torch; print(f"cuda_available={torch.cuda.is_available()} device_count={torch.cuda.device_count()}", flush=True); sys.exit(0 if torch.cuda.is_available() else 1)'; then
    echo "[diag][error] torch cannot see CUDA with CUDA_VISIBLE_DEVICES=${GPUS[0]}" >&2
    echo "[diag][hint] run outside the sandbox/session that hides GPUs, or set STRICT_CUDA=0 DEVICE=cpu for a slow smoke run." >&2
    exit 3
  fi
fi

declare -a CHILD_PIDS=()
PROGRESS_PID=""

cleanup_children() {
  if [[ -n "${PROGRESS_PID:-}" ]]; then
    kill "$PROGRESS_PID" 2>/dev/null || true
  fi
  for pid in "${CHILD_PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
}

on_exit() {
  local rc=$?
  if [[ $rc -ne 0 ]]; then
    cleanup_children
    echo "[diag] interrupted/failed rc=$rc; cleaned up child processes" >&2
  fi
}

on_signal() {
  trap - EXIT INT TERM
  cleanup_children
  echo "[diag] interrupted by signal; cleaned up child processes" >&2
  exit 130
}

trap on_exit EXIT
trap on_signal INT TERM

progress_loop() {
  local mode="$1"
  local log_dir="$2"
  while true; do
    sleep "$PROGRESS_EVERY" || true
    echo
    echo "============ progress mode=$mode @ $(date +%H:%M:%S) ============"
    for f in "$log_dir"/*.log; do
      [[ -f "$f" ]] || continue
      local w
      w=$(basename "$f" .log)
      local line
      line=$(grep -E "^\\s*\\[$w\\] " "$f" 2>/dev/null | tail -n 1 || true)
      if [[ -z "$line" ]]; then
        line=$(tail -n 1 "$f" 2>/dev/null || true)
      fi
      printf "%-28s %s\n" "$w" "$line"
    done
  done
}

run_one() {
  local mode="$1"
  local workload="$2"
  local gpu="$3"
  local mode_dir="$OUT_ROOT/$mode"
  local dump_dir="$mode_dir/dump"
  local log_dir="$mode_dir/run_logs"
  local log="$log_dir/$workload.log"
  local -a hidden_args=()
  if [[ "$DUMP_LLM_HIDDEN_METRICS" == "1" ]]; then
    hidden_args+=(--dump-llm-hidden-metrics)
  fi

  mkdir -p "$dump_dir" "$log_dir"
  echo "[diag] launch mode=$mode gpu=$gpu workload=$workload log=$log"
  HF_HUB_OFFLINE=1 CUDA_VISIBLE_DEVICES="$gpu" "$PY" eval/eval_quota_cycles.py \
    --raw-root "$RAW" \
    --workload "$workload" \
    --ckpt "$CKPT" \
    --dt-target 8000 \
    --dt-max 12000 \
    --max-len "$MAX_LEN" \
    --train-max-len "$TRAIN_MAX_LEN" \
    --max-windows "$MAX_WINDOWS" \
    --load-max-rows-per-core "$LOAD_MAX_ROWS_PER_CORE" \
    --query-placement "$QUERY_PLACEMENT" \
    --planner-state-source "$mode" \
    --device "$DEVICE" \
    --dump-window-jsonl-dir "$dump_dir" \
    "${hidden_args[@]}" \
    > "$log" 2>&1
}

wait_batch() {
  local -n _pids="$1"
  local -n _names="$2"
  local -n _logs="$3"
  local status=0
  for i in "${!_pids[@]}"; do
    local pid="${_pids[$i]}"
    local name="${_names[$i]}"
    local log="${_logs[$i]}"
    if wait "$pid"; then
      echo "[diag] done $name"
    else
      local rc=$?
      echo "[diag][error] failed $name rc=$rc log=$log" >&2
      tail -80 "$log" >&2 || true
      status=$rc
    fi
  done
  _pids=()
  _names=()
  _logs=()
  return "$status"
}

run_mode() {
  local mode="$1"
  local mode_dir="$OUT_ROOT/$mode"
  local log_dir="$mode_dir/run_logs"
  mkdir -p "$mode_dir/dump" "$log_dir"

  echo
  echo "============================================================"
  echo "[diag] start mode=$mode"
  echo "============================================================"

  local progress_pid=""
  if [[ "$PROGRESS_EVERY" != "0" ]]; then
    progress_loop "$mode" "$log_dir" &
    progress_pid=$!
    PROGRESS_PID=$progress_pid
  fi

  local -a pids=()
  local -a names=()
  local -a logs=()
  local idx=0
  local status=0
  for w in "${WORKLOAD_ARR[@]}"; do
    local gpu="${GPUS[$((idx % ${#GPUS[@]}))]}"
    local log="$log_dir/$w.log"
    run_one "$mode" "$w" "$gpu" &
    pids+=("$!")
    CHILD_PIDS+=("$!")
    names+=("$mode/$w")
    logs+=("$log")
    idx=$((idx + 1))
    if [[ ${#pids[@]} -ge ${#GPUS[@]} ]]; then
      if ! wait_batch pids names logs; then
        status=1
      fi
    fi
  done
  if [[ ${#pids[@]} -gt 0 ]]; then
    if ! wait_batch pids names logs; then
      status=1
    fi
  fi

  if [[ -n "$progress_pid" ]]; then
    kill "$progress_pid" 2>/dev/null || true
    wait "$progress_pid" 2>/dev/null || true
    PROGRESS_PID=""
  fi

  if [[ "$status" -ne 0 ]]; then
    echo "[diag][error] mode failed: $mode" >&2
    exit "$status"
  fi
}

analyze_mode() {
  local mode="$1"
  local mode_dir="$OUT_ROOT/$mode"
  local dump_dir="$mode_dir/dump"
  local align_dir="$mode_dir/alignment"
  mkdir -p "$align_dir"

  echo
  echo "============================================================"
  echo "[diag] analyze mode=$mode"
  echo "============================================================"

  local expected=${#WORKLOAD_ARR[@]}
  local actual
  actual=$(find "$dump_dir" -maxdepth 1 -name '*.windows.jsonl' -type f | wc -l)
  if [[ "$actual" -lt "$expected" ]]; then
    echo "[diag][error] expected $expected dump files for mode=$mode, got $actual in $dump_dir" >&2
    exit 4
  fi

  "$PY" scripts/analyze_eval_core_cpi_spread.py \
    "$dump_dir" \
    --high-q "$HIGH_Q" \
    --flat-range "$FLAT_RANGE" \
    --top-n "$TOP_N" \
    --json-out "$mode_dir/core_spread_summary.json" \
    | tee "$mode_dir/core_spread_summary.txt"

  if [[ "$DUMP_LLM_HIDDEN_METRICS" == "1" ]]; then
    "$PY" scripts/analyze_eval_llm_hidden_dump.py \
      "$dump_dir" \
      --out "$mode_dir/llm_hidden_summary.json" \
      --high-label-std "$HIDDEN_HIGH_LABEL_STD" \
      --low-label-std "$HIDDEN_LOW_LABEL_STD" \
      --high-cos "$HIDDEN_HIGH_COS" \
      --low-center-rel "$HIDDEN_LOW_CENTER_REL" \
      --top-k "$HIDDEN_TOP_K" \
      | tee "$mode_dir/llm_hidden_summary.txt"
  fi

  for dump in "$dump_dir"/*.windows.jsonl; do
    local w
    w=$(basename "$dump" .windows.jsonl)
    "$PY" scripts/analyze_alignment_dump.py "$dump" > "$align_dir/$w.alignment.txt"
  done
}

write_cpi_summaries() {
  "$PY" - "$OUT_ROOT" "$PLANNER_MODES" <<'PY'
import glob
import json
import math
import os
import re
import sys

out_root = sys.argv[1]
modes = sys.argv[2].split()

def grab(text, pat, cast=float):
    m = re.search(pat, text)
    if not m:
        return None
    try:
        return cast(m.group(1))
    except Exception:
        return None

def load_summary(text):
    pos = text.rfind("Summary")
    if pos < 0:
        return None
    start = text.find("[", pos)
    if start < 0:
        return None
    dec = json.JSONDecoder()
    try:
        arr, _ = dec.raw_decode(text[start:])
    except Exception:
        return None
    if isinstance(arr, list) and arr:
        return arr[0]
    return None

def pct(v):
    if v is None:
        return ""
    try:
        f = float(v)
    except Exception:
        return ""
    if not math.isfinite(f):
        return ""
    return f"{100.0 * f:.6g}"

rows = []
for mode in modes:
    log_dir = os.path.join(out_root, mode, "run_logs")
    for fp in sorted(glob.glob(os.path.join(log_dir, "W_*.log"))):
        workload = os.path.basename(fp)[:-4]
        text = open(fp, encoding="utf-8", errors="replace").read()
        obj = load_summary(text) or {}
        row = {
            "mode": mode,
            "workload": workload,
            "windows": obj.get("windows", grab(text, r"\(windows=(\d+)\)", int)),
            "pred_cpi_uop": obj.get("pred_cpi_uop", grab(text, r"cpi_uop\s+pred\s*=\s*([0-9.eE+-]+)")),
            "label_cpi_uop": obj.get("label_cpi_uop", grab(text, r"cpi_uop\s+label\s*=\s*([0-9.eE+-]+)")),
            "roi_cpi_uop": obj.get("roi_stats_cpi_uop", grab(text, r"cpi_uop\s+roi\s*=\s*([0-9.eE+-]+)")),
            "pred_vs_label_pct": pct(obj.get("pred_vs_label_cpi_uop", None)),
            "pred_vs_roi_pct": pct(obj.get("pred_vs_roi_cpi_uop", None)),
            "label_vs_roi_pct": pct(obj.get("label_vs_roi_cpi_uop", None)),
            "win_mape_pct": pct(obj.get("win_mape_cpi_uop", None)),
            "log": fp,
        }
        rows.append(row)

cols = [
    "mode", "workload", "windows", "pred_cpi_uop", "label_cpi_uop",
    "roi_cpi_uop", "pred_vs_label_pct", "pred_vs_roi_pct",
    "label_vs_roi_pct", "win_mape_pct", "log",
]
summary_path = os.path.join(out_root, "cpi_summary.tsv")
with open(summary_path, "w", encoding="utf-8") as fh:
    fh.write("\t".join(cols) + "\n")
    for r in rows:
        fh.write("\t".join("" if r.get(c) is None else str(r.get(c)) for c in cols) + "\n")

by_workload = {}
for r in rows:
    by_workload.setdefault(r["workload"], {})[r["mode"]] = r

cmp_cols = [
    "workload",
    "pred_cut_err_label_pct",
    "label_cut_err_label_pct",
    "label_minus_pred_pp",
    "pred_cut_pred_cpi",
    "label_cut_pred_cpi",
    "pred_cut_label_cpi",
    "label_cut_label_cpi",
    "pred_cut_windows",
    "label_cut_windows",
]
cmp_path = os.path.join(out_root, "pred_vs_label_mode_comparison.tsv")
with open(cmp_path, "w", encoding="utf-8") as fh:
    fh.write("\t".join(cmp_cols) + "\n")
    for workload in sorted(by_workload):
        pred = by_workload[workload].get("pred")
        label = by_workload[workload].get("label")
        if not pred or not label:
            continue
        try:
            pred_err = float(pred.get("pred_vs_label_pct") or "nan")
            label_err = float(label.get("pred_vs_label_pct") or "nan")
            delta = label_err - pred_err
        except Exception:
            pred_err = label_err = delta = float("nan")
        out = {
            "workload": workload,
            "pred_cut_err_label_pct": "" if not math.isfinite(pred_err) else f"{pred_err:.6g}",
            "label_cut_err_label_pct": "" if not math.isfinite(label_err) else f"{label_err:.6g}",
            "label_minus_pred_pp": "" if not math.isfinite(delta) else f"{delta:.6g}",
            "pred_cut_pred_cpi": pred.get("pred_cpi_uop", ""),
            "label_cut_pred_cpi": label.get("pred_cpi_uop", ""),
            "pred_cut_label_cpi": pred.get("label_cpi_uop", ""),
            "label_cut_label_cpi": label.get("label_cpi_uop", ""),
            "pred_cut_windows": pred.get("windows", ""),
            "label_cut_windows": label.get("windows", ""),
        }
        fh.write("\t".join(str(out[c]) for c in cmp_cols) + "\n")

print(f"[diag] wrote {summary_path}")
print(f"[diag] wrote {cmp_path}")
PY
}

for mode in "${MODE_ARR[@]}"; do
  run_mode "$mode"
  analyze_mode "$mode"
done

write_cpi_summaries

echo
echo "============================================================"
echo "[diag] complete"
echo "============================================================"
echo "[diag] out_root=$OUT_ROOT"
echo "[diag] config=$OUT_ROOT/config.txt"
echo "[diag] cpi_summary=$OUT_ROOT/cpi_summary.tsv"
echo "[diag] mode_comparison=$OUT_ROOT/pred_vs_label_mode_comparison.tsv"
for mode in "${MODE_ARR[@]}"; do
  echo "[diag] $mode spread=$OUT_ROOT/$mode/core_spread_summary.txt"
  if [[ "$DUMP_LLM_HIDDEN_METRICS" == "1" ]]; then
    echo "[diag] $mode hidden=$OUT_ROOT/$mode/llm_hidden_summary.txt"
  fi
  echo "[diag] $mode alignment_dir=$OUT_ROOT/$mode/alignment"
done
echo
echo "[diag] quick comparison:"
if [[ -s "$OUT_ROOT/pred_vs_label_mode_comparison.tsv" ]]; then
  column -t -s $'\t' "$OUT_ROOT/pred_vs_label_mode_comparison.tsv" || cat "$OUT_ROOT/pred_vs_label_mode_comparison.tsv"
fi

trap - EXIT INT TERM
