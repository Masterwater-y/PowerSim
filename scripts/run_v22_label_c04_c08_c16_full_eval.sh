#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-/data00/yinhaolang/LLMSim}
cd "$ROOT"

export TMPDIR="${TMPDIR:-$ROOT/tmp}"
mkdir -p "$TMPDIR" logs

PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}
CKPT=${CKPT:-ckpt/v22_v16_bind_split_direct_no_tstart_8gpu_12000}
CORES=${CORES:-"04 08 16"}
GPUS=${GPUS:-0,1,2,3,4,5,6,7}
MAX_LEN=${MAX_LEN:-32768}
TRAIN_MAX_LEN=${TRAIN_MAX_LEN:-32768}
MAX_WINDOWS=${MAX_WINDOWS:-0}
LOAD_MAX_ROWS_PER_CORE=${LOAD_MAX_ROWS_PER_CORE:-0}
QUERY_PLACEMENT=${QUERY_PLACEMENT:-tail_local}
PLANNER_STATE_SOURCE=${PLANNER_STATE_SOURCE:-label}
DEVICE=${DEVICE:-cuda}
DT_TARGET=${DT_TARGET:-8000}
DT_MAX=${DT_MAX:-12000}
PROGRESS_EVERY=${PROGRESS_EVERY:-60}
TS=${TS:-$(date +%Y%m%d_%H%M%S)}
OUT_ROOT=${OUT_ROOT:-logs/v22_label_c04_c08_c16_full_${TS}}

WORKLOADS=${WORKLOADS:-"W_ads_ctr W_ads_ranking_proxy W_branch_storm W_chase_dram W_compute_int W_false_sharing W_feed_ranking W_fp_compute_dense W_fp_lite W_graph_recall_proxy W_indirect W_int_div W_interest_graph_recall W_mlp_light W_phased_mix W_search_index_proxy W_stream"}

IFS=',' read -r -a GPU_ARR <<< "$GPUS"
read -r -a CORE_ARR <<< "$CORES"
read -r -a WORKLOAD_ARR <<< "$WORKLOADS"

if [[ ! -x "$PY" ]]; then
  echo "[v22-label][error] python not executable: $PY" >&2
  exit 2
fi
if [[ ! -f "$CKPT/head_best.pt" || ! -d "$CKPT/lora_best" ]]; then
  echo "[v22-label][error] checkpoint missing head_best.pt or lora_best: $CKPT" >&2
  exit 2
