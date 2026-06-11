#!/usr/bin/env bash
# 一键把 jsonl 数据集转为分区 parquet。
# 用法：bash scripts/pack_3m.sh [IN_JSONL] [OUT_DIR]
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

IN_JSONL="${1:-${TAO_ROOT}/tmp/dataset_3m/samples_3m.jsonl}"
OUT_DIR="${2:-${TAO_ROOT}/tmp/dataset_3m_pq}"
UARCH_PROFILE="${3:-${UARCH_PROFILE:-}}"
if [[ -z "$UARCH_PROFILE" ]]; then
  echo "FATAL: pass uarch_profile.json as arg3 or set UARCH_PROFILE" >&2
  exit 2
fi

# pyarrow 需要较新 Python（≥3.9）。Python 3.8 没有可用预编译 wheel。
PY="${PYTHON:-/root/.pyenv/versions/3.11.14/bin/python3.11}"
"$PY" -c "import pyarrow" 2>/dev/null || "$PY" -m pip install -q pyarrow

"$PY" "$REPO/tools/pack_to_parquet.py" \
  --in-jsonl "$IN_JSONL" \
  --out-dir  "$OUT_DIR" \
  --uarch-profile "$UARCH_PROFILE"

echo
echo "=== summary ==="
du -sh "$OUT_DIR"
ls -lh "$OUT_DIR"
for w in "$OUT_DIR"/workload=*/part-000.parquet; do
  echo "$(du -h "$w" | cut -f1)  $w"
done
