#!/usr/bin/env bash
# 对 packed parquet 数据集做 TAO §4 风格的 C-DUP 上下文指纹去重。
#
# 用法：
#   bash scripts/dedup_dataset.sh [IN_DIR] [OUT_DIR] [CTX_LEN]
# 默认：
#   IN_DIR  = ${TAO_ROOT}/tmp/dataset_3m_pq
#   OUT_DIR = ${TAO_ROOT}/tmp/dataset_3m_dedup_pq
#   CTX_LEN = 128
#
# 默认开启 protect_positives：mispredicted=1 的稀有正样本不参与去重。

set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

IN_DIR="${1:-${TAO_ROOT}/tmp/dataset_3m_pq}"
OUT_DIR="${2:-${TAO_ROOT}/tmp/dataset_3m_dedup_pq}"
CTX_LEN="${3:-128}"

PY="${PYTHON:-/root/.pyenv/versions/3.11.14/bin/python3.11}"
"$PY" -c "import pyarrow" 2>/dev/null || "$PY" -m pip install -q pyarrow

if [[ ! -d "$IN_DIR" ]]; then
  echo "[err] in-dir not found: $IN_DIR" >&2
  exit 2
fi

echo "IN_DIR  = $IN_DIR"
echo "OUT_DIR = $OUT_DIR"
echo "CTX_LEN = $CTX_LEN"
echo "TAIL_Q  = ${PROTECT_LATENCY_Q:-0.99}"
echo

EXTRA=()
if [[ "${PROTECT_POSITIVES:-1}" == "0" ]]; then
  EXTRA+=("--no-protect-positives")
fi

"$PY" "$REPO/tools/dedup_context.py" \
  --in-dir "$IN_DIR" \
  --out-dir "$OUT_DIR" \
  --context-len "$CTX_LEN" \
  --protect-latency-quantile "${PROTECT_LATENCY_Q:-0.99}" \
  "${EXTRA[@]}"

echo
echo "=== summary ==="
du -sh "$OUT_DIR"
for w in "$OUT_DIR"/workload=*/part-000.parquet; do
  echo "$(du -h "$w" | cut -f1)  $w"
done
echo
echo "meta.json dedup fields:"
"$PY" -c "import json; m=json.load(open('$OUT_DIR/meta.json')); \
print({k:v for k,v in m.items() if k.startswith('dedup_') or k=='source_dataset'})"
