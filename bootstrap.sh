#!/usr/bin/env bash
# MTAO bootstrap: 在冷目录把整个项目重建出来。
#
# 过程:
#   1) 拉取 gem5 v25.1.0.1 到 MTAO/gem5/ (已被 .gitignore 忽略)
#   2) 应用 MTAO/datagen/gem5_patches/ 并构建 gem5.opt + ref_sim + workloads
#   3) 安装 infer 后端 (ref_sim_py.so)
#   4) 安装 train 依赖
#
# 用法:
#   bash bootstrap.sh                      # 全步走
#   SKIP_GEM5=1 bash bootstrap.sh          # 跳过 gem5/ref_sim/workloads (验收常用)
#   SKIP_INFER=1 bash bootstrap.sh         # 跳过 infer 编译
#   SKIP_TRAIN=1 bash bootstrap.sh         # 跳过 train pip install
#   GEM5_DIR=/abs/path bash bootstrap.sh   # 自定义 gem5 工作树位置
#   PYTHON=/path/to/python3.11 bash bootstrap.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON:-$(command -v python3.11 || command -v python3)}"
JOBS="${JOBS:-$(nproc 2>/dev/null || echo 8)}"

export PYTHON="$PYTHON_BIN"
export JOBS

echo "[bootstrap] ROOT=$ROOT"
echo "[bootstrap] PYTHON=$PYTHON_BIN"
echo "[bootstrap] JOBS=$JOBS"

# 0) 基本依赖检查
for tool in git cmake make gcc g++ "$PYTHON_BIN"; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "[bootstrap][FATAL] missing required tool: $tool" >&2
    exit 2
  fi
done

# 1+2+workloads: 复用 datagen/scripts/install.sh
if [[ "${SKIP_GEM5:-0}" != "1" ]]; then
  GEM5_DIR="${GEM5_DIR:-$ROOT/gem5}"
  echo "[bootstrap] => datagen/scripts/install.sh GEM5_DIR=$GEM5_DIR"
  ( cd "$ROOT/datagen" && PYTHON="$PYTHON_BIN" JOBS="$JOBS" bash scripts/install.sh "$GEM5_DIR" )
else
  echo "[bootstrap] SKIP_GEM5=1, skip gem5/ref_sim/workloads"
fi

# 3) infer 后端 (ref_sim_py.so) + python deps
if [[ "${SKIP_INFER:-0}" != "1" ]]; then
  echo "[bootstrap] => infer/scripts/install.sh"
  ( cd "$ROOT/infer" && PYTHON="$PYTHON_BIN" JOBS="$JOBS" bash scripts/install.sh )
else
  echo "[bootstrap] SKIP_INFER=1, skip infer build"
fi

# 4) train 依赖
if [[ "${SKIP_TRAIN:-0}" != "1" ]]; then
  if [[ -f "$ROOT/train/requirements.txt" ]]; then
    echo "[bootstrap] => pip install train requirements"
    "$PYTHON_BIN" -m pip install -r "$ROOT/train/requirements.txt"
  fi
else
  echo "[bootstrap] SKIP_TRAIN=1, skip train pip install"
fi

# 5) 顶层 requirements (若存在且非空)
if [[ -s "$ROOT/requirements.txt" ]]; then
  echo "[bootstrap] => pip install root requirements"
  "$PYTHON_BIN" -m pip install -r "$ROOT/requirements.txt" || true
fi

cat <<EOF

[bootstrap] DONE.

Next steps:
  source $ROOT/scripts/env.sh
  # 然后参考 docs/03-workflow.md 中的 0X_*.sh 流水线脚本
  bash $ROOT/scripts/01_taogen_collect.sh   # 数据生成
  bash $ROOT/scripts/02_train.sh            # 训练
  bash $ROOT/scripts/04_infer.sh            # 推理 / 验证

如果需要载入已有 ckpt, 把 *.pt 放进 $ROOT/ckpt/ 后:
  export TAO_CKPT_ROOT=$ROOT/ckpt
EOF
