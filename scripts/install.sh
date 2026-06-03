#!/usr/bin/env bash
# taogen/scripts/install.sh
# 一键准备 taogen 实验环境：
#   1) clone gem5 (v25.1.0.1)
#   2) 应用 gem5_patches/ (TaoTrace + BranchEvents + ROI hook 表达)
#   3) 构建 gem5.opt X86_MESI_Three_Level (96 核 -j)
#   4) 构建 mesi_ref_sim
#   5) 构建 W11-W15 workload 二进制
#
# 用法：
#   bash scripts/install.sh [GEM5_DIR]
# 默认 GEM5_DIR=<workspace>/gem5（与 run_w11_w15_parallel_collect.sh 对齐）
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$(cd "$REPO/.." && pwd)"
GEM5_DIR="${1:-$ROOT/gem5}"
JOBS="${JOBS:-96}"
PYTHON_BIN="${PYTHON:-$(command -v python3.11 || command -v python3)}"
GCC11_LIB="${GCC11_LIB:-/opt/gcc-11/lib64}"
PY38_LIB="${PY38_LIB:-/root/.pyenv/versions/3.8.0/lib}"

echo "[taogen] REPO=$REPO"
echo "[taogen] GEM5_DIR=$GEM5_DIR"
echo "[taogen] JOBS=$JOBS"
echo "[taogen] PYTHON=$PYTHON_BIN"

# ---------- 0) prerequisites ----------
for tool in git scons cmake make gcc g++ "$PYTHON_BIN"; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "[taogen][FATAL] required tool not found: $tool" >&2
    exit 2
  fi
done
"$PYTHON_BIN" - <<'PY'
import importlib.util, sys
missing = [m for m in ("numpy", "pyarrow") if importlib.util.find_spec(m) is None]
if missing:
    print("[taogen][FATAL] missing Python modules: " + ", ".join(missing), file=sys.stderr)
    print("Install them with: python -m pip install numpy pyarrow", file=sys.stderr)
    sys.exit(2)
PY

if [ -d "$GCC11_LIB" ]; then
  export LD_LIBRARY_PATH="$GCC11_LIB:${LD_LIBRARY_PATH:-}"
fi
if [ -d "$PY38_LIB" ]; then
  export LD_LIBRARY_PATH="$PY38_LIB:${LD_LIBRARY_PATH:-}"
fi

# ---------- 1) clone gem5 ----------
if [ ! -d "$GEM5_DIR" ]; then
  echo "[taogen] cloning gem5 v25.1.0.1..."
  git clone --depth 1 --branch v25.1.0.1 https://github.com/gem5/gem5.git "$GEM5_DIR"
fi

# ---------- 2) 应用 patch ----------
echo "[taogen] applying gem5_patches/ -> $GEM5_DIR"
mkdir -p "$GEM5_DIR/build_opts"
cp -v "$REPO/gem5_patches/build_opts/X86_MESI_Three_Level" "$GEM5_DIR/build_opts/"
cp -v "$REPO/gem5_patches/src/cpu/o3/probe/BranchEvents.py" "$GEM5_DIR/src/cpu/o3/probe/"
cp -v "$REPO/gem5_patches/src/cpu/o3/probe/branch_events.cc" "$GEM5_DIR/src/cpu/o3/probe/"
cp -v "$REPO/gem5_patches/src/cpu/o3/probe/branch_events.hh" "$GEM5_DIR/src/cpu/o3/probe/"
cp -v "$REPO/gem5_patches/src/cpu/o3/probe/TaoTrace.py"  "$GEM5_DIR/src/cpu/o3/probe/"
cp -v "$REPO/gem5_patches/src/cpu/o3/probe/tao_trace.cc" "$GEM5_DIR/src/cpu/o3/probe/"
cp -v "$REPO/gem5_patches/src/cpu/o3/probe/tao_trace.hh" "$GEM5_DIR/src/cpu/o3/probe/"
cp -v "$REPO/gem5_patches/src/cpu/o3/probe/SConscript"   "$GEM5_DIR/src/cpu/o3/probe/"

