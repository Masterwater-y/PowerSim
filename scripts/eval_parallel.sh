#!/usr/bin/env bash
# 并行 quota-cycle 部署侧评估：每张 GPU 一个进程，跑完一个再排下一个。
# 实时把每个 workload 的最新一行进度滚动打印出来。
#
# 用法：
#   CKPT=ckpt/v7_c08_absmiss_ddp8 bash scripts/eval_parallel.sh
#   CKPT=ckpt/xxx WORKLOADS="W_ads_ctr W_phased_mix" bash scripts/eval_parallel.sh
#
# 主要参数（环境变量）：
#   CKPT            必填，待评估的 ckpt 目录
#   RAW             默认 data/raw_v7_seedA_c08，v7 8c raw trace 根目录
#   WORKLOADS       默认 v7 的 17 个负载
#   GPUS            默认 0,1,2,3,4,5,6,7
#   DT_TARGET       默认 8000
#   DT_MAX          默认 12000
#   MAX_LEN         默认 32768
#   MAX_WINDOWS     默认 0（全量）；调试时可设 2/10
#   DEVICE          默认自动选择；可设 cuda/cpu
#   TAG             默认基于 CKPT 自动生成
#   PROGRESS_EVERY  默认 30s 刷新一次进度
set -euo pipefail

ROOT=/data00/yinhaolang/LLMSim
cd "$ROOT"

CKPT=${CKPT:?"need CKPT=ckpt/xxx"}
RAW=${RAW:-data/raw_v7_seedA_c08}
DT_TARGET=${DT_TARGET:-8000}
DT_MAX=${DT_MAX:-12000}
MAX_LEN=${MAX_LEN:-32768}
MAX_WINDOWS=${MAX_WINDOWS:-0}
DEVICE=${DEVICE:-}
PROGRESS_EVERY=${PROGRESS_EVERY:-30}
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}

DEFAULT_WORKLOADS=(
  W_ads_ctr W_ads_ranking_proxy W_branch_storm W_chase_dram
  W_compute_int W_false_sharing W_feed_ranking W_fp_compute_dense
  W_fp_lite W_graph_recall_proxy W_indirect W_int_div
  W_interest_graph_recall W_mlp_light W_phased_mix
  W_search_index_proxy W_stream
)
read -r -a WORKLOADS <<< "${WORKLOADS:-${DEFAULT_WORKLOADS[*]}}"

IFS=',' read -r -a GPUS <<< "${GPUS:-0,1,2,3,4,5,6,7}"

TAG=${TAG:-$(basename "$CKPT")}
TS=$(date +%Y%m%d_%H%M%S)
LOGDIR="logs/eval_parallel_${TAG}_${TS}"
mkdir -p "$LOGDIR"

echo "[meta] CKPT=$CKPT"
echo "[meta] RAW=$RAW"
echo "[meta] GPUS=${GPUS[*]}"
echo "[meta] WORKLOADS=${WORKLOADS[*]}"
echo "[meta] MAX_WINDOWS=$MAX_WINDOWS"
echo "[meta] DEVICE=${DEVICE:-auto}"
echo "[meta] LOGDIR=$LOGDIR"
echo

# ---------- launcher 用文件锁实现 N 卡 M 任务的工作池 ----------
declare -A GPU_PID
declare -A GPU_WORKLOAD
FAILS=0

launch_one() {
  local W=$1
  local GPU=$2
  local LOG="$LOGDIR/${W}.log"
  local OUT="$LOGDIR/${W}.json"
  local -a device_args=()
  if [[ -n "$DEVICE" ]]; then
    device_args=(--device "$DEVICE")
  fi
  echo "[launch] gpu=$GPU workload=$W -> $LOG"
  HF_HUB_OFFLINE=1 CUDA_VISIBLE_DEVICES="$GPU" \
    "$PY" eval/eval_quota_cycles.py \
      --raw-root "$RAW" \
      --workload "$W" \
      --ckpt "$CKPT" \
      --dt-target "$DT_TARGET" --dt-max "$DT_MAX" \
      --max-len "$MAX_LEN" \
      --max-windows "$MAX_WINDOWS" \
      "${device_args[@]}" \
      </dev/null > "$LOG" 2>&1 &
  GPU_PID[$GPU]=$!
  GPU_WORKLOAD[$GPU]=$W
}

# 任务队列
QUEUE=("${WORKLOADS[@]}")

