#!/usr/bin/env bash
# Warmup A/B 对比：用 3 个 holdout workload 同时跑 warmup-dt=0 和 warmup-dt=100000，
# 输出 shared_system PMU snapshot 用于比较 cold-start 误差 vs warm-state 误差。
#
# 用法：
#   CKPT=ckpt/quota_32k_balanced_v1 bash scripts/eval_warmup_ab.sh
set -euo pipefail

ROOT=/data00/yinhaolang/LLMSim
cd "$ROOT"

CKPT=${CKPT:?"need CKPT=ckpt/xxx"}
RAW=${RAW:-data/raw_eval11_8c}
WARMUP_DT=${WARMUP_DT:-100000}
MAX_WINDOWS=${MAX_WINDOWS:-20}
DT_TARGET=${DT_TARGET:-8000}
DT_MAX=${DT_MAX:-12000}
MAX_LEN=${MAX_LEN:-32768}
PY=${PY:-/data00/yinhaolang/infer/.venv/bin/python}

WORKLOADS=(W_ads_ctr W_feed_ranking W_interest_graph_recall)

TAG=${TAG:-$(basename "$CKPT")}
TS=$(date +%Y%m%d_%H%M%S)
LOGDIR="logs/warmup_ab_${TAG}_${TS}"
mkdir -p "$LOGDIR"

echo "[meta] CKPT=$CKPT WARMUP_DT=$WARMUP_DT MAX_WINDOWS=$MAX_WINDOWS"
echo "[meta] LOGDIR=$LOGDIR"

GPU=0
PIDS=()
launch() {
  local W=$1 DT=$2 GPU=$3
  local tag
  if (( DT == 0 )); then
    tag="cold"
  else
    tag="warm${DT}"
  fi
  local LOG="$LOGDIR/${W}_${tag}.log"
  local EVDIR="$LOGDIR/${W}_${tag}_events"
  local PMUDIR="$LOGDIR/${W}_${tag}_pmu"
  mkdir -p "$EVDIR" "$PMUDIR"
  echo "[launch] gpu=$GPU $W $tag dt=$DT -> $LOG"
  HF_HUB_OFFLINE=1 CUDA_VISIBLE_DEVICES="$GPU" \
    "$PY" eval/eval_quota_cycles.py \
      --raw-root "$RAW" \
      --workload "$W" \
      --ckpt "$CKPT" \
      --dt-target "$DT_TARGET" --dt-max "$DT_MAX" \
      --max-len "$MAX_LEN" \
      --max-windows "$MAX_WINDOWS" \
      --warmup-dt "$DT" \
      --emit-mem-events-dir "$EVDIR" \
      --shared-system-out-dir "$PMUDIR" \
      </dev/null > "$LOG" 2>&1 &
  PIDS+=("$!:$W:$tag")
}

for W in "${WORKLOADS[@]}"; do
  launch "$W" 0 "$GPU"; GPU=$((GPU+1))
  launch "$W" "$WARMUP_DT" "$GPU"; GPU=$((GPU+1))
done

FAILS=0
for entry in "${PIDS[@]}"; do
  pid=${entry%%:*}
  rest=${entry#*:}
  name=${rest%%:*}
  tag=${rest#*:}
  if wait "$pid"; then
    echo "[done] $name $tag"
  else
    echo "[FAIL] $name $tag pid=$pid log=$LOGDIR/${name}_${tag}.log"
    FAILS=$((FAILS+1))
  fi
done

echo
echo "============ shared_system batch (cold runs already produced shared_pmu via inline subprocess? no) ============"
echo "NOTE: eval_quota_cycles.py 默认不启动 shared_system 子进程；mem events 已经写到 *_events/。"
echo "下面用 run_shared_system.py 离线再跑一次。"

for W in "${WORKLOADS[@]}"; do
  for tag in cold warm${WARMUP_DT}; do
    EVDIR="$LOGDIR/${W}_${tag}_events"
    PMUDIR="$LOGDIR/${W}_${tag}_pmu"
    LOG="$LOGDIR/${W}_${tag}_shared.log"
    if compgen -G "$EVDIR/*.mem_events.jsonl" > /dev/null; then
      echo "[shared] $W $tag -> $PMUDIR"
      "$PY" shared_system/run_shared_system.py \
        --events-dir "$EVDIR" \
        --out-dir "$PMUDIR" \
        --snapshot-interval 0 > "$LOG" 2>&1 || {
          echo "[FAIL shared] $W $tag log=$LOG"; FAILS=$((FAILS+1));
        }
    else
      echo "[skip shared] no mem events for $W $tag"
    fi
  done
done

echo
echo "============ summary ============"
"$PY" - "$LOGDIR" "$WARMUP_DT" <<'PYEOF'
import json, os, sys
logdir, wdt = sys.argv[1], sys.argv[2]
workloads = ["W_ads_ctr", "W_feed_ranking", "W_interest_graph_recall"]
tags = ["cold", f"warm{wdt}"]

print(f"{'workload':<28} {'tag':<10} {'roi_cpi':>8} {'shared_mr_llc':>14} {'shared_mr_l1d_ld':>17} {'shared_mr_l1d_st':>17}")
print("-"*100)

def last_snapshot(pmu_jsonl):
    last = None
    if not os.path.isfile(pmu_jsonl): return None
    with open(pmu_jsonl) as f:
        for ln in f:
            ln=ln.strip()
            if not ln: continue
            try:
                obj = json.loads(ln)
            except Exception:
                continue
            if obj.get("event_type") == "pmu_snapshot":
                last = obj
    return last

def load_eval(log_path):
    if not os.path.isfile(log_path): return None
    txt = open(log_path).read()
    pos = txt.rfind("Summary")
    if pos < 0: return None
    s = txt.find("[", pos)
    if s < 0: return None
    try:
        arr, _ = json.JSONDecoder().raw_decode(txt[s:])
    except Exception:
        return None
    return arr[0] if arr else None

for w in workloads:
    for tag in tags:
        eval_log = os.path.join(logdir, f"{w}_{tag}.log")
        pmu_glob = [os.path.join(logdir, f"{w}_{tag}_pmu", fn)
                    for fn in os.listdir(os.path.join(logdir, f"{w}_{tag}_pmu"))
                    if fn.endswith(".shared_pmu.jsonl")] \
                    if os.path.isdir(os.path.join(logdir, f"{w}_{tag}_pmu")) else []
        snap = last_snapshot(pmu_glob[0]) if pmu_glob else None
        ev = load_eval(eval_log) or {}
        roi_cpi = ev.get("roi_stats_cpi")
        rates = (snap or {}).get("rates", {})
        print(f"{w:<28} {tag:<10} "
              f"{roi_cpi if roi_cpi is None else f'{roi_cpi:.4f}':>8} "
              f"{rates.get('mr_llc', float('nan')):>14.6f} "
              f"{rates.get('mr_l1d_ld', float('nan')):>17.6f} "
              f"{rates.get('mr_l1d_st', float('nan')):>17.6f}")
    print()
PYEOF

if (( FAILS > 0 )); then
  echo "[final] failed=$FAILS"
  exit 1
fi
echo "[final] all done"
