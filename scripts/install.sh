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
