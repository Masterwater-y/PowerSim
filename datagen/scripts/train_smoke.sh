#!/usr/bin/env bash
# 冒烟训练：bs=64 bf16 30 步 + 仅扫 mispred 列估算 pos_weight，目标 ~1 分钟
# 用 mini 数据集，验证链路 + pos_weight + bf16 没问题
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="${DATA:-${TAO_ROOT}/tmp/dataset_144k_pq}"
PYBIN="${PYBIN:-/root/.pyenv/versions/3.11.14/bin/python3.11}"

cd "$REPO"

if [ ! -d "$DATA" ]; then
  echo "[smoke] mini dataset not found, generating ..."
  "$PYBIN" tools/subsample_dataset.py --target 144000
fi

echo "[smoke] starting bf16 30-step run on $DATA"
exec numactl --cpunodebind=0 --membind=0 \
  "$PYBIN" -m ml.train \
    --data "$DATA" \
    --bs 64 --ctx 128 --workers 4 \
    --steps 30 --lr 3e-4 --warmup 5 --log-every 5 \
    --num-threads 32
