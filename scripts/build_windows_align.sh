#!/usr/bin/env bash
# 方案A（固定每核指令数 N + 近似跨核时间对齐）数据集构建。
#
# 全局时间锚点 T_k 推进，每核取 commit_tick>=T_k 的连续 N 条 µop。
# 各核段恒为 N 条（上下文均等），且都从物理时刻 T_k 附近起步（近似对齐）。
#
# 用法：
#   bash scripts/build_windows_align.sh
# 可调环境变量：
#   ALIGN_N(默认160) TARGET_WINDOWS(默认3000) MAXLEN(默认8192)
#   JOBS(默认8) RAW WINDOW_OUT WORKLOADS
set -euo pipefail

PY=/data00/yinhaolang/infer/.venv/bin/python
ROOT=/data00/yinhaolang/LLMSim
cd "$ROOT"

RAW_MERGED=${RAW_MERGED:-$ROOT/data/raw_align}
WINDOW_OUT=${WINDOW_OUT:-$ROOT/data/windows_align_n160}
ALIGN_N=${ALIGN_N:-160}
TARGET_WINDOWS=${TARGET_WINDOWS:-3000}
MAXLEN=${MAXLEN:-8192}
JOBS=${JOBS:-8}
RELINK_RAW=${RELINK_RAW:-1}
PREPARE_CACHE=${PREPARE_CACHE:-1}

# 第一版只用两个对照负载（与 exp_baseline/exp_tstart 同口径）
WORKLOADS_STR=${WORKLOADS:-"W_false_sharing W_compute_int"}
read -r -a WORKLOADS <<< "$WORKLOADS_STR"

echo "[prep] RAW_MERGED=$RAW_MERGED WINDOW_OUT=$WINDOW_OUT"
echo "[prep] ALIGN_N=$ALIGN_N TARGET_WINDOWS=$TARGET_WINDOWS MAXLEN=$MAXLEN JOBS=$JOBS"
echo "[prep] WORKLOADS=${WORKLOADS[*]}"

mkdir -p "$RAW_MERGED" "$WINDOW_OUT" logs

if [[ "$RELINK_RAW" == "1" ]]; then
  rm -rf "$RAW_MERGED"; mkdir -p "$RAW_MERGED"
  # 负载 -> 原始目录映射（与 build_windows_train8.sh 一致）
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
    if [[ -z "${SRC[$w]:-}" ]]; then
      echo "[err] unknown workload $w" >&2; exit 1
    fi
    ln -sfn "${SRC[$w]}" "$RAW_MERGED/$w"
  done
fi

echo "[prep] linked:"; ls -1 "$RAW_MERGED"

# 构建（多负载并行，每负载一进程）
"$PY" data/build_windows.py \
  --raw "$RAW_MERGED" \
  --out "$WINDOW_OUT" \
  --align-n "$ALIGN_N" \
  --target-windows "$TARGET_WINDOWS" \
  --jobs "$JOBS" \
  --workloads "${WORKLOADS[@]}"

if [[ "$PREPARE_CACHE" == "1" ]]; then
  "$PY" scripts/prepare_dataset_cache.py \
    --data "$WINDOW_OUT/windows.jsonl" \
    --max-len "$MAXLEN" \
    --jobs "$JOBS"
fi

# sanity
"$PY" - "$WINDOW_OUT/windows.jsonl" <<'PYEOF'
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
total = 0; per = {}; ncores = set(); splits = []
for ln in p.open():
    s = ln.strip()
    if not s.startswith("{"): continue
    r = json.loads(s); total += 1
    per[r["workload"]] = per.get(r["workload"], 0) + 1
    ncores.add(r["n_core"])
    splits.extend(r["core_split"])
print(f"[sanity] total={total} n_core={sorted(ncores)}")
for w in sorted(per): print(f"[sanity]   {w}: {per[w]}")
import statistics as st
if splits:
    print(f"[sanity] core_split(每核指令数) min={min(splits)} "
          f"med={st.median(splits)} max={max(splits)} "
          f"(方案A应恒等于 N)")
PYEOF

echo "[done] align dataset ready: $WINDOW_OUT/windows.jsonl"
