#!/usr/bin/env bash
# MTAO 环境变量
# 用法: source scripts/env.sh

# 解析项目根
_THIS_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
export TAO_ROOT="$( dirname "${_THIS_DIR}" )"

# 子模块根（与 MTAO/ 平铺）
export TAO_DATAGEN_ROOT="${TAO_ROOT}/datagen"
export TAO_TRAIN_ROOT="${TAO_ROOT}/train"
export TAO_INFER_ROOT="${TAO_ROOT}/infer"
export TAO_GEM5_ROOT="${TAO_GEM5_ROOT:-${TAO_ROOT}/gem5}"

# 数据 / ckpt 路径
export TAO_DATA_ROOT="${TAO_DATA_ROOT:-${TAO_INFER_ROOT}/data}"
export TAO_TAOGEN_DATA_ROOT="${TAO_TAOGEN_DATA_ROOT:-${TAO_DATAGEN_ROOT}/data}"
export TAO_CKPT_ROOT="${TAO_CKPT_ROOT:-${TAO_ROOT}/ckpt}"

# Python / 推理
export PYTHONPATH="${TAO_INFER_ROOT}:${TAO_TRAIN_ROOT}:${PYTHONPATH:-}"
export TAO_INFER_DEVICE="${TAO_INFER_DEVICE:-cuda}"
export TAO_INFER_CUDA_DEVICES="${TAO_INFER_CUDA_DEVICES:-0}"
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-${TAO_INFER_CUDA_DEVICES}}"

# gem5 runtime libraries. The local gem5 binary is linked against Python 3.11
# and a libstdc++ that provides GLIBCXX_3.4.30.
export TAO_GEM5_RUNTIME_LIB_DIR="${TAO_GEM5_RUNTIME_LIB_DIR:-/root/miniconda3/envs/yinhaolang/lib}"
if [[ -d "${TAO_GEM5_RUNTIME_LIB_DIR}" ]]; then
  case ":${LD_LIBRARY_PATH:-}:" in
    *":${TAO_GEM5_RUNTIME_LIB_DIR}:"*) ;;
    *) export LD_LIBRARY_PATH="${TAO_GEM5_RUNTIME_LIB_DIR}${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}" ;;
  esac
fi

# Quantum 默认值
export TAO_QUANTUM_CYCLES="${TAO_QUANTUM_CYCLES:-256}"

# ref_sim 模块
export TAO_REF_SIM_BUILD_DIR="${TAO_REF_SIM_BUILD_DIR:-${TAO_INFER_ROOT}/mesi_ref_sim/build}"

echo "[MTAO] TAO_ROOT=${TAO_ROOT}"
echo "[MTAO] TAO_DATAGEN_ROOT=${TAO_DATAGEN_ROOT}"
echo "[MTAO] TAO_TRAIN_ROOT=${TAO_TRAIN_ROOT}"
echo "[MTAO] TAO_INFER_ROOT=${TAO_INFER_ROOT}"
echo "[MTAO] TAO_GEM5_ROOT=${TAO_GEM5_ROOT}"
echo "[MTAO] TAO_DATA_ROOT=${TAO_DATA_ROOT}"
echo "[MTAO] TAO_CKPT_ROOT=${TAO_CKPT_ROOT}"
echo "[MTAO] TAO_INFER_DEVICE=${TAO_INFER_DEVICE}  CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES}"
echo "[MTAO] TAO_QUANTUM_CYCLES=${TAO_QUANTUM_CYCLES}"
echo "[MTAO] TAO_GEM5_RUNTIME_LIB_DIR=${TAO_GEM5_RUNTIME_LIB_DIR}"
