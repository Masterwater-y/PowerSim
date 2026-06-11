#!/usr/bin/env bash
# 烟囱测试：5 步 dryrun，验证 dataset / model / forward / backward / ckpt 全链路
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_DATA="${TAO_DATAGEN_ROOT:-${TAO_ROOT}/datagen}/tmp/06031920/final_balanced_50000000_pq"
if [[ ! -d "$DEFAULT_DATA" ]]; then
  DEFAULT_DATA="$HERE/data/final_balanced_50000000_pq_dedup_v10_3_ma16"
fi
DATA="${DATA:-$DEFAULT_DATA}"
TORCHRUN_BIN="${TORCHRUN_BIN:-/root/miniconda3/envs/yinhaolang/bin/torchrun}"
SMOKE_NPROC="${SMOKE_NPROC:-1}"
THREADS_PER_RANK="${THREADS_PER_RANK:-$(( 32 / SMOKE_NPROC > 0 ? 32 / SMOKE_NPROC : 1 ))}"
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-$THREADS_PER_RANK}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-$THREADS_PER_RANK}"
cd "$HERE"
if [[ "$SMOKE_NPROC" -gt 1 ]]; then
  exec "$TORCHRUN_BIN" --standalone --nnodes=1 --nproc_per_node="$SMOKE_NPROC" -m ml.train \
    --data    "$DATA" \
    --bs      8 \
    --steps   5 \
    --workers 2 \
    --no-bf16 \
    --save    /tmp/dryrun_v10_3.pt
fi
exec python -m ml.train \
  --data    "$DATA" \
  --bs      8 \
  --steps   5 \
  --workers 2 \
  --no-bf16 \
  --save    /tmp/dryrun_v10_3.pt
