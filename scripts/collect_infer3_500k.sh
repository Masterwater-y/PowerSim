#!/usr/bin/env bash
# 采集 3 个 inference-only 泛化验证负载。
# 口径与现有 train8 采集保持一致：
# - 8 核 pthread workload
# - gem5 --require-roi
# - probe -> 估算 scale -> 正式采集
# - 每核目标约 500k records.micro
#
# 这 3 个负载仅用于推理验证，不接入训练集脚本。

set -euo pipefail

ROOT=/data00/yinhaolang/LLMSim
export OUT_BASE=${OUT_BASE:-$ROOT/data/raw_infer3_8c_500k}
export TARGET_PER_CORE=${TARGET_PER_CORE:-500000}
export MIN_ACCEPT_PER_CORE=${MIN_ACCEPT_PER_CORE:-400000}
export MAX_ACCEPT_PER_CORE=${MAX_ACCEPT_PER_CORE:-600000}
export SCALE_MARGIN_PCT=${SCALE_MARGIN_PCT:-100}
export VALIDATE_WINDOWS=${VALIDATE_WINDOWS:-0}

exec bash "$ROOT/scripts/collect_parallel_500k.sh" \
  feed_ranking \
  ads_ctr \
  interest_graph_recall
