#!/usr/bin/env bash
# taogen/scripts/build_3m_dataset.sh
# 构造 3M 跨 workload 均衡 µop 数据集（schema v2，17 字段）。
#
# 前置：scripts/run_experiment.sh 已跑完，得到
#   $RUN_BASE/{W1_compute_int,W2_chase_dram,W3_micro_coh,W4_coh_stress}/tao_trace/...
#
# 用法：
#   bash scripts/build_3m_dataset.sh [RUN_BASE] [OUT_FILE] [TARGET]
# 默认：
#   RUN_BASE = $REPO/tmp/step5_a2a3
#   OUT_FILE = $REPO/tmp/dataset_3m/samples_3m.jsonl
#   TARGET   = 3000000

set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

RUN_BASE="${1:-$REPO/tmp/step5_a2a3}"
OUT_FILE="${2:-$REPO/tmp/dataset_3m/samples_3m.jsonl}"
TARGET="${3:-3000000}"

mkdir -p "$(dirname "$OUT_FILE")"

if [[ ! -d "$RUN_BASE/W1_compute_int" ]]; then
  # 兼容外部 tmp 目录
  ALT="${TAO_ROOT}/tmp/step5_a2a3"
  if [[ -d "$ALT/W1_compute_int" ]]; then
    RUN_BASE="$ALT"
  fi
fi

echo "RUN_BASE = $RUN_BASE"
echo "OUT_FILE = $OUT_FILE"
echo "TARGET   = $TARGET"

python3 "$REPO/tools/sample_steady_balanced.py" \
  --target "$TARGET" \
  --head-skip 0.05 --tail-skip 0.05 \
  --run "W1_compute_int=$RUN_BASE/W1_compute_int" \
  --run "W2_chase_dram=$RUN_BASE/W2_chase_dram" \
  --run "W3_micro_coh=$RUN_BASE/W3_micro_coh" \
  --run "W4_coh_stress=$RUN_BASE/W4_coh_stress" \
  --out "$OUT_FILE"

LINES=$(wc -l < "$OUT_FILE")
SIZE=$(du -h "$OUT_FILE" | cut -f1)
echo
echo "=== dataset built: $LINES rows, $SIZE -> $OUT_FILE ==="