# 让 probe 能 include gem5_patches/ 内自带的 shared 头文件，
# 避免构建时再隐式依赖仓库根目录下的 shared/。
SHARED_INC="$REPO/gem5_patches/shared"

# V9.6 ROI hook：把 m5_work_begin / m5_work_end 接到 TaoTrace::traceWorkBegin/End。
#   gem5_patches/src/sim/pseudo_inst.cc.roi_hook.patch 记录当前 hook 的统一 diff；
#   这里继续保留 sed 注入，兼顾幂等与跨版本兼容性。
#   幂等：以 "TAOGEN_ROI_HOOK" 标记位判断是否已注入。
#   注：必须把 hook 加在 `if (params.exit_on_work_items)` 之前，否则 gem5 stdlib
#       Simulator 的默认 exit_on_work_items=True 会让 workbegin/workend 走 exitSimLoop
#       并 return，hook 永不触发，ROI 流变成空文件。
PSEUDO_CC="$GEM5_DIR/src/sim/pseudo_inst.cc"
if ! grep -q "TAOGEN_ROI_HOOK" "$PSEUDO_CC"; then
  echo "[taogen] injecting ROI hook into $PSEUDO_CC"
  # 1) 在 #include "sim/pseudo_inst.hh" 之后追加 tao_trace.hh 引用
  sed -i '/^#include "sim\/pseudo_inst.hh"/a \
\
// TAOGEN_ROI_HOOK: m5_work_begin/end -> TaoTrace::traceWorkBegin/End\
#include "cpu/o3/probe/tao_trace.hh"' "$PSEUDO_CC"
  # 2) workbegin: hook 必须早于 exit_on_work_items 早退出。匹配函数体内首条
  #    DPRINTF(PseudoInst, ...) 后插入 hook。
  sed -i '/pseudo_inst::workbegin(%i, %i)/a \
    // TAOGEN_ROI_HOOK_EARLY\
    gem5::o3::TaoTrace::traceWorkBegin(\
        uint32_t(tc->getCpuPtr()->cpuId()), workid, threadid);' "$PSEUDO_CC"
  # 3) workend: 同上
  sed -i '/pseudo_inst::workend(%i, %i)/a \
    // TAOGEN_ROI_HOOK_EARLY\
    gem5::o3::TaoTrace::traceWorkEnd(\
        uint32_t(tc->getCpuPtr()->cpuId()), workid, threadid);' "$PSEUDO_CC"
fi

# ---------- 3) configure + build gem5 ----------
echo "[taogen] configuring gem5 X86_MESI_Three_Level"
( cd "$GEM5_DIR" && \
  scons defconfig build/X86_MESI_Three_Level "$REPO/gem5_patches/build_opts/X86_MESI_Three_Level" )

echo "[taogen] building gem5.opt X86_MESI_Three_Level (-j$JOBS)"
( cd "$GEM5_DIR" && \
  TAOGEN_SHARED="$SHARED_INC" \
  scons build/X86_MESI_Three_Level/gem5.opt PROTOCOL=MESI_Three_Level -j"$JOBS" )

# ---------- 4) build mesi_ref_sim ----------
echo "[taogen] building mesi_ref_sim"
mkdir -p "$REPO/mesi_ref_sim/build"
( cd "$REPO/mesi_ref_sim/build" && cmake .. && make -j"$JOBS" )

# ---------- 5) build workloads ----------
echo "[taogen] building workloads"
for w in mt_stream_mix mt_stencil2d mt_graph_walk mt_branch_state_machine mt_indirect_dispatch; do
  ( cd "$REPO/workloads/$w" && make )
done

echo ""
echo "[taogen] install OK"
echo "  gem5.opt   : $GEM5_DIR/build/X86_MESI_Three_Level/gem5.opt"
echo "  ref_sim    : $REPO/mesi_ref_sim/build/mesi_ref_sim"
echo "  workloads  : $REPO/workloads/{mt_stream_mix,mt_stencil2d,mt_graph_walk,mt_branch_state_machine,mt_indirect_dispatch}/"
echo ""
echo "[taogen] next:"
echo "  SMOKE=1 PYTHON=$PYTHON_BIN bash $REPO/scripts/run_w11_w15_parallel_collect.sh"
