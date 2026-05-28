#!/usr/bin/env bash
# 一键正式训练：144K 数据集 × 1 epoch ≈ 1 小时（bs=128 bf16 ~38～50 samples/s）
#
# 用法：
#   bash scripts/train_run.sh                           # 默认 1 epoch、每 epoch 落一份 ckpt
#   EPOCHS=4 bash scripts/train_run.sh                  # 跑 4 个 epoch（≈ 4h）
#   EPOCHS=4 SAVE_EVERY_EPOCH=2 bash ...                # 每 2 个 epoch 落一份
#   SAVE_EVERY=200 bash ...                             # 直接按 step 指定（覆盖 SAVE_EVERY_EPOCH）
#   RESUME=/path/tao_xx.last.pt EPOCHS=4 bash ...       # 从 ckpt 续训（last.pt / best.pt / step*.pt 都行）
#
# 自动：
#   - 缺数据集时先用 tools/subsample_dataset.py 生成 144K mini 集
#   - mispred pos_weight 自动估算
#   - BF16 autocast + OMP/MKL=32 + numactl NUMA 绑定
#   - 输出 ckpt 到 tmp/ckpt/tao_<timestamp>.pt
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="${DATA:-/data00/yinhaolang/simulators/tmp/dataset_144k_pq}"
PYBIN="${PYBIN:-/root/.pyenv/versions/3.11.14/bin/python3.11}"
EPOCHS="${EPOCHS:-1}"
BS="${BS:-128}"
CTX="${CTX:-128}"
LR="${LR:-3e-4}"
WORKERS="${WORKERS:-8}"
NUM_THREADS="${NUM_THREADS:-32}"
N_ROWS="${N_ROWS:-144000}"
WARMUP_FRAC="${WARMUP_FRAC:-0.03}"
# checkpoint 频率：默认每 1 个 epoch 一份，可被 SAVE_EVERY (按 step) 覆盖
SAVE_EVERY_EPOCH="${SAVE_EVERY_EPOCH:-1}"
KEEP_LAST="${KEEP_LAST:-3}"
# 续训 ckpt（空字符串 = 从头训）
RESUME="${RESUME:-}"

cd "$REPO"

if [ ! -d "$DATA" ]; then
  echo "[train] mini dataset not found, generating ${N_ROWS}-row subset ..."
  "$PYBIN" tools/subsample_dataset.py --target "$N_ROWS"
fi

# steps = ceil(EPOCHS * N_ROWS / BS)
STEPS=$(( (EPOCHS * N_ROWS + BS - 1) / BS ))
STEPS_PER_EPOCH=$(( (N_ROWS + BS - 1) / BS ))
WARMUP=$(awk -v s="$STEPS" -v f="$WARMUP_FRAC" 'BEGIN{printf "%d", s*f}')
LOG_EVERY=$(( STEPS / 200 > 0 ? STEPS / 200 : 1 ))

# SAVE_EVERY 优先级：用户显式指定 > 由 SAVE_EVERY_EPOCH 折算
if [ -n "${SAVE_EVERY:-}" ]; then
  SAVE_EVERY_EFFECTIVE="$SAVE_EVERY"
  SAVE_EVERY_DESC="${SAVE_EVERY} steps (env override)"
else
  SAVE_EVERY_EFFECTIVE=$(( STEPS_PER_EPOCH * SAVE_EVERY_EPOCH ))
  SAVE_EVERY_DESC="${SAVE_EVERY_EFFECTIVE} steps (= ${SAVE_EVERY_EPOCH} epoch × ${STEPS_PER_EPOCH} steps/epoch)"
fi

# 续训校验：取出 ckpt 里的 step，如果 STEPS <= step 就直接拒绝运行
RESUME_DESC="(从头训)"
RESUME_ARGS=()
if [ -n "$RESUME" ]; then
  if [ ! -f "$RESUME" ]; then
    echo "[train] ERROR: RESUME ckpt 不存在: $RESUME" >&2
    exit 2
  fi
  CKPT_STEP="$("$PYBIN" - <<PY
import torch, sys
ck = torch.load("$RESUME", map_location='cpu', weights_only=False)
print(int(ck.get('step', 0)))
PY
)"
  if [ -z "$CKPT_STEP" ]; then
    echo "[train] ERROR: 无法读取 ckpt 的 step 字段: $RESUME" >&2
    exit 2
  fi
  if [ "$STEPS" -le "$CKPT_STEP" ]; then
    cat >&2 <<EOF
[train] ERROR: 目标 STEPS=${STEPS} 不大于 ckpt 已训步数 (step=${CKPT_STEP})。
       这种情况下 train.py 会立刻进入 finally 写一份 last.pt 然后退出，不会真正续训。
       请加大 EPOCHS（当前=${EPOCHS}），让 STEPS > ${CKPT_STEP}。
       例如：EPOCHS=$(( (CKPT_STEP * BS + N_ROWS - 1) / N_ROWS + 1 )) RESUME=$RESUME bash scripts/train_run.sh
EOF
    exit 2
  fi
  RESUME_DESC="$RESUME (ckpt step=${CKPT_STEP} → 还需训 $(( STEPS - CKPT_STEP )) 步)"
  RESUME_ARGS=(--resume "$RESUME")
fi

TS="$(date +%Y%m%d_%H%M%S)"
CKPT_DIR="/data00/yinhaolang/simulators/tmp/ckpt"
mkdir -p "$CKPT_DIR"
CKPT="$CKPT_DIR/tao_${TS}.pt"

echo "============================================================"
echo "[train] data       = $DATA"
echo "[train] epochs     = $EPOCHS  (rows=$N_ROWS, bs=$BS, ctx=$CTX)"
echo "[train] steps      = $STEPS  (steps/epoch = $STEPS_PER_EPOCH)"
echo "[train] warmup     = $WARMUP  ($WARMUP_FRAC of total)"
echo "[train] lr         = $LR"
echo "[train] workers    = $WORKERS  threads=$NUM_THREADS"
echo "[train] save_every = $SAVE_EVERY_DESC"
echo "[train] keep_last  = $KEEP_LAST"
echo "[train] resume     = $RESUME_DESC"
echo "[train] ckpt       = $CKPT"
echo "[train] log        = $CKPT_DIR/tao_${TS}.log         (train.py 自管)"
echo "[train] status     = $CKPT_DIR/tao_${TS}.status.json (cat 即可看进度)"
echo "============================================================"

# 注意：train.py 内部已经用 logging.FileHandler 写 <base>.log，并对每条记录 fsync。
# 这里不再用 tee 转写同一个文件，避免重复行。stderr 仍打到当前终端。
exec numactl --cpunodebind=0 --membind=0 \
  stdbuf -oL -eL \
  "$PYBIN" -u -m ml.train \
    --data "$DATA" \
    --bs "$BS" --ctx "$CTX" --workers "$WORKERS" \
    --steps "$STEPS" --lr "$LR" --warmup "$WARMUP" \
    --log-every "$LOG_EVERY" \
    --num-threads "$NUM_THREADS" \
    --save "$CKPT" \
    --save-every "$SAVE_EVERY_EFFECTIVE" \
    --keep-last "$KEEP_LAST" \
    ${RESUME_ARGS[@]+"${RESUME_ARGS[@]}"}
