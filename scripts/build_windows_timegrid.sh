#!/usr/bin/env bash
# 方案C（时间网格连续切窗，无采样偏差 + 跨核对齐）数据集构建。
#
# 用 build_samples_timewin：按全局时间区间 [t_lo+k·ΔT, t_lo+(k+1)·ΔT) 连续切，
# stride=ΔT 不重叠 → 每条指令恰进一个窗口 → 全程CPI还原无偏（修复方案A的stream高估）。
# 各核取同一时间区间指令，指令数自然可变 → 跨核物理时间对齐。
#
# ΔT=2000 cycle, tick_per_cycle≈333 → dt_tick=666000。MAXLEN=8192。
# max_per_core 截断每核段，保证 8核×N×6token + 结构 < MAXLEN。
#
# 用法: bash scripts/build_windows_timegrid.sh
# 可调: DT_CYCLE(默认2000) TPC(默认333) MAX_PER_CORE(默认160) MAXLEN(默认8192)
#       JOBS(默认8) WINDOW_OUT RAW_MERGED WORKLOADS
set -euo pipefail

PY=/data00/yinhaolang/infer/.venv/bin/python
ROOT=/data00/yinhaolang/LLMSim
cd "$ROOT"

DT_CYCLE=${DT_CYCLE:-2000}
TPC=${TPC:-333}
DT_TICK=$(( DT_CYCLE * TPC ))
MAX_PER_CORE=${MAX_PER_CORE:-160}
MAXLEN=${MAXLEN:-8192}
JOBS=${JOBS:-8}
RELINK_RAW=${RELINK_RAW:-1}
PREPARE_CACHE=${PREPARE_CACHE:-1}

RAW_MERGED=${RAW_MERGED:-$ROOT/data/raw_timegrid}
WINDOW_OUT=${WINDOW_OUT:-$ROOT/data/windows_timegrid_dt2000}

WORKLOADS_STR=${WORKLOADS:-"W_branch_storm W_chase_dram W_compute_int W_false_sharing W_indirect W_int_div W_phased_mix W_stream"}
read -r -a WORKLOADS <<< "$WORKLOADS_STR"

echo "[prep] DT_CYCLE=$DT_CYCLE TPC=$TPC -> DT_TICK=$DT_TICK"
echo "[prep] MAX_PER_CORE=$MAX_PER_CORE MAXLEN=$MAXLEN JOBS=$JOBS"
echo "[prep] WINDOW_OUT=$WINDOW_OUT"
echo "[prep] WORKLOADS=${WORKLOADS[*]}"

mkdir -p "$RAW_MERGED" "$WINDOW_OUT" logs

if [[ "$RELINK_RAW" == "1" ]]; then
  rm -rf "$RAW_MERGED"; mkdir -p "$RAW_MERGED"
  declare -A SRC=(
    [W_branch_storm]="$ROOT/data/raw_fix3_8c_500k/W_branch_storm"
    [W_chase_dram]="$ROOT/data/raw_8w_8c_500k/W_chase_dram"
    [W_compute_int]="$ROOT/data/raw_8w_8c_500k/W_compute_int"
    [W_false_sharing]="$ROOT/data/raw_8w_8c_500k/W_false_sharing"
    [W_indirect]="$ROOT/data/raw_8w_8c_500k/W_indirect"
    [W_int_div]="$ROOT/data/raw_fix3_8c_500k/W_int_div"
    [W_phased_mix]="$ROOT/data/raw_fix3_8c_500k/W_phased_mix"
    [W_stream]="$ROOT/data/raw_8w_8c_500k/W_stream"
  )
  for w in "${WORKLOADS[@]}"; do
    [[ -z "${SRC[$w]:-}" ]] && { echo "[err] unknown workload $w" >&2; exit 1; }
    ln -sfn "${SRC[$w]}" "$RAW_MERGED/$w"
  done
fi
echo "[prep] linked:"; ls -1 "$RAW_MERGED"

# 时间网格连续切窗（dt_tick>0 走 build_samples_timewin）
"$PY" data/build_windows.py \
  --raw "$RAW_MERGED" \
  --out "$WINDOW_OUT" \
  --dt-tick "$DT_TICK" \
  --max-per-core "$MAX_PER_CORE" \
  --jobs "$JOBS" \
  --workloads "${WORKLOADS[@]}"

if [[ "$PREPARE_CACHE" == "1" ]]; then
  "$PY" scripts/prepare_dataset_cache.py \
    --data "$WINDOW_OUT/windows.jsonl" \
    --max-len "$MAXLEN" \
    --jobs "$JOBS"
fi

# sanity: 窗口数 + 每核指令数分布（方案C应可变）
"$PY" - "$WINDOW_OUT/windows.jsonl" <<'PYEOF'
import json, sys, statistics as st
from pathlib import Path
p = Path(sys.argv[1])
total=0; per={}; splits=[]
for ln in p.open():
    s=ln.strip()
    if not s.startswith("{"): continue
    r=json.loads(s); total+=1
    per[r["workload"]]=per.get(r["workload"],0)+1
    splits.extend(r["core_split"])
print(f"[sanity] total={total}")
for w in sorted(per): print(f"[sanity]   {w}: {per[w]}")
if splits:
    print(f"[sanity] 每核指令数(方案C应可变): min={min(splits)} "
          f"med={st.median(splits)} p90={sorted(splits)[int(len(splits)*0.9)]} "
          f"max={max(splits)}")
PYEOF

echo "[done] timegrid dataset ready: $WINDOW_OUT/windows.jsonl"