fi
if [[ ${#GPU_ARR[@]} -eq 0 ]]; then
  echo "[v22-label][error] GPUS is empty" >&2
  exit 2
fi
case "$PLANNER_STATE_SOURCE" in
  pred|label|tq_forward) ;;
  *)
    echo "[v22-label][error] invalid PLANNER_STATE_SOURCE=$PLANNER_STATE_SOURCE" >&2
    exit 2
    ;;
esac

for C in "${CORE_ARR[@]}"; do
  RAW="data/raw_v7_seedB_c${C}_infer17"
  if [[ ! -d "$RAW" ]]; then
    echo "[v22-label][error] raw root not found: $RAW" >&2
    exit 2
  fi
  for W in "${WORKLOAD_ARR[@]}"; do
    if [[ ! -f "$RAW/$W/stats.txt" || ! -d "$RAW/$W/tao_trace" ]]; then
      echo "[v22-label][error] missing workload=$W under $RAW" >&2
      exit 2
    fi
  done
done

mkdir -p "$OUT_ROOT"
cat > "$OUT_ROOT/config.txt" <<EOF
root=$ROOT
python=$PY
ckpt=$CKPT
cores=$CORES
gpus=$GPUS
max_len=$MAX_LEN
train_max_len=$TRAIN_MAX_LEN
max_windows=$MAX_WINDOWS
load_max_rows_per_core=$LOAD_MAX_ROWS_PER_CORE
query_placement=$QUERY_PLACEMENT
planner_state_source=$PLANNER_STATE_SOURCE
device=$DEVICE
dt_target=$DT_TARGET
dt_max=$DT_MAX
workloads=$WORKLOADS
timestamp=$TS
EOF

echo "[v22-label] start $(date '+%F %T')"
echo "[v22-label] out_root=$OUT_ROOT"
echo "[v22-label] ckpt=$CKPT"
echo "[v22-label] cores=$CORES workloads=${#WORKLOAD_ARR[@]} total_tasks=$((${#CORE_ARR[@]} * ${#WORKLOAD_ARR[@]}))"
echo "[v22-label] gpus=$GPUS planner=$PLANNER_STATE_SOURCE max_windows=$MAX_WINDOWS load_max_rows_per_core=$LOAD_MAX_ROWS_PER_CORE"
echo

declare -a TASK_CORES=()
declare -a TASK_WORKLOADS=()
for C in "${CORE_ARR[@]}"; do
  mkdir -p "$OUT_ROOT/c${C}/run_logs"
  for W in "${WORKLOAD_ARR[@]}"; do
    TASK_CORES+=("$C")
    TASK_WORKLOADS+=("$W")
  done
done

declare -A GPU_PID=()
declare -A GPU_CORE=()
declare -A GPU_WORKLOAD=()
declare -A GPU_LOG=()
NEXT_TASK=0
FAILS=0
DONE=0
PROGRESS_PID=""

cleanup_children() {
  if [[ -n "${PROGRESS_PID:-}" ]]; then
    kill "$PROGRESS_PID" 2>/dev/null || true
  fi
  for G in "${!GPU_PID[@]}"; do
    kill "${GPU_PID[$G]}" 2>/dev/null || true
  done
}

on_signal() {
  trap - EXIT INT TERM
  cleanup_children
  echo "[v22-label] interrupted; cleaned child processes" >&2
  exit 130
}

trap on_signal INT TERM
trap 'rc=$?; if [[ $rc -ne 0 ]]; then cleanup_children; fi' EXIT

launch_task() {
  local gpu="$1"
  local idx="$2"
  local C="${TASK_CORES[$idx]}"
  local W="${TASK_WORKLOADS[$idx]}"
  local RAW="data/raw_v7_seedB_c${C}_infer17"
  local LOG="$OUT_ROOT/c${C}/run_logs/${W}.log"
  local -a device_args=()
  if [[ -n "$DEVICE" ]]; then
    device_args=(--device "$DEVICE")
  fi

  echo "[v22-label] launch gpu=$gpu c${C} workload=$W log=$LOG"
  HF_HUB_OFFLINE=1 CUDA_VISIBLE_DEVICES="$gpu" "$PY" eval/eval_quota_cycles.py \
    --raw-root "$RAW" \
    --workload "$W" \
    --ckpt "$CKPT" \
    --dt-target "$DT_TARGET" \
    --dt-max "$DT_MAX" \
    --max-len "$MAX_LEN" \
    --train-max-len "$TRAIN_MAX_LEN" \
    --max-windows "$MAX_WINDOWS" \
    --load-max-rows-per-core "$LOAD_MAX_ROWS_PER_CORE" \
    --query-placement "$QUERY_PLACEMENT" \
    --planner-state-source "$PLANNER_STATE_SOURCE" \
    "${device_args[@]}" \
    > "$LOG" 2>&1 &

  GPU_PID[$gpu]=$!
  GPU_CORE[$gpu]="$C"
  GPU_WORKLOAD[$gpu]="$W"
  GPU_LOG[$gpu]="$LOG"
}

progress_loop() {
  while true; do
    sleep "$PROGRESS_EVERY" || true
    echo
    echo "============ v22-label progress @ $(date '+%F %T') done=$DONE/${#TASK_CORES[@]} running=${#GPU_PID[@]} ============"
    for G in "${GPU_ARR[@]}"; do
      local pid="${GPU_PID[$G]:-}"
      [[ -n "$pid" ]] || continue
      local C="${GPU_CORE[$G]}"
      local W="${GPU_WORKLOAD[$G]}"
      local LOG="${GPU_LOG[$G]}"
      local LINE=""
      LINE=$(grep -E "^\\s*\\[$W\\] " "$LOG" 2>/dev/null | tail -n 1 || true)
      if [[ -z "$LINE" ]]; then
        LINE=$(tail -n 1 "$LOG" 2>/dev/null || true)
      fi
      printf "gpu=%-2s c%-2s %-26s %s\n" "$G" "$C" "$W" "$LINE"
    done
  done
}

if [[ "$PROGRESS_EVERY" != "0" ]]; then
  progress_loop &
  PROGRESS_PID=$!
fi

for G in "${GPU_ARR[@]}"; do
  if [[ "$NEXT_TASK" -ge "${#TASK_CORES[@]}" ]]; then
    break
  fi
  launch_task "$G" "$NEXT_TASK"
  NEXT_TASK=$((NEXT_TASK + 1))
done

while true; do
  RUNNING=0
  for G in "${GPU_ARR[@]}"; do
    pid="${GPU_PID[$G]:-}"
    [[ -n "$pid" ]] || continue
    if kill -0 "$pid" 2>/dev/null; then
      RUNNING=$((RUNNING + 1))
      continue
    fi

    rc=0
    wait "$pid" 2>/dev/null || rc=$?
    C="${GPU_CORE[$G]}"
    W="${GPU_WORKLOAD[$G]}"
    LOG="${GPU_LOG[$G]}"
    if [[ "$rc" -eq 0 ]]; then
      DONE=$((DONE + 1))
      echo "[v22-label] done c${C}/$W ($DONE/${#TASK_CORES[@]})"
    else
      FAILS=$((FAILS + 1))
      DONE=$((DONE + 1))
      echo "[v22-label][error] failed c${C}/$W rc=$rc log=$LOG" >&2
      tail -80 "$LOG" >&2 || true
    fi

    unset "GPU_PID[$G]"
    unset "GPU_CORE[$G]"
    unset "GPU_WORKLOAD[$G]"
    unset "GPU_LOG[$G]"

    if [[ "$NEXT_TASK" -lt "${#TASK_CORES[@]}" ]]; then
      launch_task "$G" "$NEXT_TASK"
      NEXT_TASK=$((NEXT_TASK + 1))
      RUNNING=$((RUNNING + 1))
    fi
  done

  if [[ "$RUNNING" -eq 0 && "$NEXT_TASK" -ge "${#TASK_CORES[@]}" ]]; then
    break
  fi
  sleep 5
done

if [[ -n "$PROGRESS_PID" ]]; then
  kill "$PROGRESS_PID" 2>/dev/null || true
  wait "$PROGRESS_PID" 2>/dev/null || true
  PROGRESS_PID=""
fi

echo
echo "[v22-label] eval tasks done $(date '+%F %T') fails=$FAILS"

"$PY" - "$OUT_ROOT" "$CORES" "$WORKLOADS" <<'PY'
import glob
import json
import math
import os
import re
import statistics as st
import sys

out_root, cores_s, workloads_s = sys.argv[1:4]
cores = cores_s.split()
workloads = workloads_s.split()

def load_summary(path):
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except FileNotFoundError:
        return None
    pos = text.rfind("Summary")
    if pos < 0:
        return None
    start = text.find("[", pos)
    if start < 0:
        return None
    try:
        arr, _ = json.JSONDecoder().raw_decode(text[start:])
    except Exception:
        return None
    if isinstance(arr, list) and arr:
        return arr[0]
    return None

def pct(v):
    if v is None:
        return ""
    try:
        f = float(v) * 100.0
    except Exception:
        return ""
    if not math.isfinite(f):
        return ""
    return f"{f:.6g}"

def raw(v):
    if v is None:
        return ""
    if isinstance(v, float) and not math.isfinite(v):
        return ""
    return str(v)

def latest_pred_dir(core):
    pats = sorted(glob.glob(f"logs/eval_parallel_v22_bind_split_direct_best_c{core}_full_*"),
                  key=os.path.getmtime, reverse=True)
    return pats[0] if pats else ""

all_label = []
all_cmp = []

for core in cores:
    label_rows = []
    for workload in workloads:
        log = os.path.join(out_root, f"c{core}", "run_logs", f"{workload}.log")
        obj = load_summary(log) or {}
        row = {
            "core": f"c{core}",
            "workload": workload,
            "windows": obj.get("windows"),
            "pred_cpi_uop": obj.get("pred_cpi_uop"),
            "label_cpi_uop": obj.get("label_cpi_uop"),
            "roi_cpi_uop": obj.get("roi_stats_cpi_uop"),
            "pred_vs_label_pct": pct(obj.get("pred_vs_label_cpi_uop")),
            "pred_vs_roi_pct": pct(obj.get("pred_vs_roi_cpi_uop")),
            "label_vs_roi_pct": pct(obj.get("label_vs_roi_cpi_uop")),
            "win_mape_pct": pct(obj.get("win_mape_cpi_uop")),
            "log": log,
            "_obj": obj,
        }
        label_rows.append(row)
        all_label.append(row)

    label_path = os.path.join(out_root, f"c{core}_label_summary.tsv")
    label_cols = [
        "core", "workload", "windows", "pred_cpi_uop", "label_cpi_uop",
        "roi_cpi_uop", "pred_vs_label_pct", "pred_vs_roi_pct",
        "label_vs_roi_pct", "win_mape_pct", "log",
    ]
    with open(label_path, "w", encoding="utf-8") as fh:
        fh.write("\t".join(label_cols) + "\n")
        for r in label_rows:
            fh.write("\t".join(raw(r.get(c)) for c in label_cols) + "\n")

    pred_dir = os.environ.get(f"PRED_C{core}_DIR") or latest_pred_dir(core)
    cmp_rows = []
    for r in label_rows:
        workload = r["workload"]
        pred_log = os.path.join(pred_dir, f"{workload}.log") if pred_dir else ""
        pred = load_summary(pred_log) or {}
        try:
            pred_err = float(pred.get("pred_vs_label_cpi_uop")) * 100.0
        except Exception:
            pred_err = float("nan")
        try:
            label_err = float(r["_obj"].get("pred_vs_label_cpi_uop")) * 100.0
        except Exception:
            label_err = float("nan")
        delta = label_err - pred_err if math.isfinite(pred_err) and math.isfinite(label_err) else float("nan")
        cmp = {
            "core": f"c{core}",
            "workload": workload,
            "pred_cut_err_label_pct": "" if not math.isfinite(pred_err) else f"{pred_err:.6g}",
            "label_cut_err_label_pct": "" if not math.isfinite(label_err) else f"{label_err:.6g}",
            "label_minus_pred_pp": "" if not math.isfinite(delta) else f"{delta:.6g}",
            "pred_cut_pred_cpi": pred.get("pred_cpi_uop", ""),
            "label_cut_pred_cpi": r["_obj"].get("pred_cpi_uop", ""),
            "pred_cut_label_cpi": pred.get("label_cpi_uop", ""),
            "label_cut_label_cpi": r["_obj"].get("label_cpi_uop", ""),
            "pred_cut_windows": pred.get("windows", ""),
            "label_cut_windows": r["_obj"].get("windows", ""),
            "pred_log": pred_log,
            "label_log": r["log"],
        }
        cmp_rows.append(cmp)
        all_cmp.append(cmp)

    cmp_path = os.path.join(out_root, f"c{core}_pred_vs_labelcut_comparison.tsv")
    cmp_cols = [
        "core", "workload", "pred_cut_err_label_pct", "label_cut_err_label_pct",
        "label_minus_pred_pp", "pred_cut_pred_cpi", "label_cut_pred_cpi",
        "pred_cut_label_cpi", "label_cut_label_cpi", "pred_cut_windows",
        "label_cut_windows", "pred_log", "label_log",
    ]
    with open(cmp_path, "w", encoding="utf-8") as fh:
        fh.write("\t".join(cmp_cols) + "\n")
        for r in cmp_rows:
            fh.write("\t".join(raw(r.get(c)) for c in cmp_cols) + "\n")

all_label_path = os.path.join(out_root, "all_label_summary.tsv")
label_cols = [
    "core", "workload", "windows", "pred_cpi_uop", "label_cpi_uop",
    "roi_cpi_uop", "pred_vs_label_pct", "pred_vs_roi_pct",
    "label_vs_roi_pct", "win_mape_pct", "log",
]
with open(all_label_path, "w", encoding="utf-8") as fh:
    fh.write("\t".join(label_cols) + "\n")
    for r in all_label:
        fh.write("\t".join(raw(r.get(c)) for c in label_cols) + "\n")

all_cmp_path = os.path.join(out_root, "all_pred_vs_labelcut_comparison.tsv")
cmp_cols = [
    "core", "workload", "pred_cut_err_label_pct", "label_cut_err_label_pct",
    "label_minus_pred_pp", "pred_cut_pred_cpi", "label_cut_pred_cpi",
    "pred_cut_label_cpi", "label_cut_label_cpi", "pred_cut_windows",
    "label_cut_windows", "pred_log", "label_log",
]
with open(all_cmp_path, "w", encoding="utf-8") as fh:
    fh.write("\t".join(cmp_cols) + "\n")
    for r in all_cmp:
        fh.write("\t".join(raw(r.get(c)) for c in cmp_cols) + "\n")

print(f"[summary] wrote {all_label_path}")
print(f"[summary] wrote {all_cmp_path}")
print()
print("quick comparison: mean pred-vs-label error, pred-cut vs label-cut")
for core in cores:
    rows = [r for r in all_cmp if r["core"] == f"c{core}"]
    pred_errs = [float(r["pred_cut_err_label_pct"]) for r in rows if r["pred_cut_err_label_pct"]]
    label_errs = [float(r["label_cut_err_label_pct"]) for r in rows if r["label_cut_err_label_pct"]]
    deltas = [float(r["label_minus_pred_pp"]) for r in rows if r["label_minus_pred_pp"]]
    if pred_errs and label_errs:
        print(
            f"c{core}: pred_mean={sum(pred_errs)/len(pred_errs):.2f}% "
            f"label_mean={sum(label_errs)/len(label_errs):.2f}% "
            f"delta_mean={sum(deltas)/len(deltas):+.2f}pp "
            f"label_median={st.median(label_errs):.2f}%"
        )
print()
print("worst label-cut rows:")
def key_err(r):
    try:
        return float(r["label_cut_err_label_pct"])
    except Exception:
        return -1.0
for r in sorted(all_cmp, key=key_err, reverse=True)[:20]:
    print(
        f"{r['core']:>3s} {r['workload']:<24s} "
        f"pred={r['pred_cut_err_label_pct']:>8s}% "
        f"label={r['label_cut_err_label_pct']:>8s}% "
        f"delta={r['label_minus_pred_pp']:>8s}pp "
        f"wins={r['label_cut_windows']}"
    )
PY

if [[ "$FAILS" -ne 0 ]]; then
  echo "[v22-label][error] failed tasks=$FAILS" >&2
  exit 1
fi

echo
echo "[v22-label] complete $(date '+%F %T')"
echo "[v22-label] out_root=$OUT_ROOT"
echo "[v22-label] summary=$OUT_ROOT/all_label_summary.tsv"
echo "[v22-label] comparison=$OUT_ROOT/all_pred_vs_labelcut_comparison.tsv"

trap - EXIT INT TERM
