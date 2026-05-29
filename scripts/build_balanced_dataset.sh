#!/usr/bin/env bash
# 从已完成的 gem5 run 目录构造跨 workload 均衡数据集，并执行 parquet pack + C-DUP 去重。
#
# 用法：
#   bash scripts/build_balanced_dataset.sh [RUN_BASE] [OUT_BASE] [TARGET]
#
# 示例：
#   bash scripts/build_balanced_dataset.sh \
#     tmp/runP3_train_20260529 tmp/dataset_v97_balanced_3m 3000000
#
# 环境变量：
#   WORKLOADS          逗号分隔 workload 名，默认 W11..W15
#   HEAD_SKIP          每线程跳过比例，默认 0.05
#   TAIL_SKIP          每线程尾部跳过比例，默认 0.05
#   CTX_WARMUP_SKIP    每线程 ROI 起始显式跳过 µop 数，默认 128
#   CTX_LEN            C-DUP 上下文长度，默认 128
#   PROTECT_POSITIVES  传给 dedup_dataset.sh，默认 1
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

RUN_BASE="${1:-$REPO/tmp/runP3_train}"
OUT_BASE="${2:-$REPO/tmp/dataset_v97_balanced}"
TARGET="${3:-3000000}"

WORKLOADS_CSV="${WORKLOADS:-W11_stream_mix,W12_stencil2d,W13_graph_walk,W14_branch_state,W15_indirect}"
HEAD_SKIP="${HEAD_SKIP:-0.05}"
TAIL_SKIP="${TAIL_SKIP:-0.05}"
CTX_WARMUP_SKIP="${CTX_WARMUP_SKIP:-128}"
CTX_LEN="${CTX_LEN:-128}"

PY="${PYTHON:-/root/.pyenv/versions/3.11.14/bin/python3.11}"
JSONL="$OUT_BASE/samples.jsonl"
PQ="$OUT_BASE/pq"
DEDUP="$OUT_BASE/dedup_ctx${CTX_LEN}"

mkdir -p "$OUT_BASE"

IFS=',' read -r -a WORKLOADS_ARR <<< "$WORKLOADS_CSV"
RUN_ARGS=()
for w in "${WORKLOADS_ARR[@]}"; do
  d="$RUN_BASE/$w"
  if [[ ! -d "$d" ]]; then
    echo "[err] missing workload run dir: $d" >&2
    exit 2
  fi
  RUN_ARGS+=(--run "$w=$d")
done

echo "RUN_BASE         = $RUN_BASE"
echo "OUT_BASE         = $OUT_BASE"
echo "TARGET           = $TARGET"
echo "WORKLOADS        = $WORKLOADS_CSV"
echo "HEAD_SKIP        = $HEAD_SKIP"
echo "TAIL_SKIP        = $TAIL_SKIP"
echo "CTX_WARMUP_SKIP  = $CTX_WARMUP_SKIP"
echo "CTX_LEN          = $CTX_LEN"
echo

"$PY" "$REPO/tools/sample_steady_balanced.py" \
  --target "$TARGET" \
  --head-skip "$HEAD_SKIP" \
  --tail-skip "$TAIL_SKIP" \
  --context-warmup-skip "$CTX_WARMUP_SKIP" \
  "${RUN_ARGS[@]}" \
  --out "$JSONL"

bash "$REPO/scripts/pack_3m.sh" "$JSONL" "$PQ"
bash "$REPO/scripts/dedup_dataset.sh" "$PQ" "$DEDUP" "$CTX_LEN"

echo
echo "=== dataset outputs ==="
echo "jsonl : $JSONL"
echo "pq    : $PQ"
echo "dedup : $DEDUP"
