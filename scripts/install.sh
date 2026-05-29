#!/usr/bin/env bash
# taogen/scripts/install.sh
# 一键准备 taogen 实验环境：
#   1) clone gem5 (v23.0.x 兼容)
#   2) 应用 gem5_patches/ (TaoTrace probe + SConscript hook)
#   3) 构建 gem5.opt X86_MESI_Three_Level (96 核 -j)
#   4) 构建 mesi_ref_sim
#   5) 构建 4 个 workload 二进制
#
# 用法：
#   bash scripts/install.sh [GEM5_DIR]
# 默认 GEM5_DIR=$REPO/gem5
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GEM5_DIR="${1:-$REPO/gem5}"
JOBS="${JOBS:-96}"

echo "[taogen] REPO=$REPO"
echo "[taogen] GEM5_DIR=$GEM5_DIR"
echo "[taogen] JOBS=$JOBS"

# ---------- 1) clone gem5 ----------
if [ ! -d "$GEM5_DIR" ]; then
  echo "[taogen] cloning gem5 v23.0..."
  git clone --depth 1 --branch v23.0.0.0 https://github.com/gem5/gem5.git "$GEM5_DIR"
fi

# ---------- 2) 应用 patch ----------
echo "[taogen] applying gem5_patches/ -> $GEM5_DIR"
cp -v "$REPO/gem5_patches/src/cpu/o3/probe/TaoTrace.py"  "$GEM5_DIR/src/cpu/o3/probe/"
cp -v "$REPO/gem5_patches/src/cpu/o3/probe/tao_trace.cc" "$GEM5_DIR/src/cpu/o3/probe/"
cp -v "$REPO/gem5_patches/src/cpu/o3/probe/tao_trace.hh" "$GEM5_DIR/src/cpu/o3/probe/"
cp -v "$REPO/gem5_patches/src/cpu/o3/probe/SConscript"   "$GEM5_DIR/src/cpu/o3/probe/"

# 让 probe 能 include 我们 shared/ 头文件
SHARED_INC="$REPO/shared"
if ! grep -q "TAOGEN_SHARED_INC" "$GEM5_DIR/src/cpu/o3/probe/SConscript"; then
  echo "[taogen] hooking shared/ include path into SConscript"
  cat >> "$GEM5_DIR/src/cpu/o3/probe/SConscript" <<EOF
# TAOGEN_SHARED_INC: 让 tao_trace.cc 能 include taogen/shared/{lru_banked.hh,uarch_profile.hh}
import os
Import('env')
env.Append(CPPPATH=[os.environ.get('TAOGEN_SHARED', '$SHARED_INC')])
EOF
fi

# V9.6 ROI hook：把 m5_work_begin / m5_work_end 接到 TaoTrace::traceWorkBegin/End。
#   tao_trace 与 Ruby 文件的 traceCacheEvent 同款套路：sed 注入 include + 一行调用。
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

# ---------- 3) build gem5 ----------
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
for w in mt_compute_int mt_chase_dram mt_micro_coh mt_coh_stress; do
  ( cd "$REPO/workloads/$w" && make )
done

echo ""
echo "[taogen] install OK"
echo "  gem5.opt   : $GEM5_DIR/build/X86_MESI_Three_Level/gem5.opt"
echo "  ref_sim    : $REPO/mesi_ref_sim/build/mesi_ref_sim"
echo "  workloads  : $REPO/workloads/{mt_compute_int,mt_chase_dram,mt_micro_coh,mt_coh_stress}/"