# 初始化每张卡上一项任务
for G in "${GPUS[@]}"; do
  if [[ ${#QUEUE[@]} -eq 0 ]]; then break; fi
  W=${QUEUE[0]}; QUEUE=("${QUEUE[@]:1}")
  launch_one "$W" "$G"
done

# 进度刷新器：周期性打印每个 workload 最新一行 progress
progress_loop() {
  while true; do
    sleep "$PROGRESS_EVERY"
    echo
    echo "============ progress @ $(date +%H:%M:%S) ============"
    for f in "$LOGDIR"/*.log; do
      [[ -f "$f" ]] || continue
      W=$(basename "$f" .log)
      LINE=$(grep -E "^\s*\[$W\] " "$f" 2>/dev/null | tail -n 1)
      if [[ -z "$LINE" ]]; then
        # 还在 init 阶段
        LINE=$(tail -n 1 "$f")
      fi
      printf "%-28s %s\n" "$W" "$LINE"
    done
  done
}

progress_loop &
PROG_PID=$!

# 工作池：等待任意进程结束，则把队列里下一个任务派给这张卡
trap 'kill $PROG_PID 2>/dev/null || true' EXIT

while true; do
  # 是否还有任务在跑
  RUNNING=0
  for G in "${!GPU_PID[@]}"; do
    PID=${GPU_PID[$G]}
    if kill -0 "$PID" 2>/dev/null; then
      RUNNING=$((RUNNING + 1))
    else
      RC=0
      wait "$PID" 2>/dev/null || RC=$?
      if [[ $RC -ne 0 ]]; then
        echo "[error] gpu=$G workload=${GPU_WORKLOAD[$G]:-unknown} exit=$RC log=$LOGDIR/${GPU_WORKLOAD[$G]:-unknown}.log"
        FAILS=$((FAILS + 1))
      fi
      unset "GPU_PID[$G]"
      unset "GPU_WORKLOAD[$G]"
      if [[ ${#QUEUE[@]} -gt 0 ]]; then
        W=${QUEUE[0]}; QUEUE=("${QUEUE[@]:1}")
        launch_one "$W" "$G"
        RUNNING=$((RUNNING + 1))
      fi
    fi
  done
  if [[ $RUNNING -eq 0 && ${#QUEUE[@]} -eq 0 ]]; then
    break
  fi
  sleep 5
done

kill $PROG_PID 2>/dev/null || true
echo
echo "============ all done @ $(date +%H:%M:%S) ============"
echo "logs in $LOGDIR"

# 用 python 把每个 workload 日志里的关键指标抽出来，整成一张对齐表落到末尾
"$PY" - "$LOGDIR" "$CKPT" <<'PYEOF'
import os, re, sys, json, glob
import math

logdir, ckpt = sys.argv[1], sys.argv[2]
files = sorted(glob.glob(os.path.join(logdir, "W_*.log")))

def grab(text, pat, cast=float):
    m = re.search(pat, text)
    if not m:
        return None
    try:
        return cast(m.group(1))
    except Exception:
        return None

def load_eval_summary(text):
    marker = "Summary"
    pos = text.rfind(marker)
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

rows = []
for fp in files:
    name = os.path.basename(fp)[:-4]
    with open(fp) as f:
        s = f.read()
    obj = load_eval_summary(s) or {}
    row = {
        "workload": name,
        "windows": obj.get("windows", grab(s, r"\(windows=(\d+)\)", int)),
        "pred_cpi": obj.get("pred_cpi_uop", obj.get("pred_cpi", grab(s, r"cpi_uop\s+pred\s*=\s*([0-9.]+)"))),
        "label_cpi": obj.get("label_cpi_uop", obj.get("label_cpi", grab(s, r"cpi_uop\s+label\s*=\s*([0-9.]+)"))),
        "roi_cpi": obj.get("roi_stats_cpi_uop", obj.get("roi_stats_cpi", grab(s, r"cpi_uop\s+roi\s*=\s*([0-9.]+)"))),
        "gem5_cpi": obj.get("gem5_cpi_macro", obj.get("gem5_cpi", grab(s, r"cpi_macro\s+gem5\s*=\s*([0-9.]+)"))),
        "err_pred_label": (obj.get("pred_vs_label_cpi_uop") * 100 if obj.get("pred_vs_label_cpi_uop") is not None else (obj.get("pred_vs_label") * 100 if obj.get("pred_vs_label") is not None else grab(s, r"误差 cpi_uop\s+pred vs label\s*=\s*([0-9.\-]+)%"))),
        "err_pred_roi": (obj.get("pred_vs_roi_cpi_uop") * 100 if obj.get("pred_vs_roi_cpi_uop") is not None else (obj.get("pred_vs_roi_stats") * 100 if obj.get("pred_vs_roi_stats") is not None else grab(s, r"误差 cpi_uop\s+pred vs ROI\s*=\s*([0-9.\-]+)%"))),
        "ref_label_roi": (obj.get("label_vs_roi_cpi_uop") * 100 if obj.get("label_vs_roi_cpi_uop") is not None else (obj.get("label_vs_roi_stats") * 100 if obj.get("label_vs_roi_stats") is not None else grab(s, r"参考 cpi_uop\s+label vs ROI\s*=\s*([0-9.\-]+)%"))),
        "ref_gem5_roi": (obj.get("gem5_full_vs_roi_cpi_macro") * 100 if obj.get("gem5_full_vs_roi_cpi_macro") is not None else (obj.get("gem5_full_vs_roi_stats") * 100 if obj.get("gem5_full_vs_roi_stats") is not None else grab(s, r"参考 cpi_macro\s+gem5 vs ROI\s*=\s*([0-9.\-]+)%"))),
        "win_mape": (obj.get("win_mape_cpi_uop") * 100 if obj.get("win_mape_cpi_uop") is not None else (obj.get("win_mape") * 100 if obj.get("win_mape") is not None else grab(s, r"per-window cpi_uop MAPE\s*=\s*([0-9.\-]+)%"))),
        "pmu_global": obj.get("pmu_global", {}),
    }
    rows.append(row)

print()
print("=" * 110)
print(f"FINAL SUMMARY  ckpt={ckpt}  workloads={len(rows)}")
print("=" * 110)
hdr = f"{'workload':<26} {'win':>5}  {'pred':>8} {'label':>8} {'roi':>8} {'gem5':>8}  " \
      f"{'pVl%':>7} {'pVr%':>7} {'lVr%':>7} {'gVr%':>7}  {'mape%':>7}"
print(hdr)
print("-" * 110)
def fmt(v, w, p):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return " " * w + "-"
    return f"{v:>{w}.{p}f}"
ok = lambda v: 0.0 if v is None else v
agg_err = []
for r in rows:
    line = (
        f"{r['workload']:<26} "
        f"{(r['windows'] or 0):>5d}  "
        f"{fmt(r['pred_cpi'], 8, 4)} {fmt(r['label_cpi'], 8, 4)} "
        f"{fmt(r['roi_cpi'], 8, 4)} {fmt(r['gem5_cpi'], 8, 4)}  "
        f"{fmt(r['err_pred_label'], 7, 2)} {fmt(r['err_pred_roi'], 7, 2)} "
        f"{fmt(r['ref_label_roi'], 7, 2)} {fmt(r['ref_gem5_roi'], 7, 2)}  "
        f"{fmt(r['win_mape'], 7, 2)}"
    )
    print(line)
    if r["err_pred_roi"] is not None:
        agg_err.append(r["err_pred_roi"])
print("-" * 110)
if agg_err:
    print(f"{'AGG':<26} {'':>5}  {'':>8} {'':>8} {'':>8} {'':>8}  "
          f"{'':>7} {sum(agg_err)/len(agg_err):>7.2f} {'':>7} {'':>7}  {'':>7}  "
          f"(mean pred-vs-ROI)")
print("=" * 110)
print()
print("=" * 132)
print("FINAL PMU SUMMARY  values=global aggregated PMU, errors in %, winMAPE=per-window pred-vs-label MAPE")
print("=" * 132)
hdr = f"{'workload':<26} {'pmu':<14} {'pred':>10} {'label':>10} {'roi':>10} {'gem5':>10} " \
      f"{'pVl%':>8} {'pVr%':>8} {'lVr%':>8} {'gVr%':>8} {'winMAPE%':>9}"
print(hdr)
print("-" * 132)
pmu_err = {}
for r in rows:
    for k, m in (r.get("pmu_global") or {}).items():
        def val(name):
            return m.get(name)
        def pct(name):
            v = m.get(name)
            return None if v is None else v * 100
        print(
            f"{r['workload']:<26} {k:<14} "
            f"{fmt(val('pred'), 10, 4)} {fmt(val('label'), 10, 4)} "
            f"{fmt(val('roi'), 10, 4)} {fmt(val('gem5'), 10, 4)} "
            f"{fmt(pct('pred_vs_label'), 8, 2)} {fmt(pct('pred_vs_roi'), 8, 2)} "
            f"{fmt(pct('label_vs_roi'), 8, 2)} {fmt(pct('gem5_vs_roi'), 8, 2)} "
            f"{fmt(pct('window_mape'), 9, 2)}"
        )
        e = pct("pred_vs_roi")
        if e is not None and not (isinstance(e, float) and math.isnan(e)):
            pmu_err.setdefault(k, []).append(e)
print("-" * 132)
for k in sorted(pmu_err):
    xs = pmu_err[k]
    print(f"PMU_AGG {'mean_pred_vs_roi':<20} {k:<14} {sum(xs)/len(xs):.2f}%  n={len(xs)}")
print("=" * 132)
print()
print("RAW JSON:")
print(json.dumps(rows, ensure_ascii=False, indent=2))
PYEOF

if [[ $FAILS -ne 0 ]]; then
  echo "[final] failed workloads=$FAILS"
  exit 1
fi
