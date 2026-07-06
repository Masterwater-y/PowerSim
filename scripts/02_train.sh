#!/usr/bin/env bash
# LLMSim Phase0 8 卡 DDP 训练 + 吞吐报告。
set -uo pipefail
PY=/data00/yinhaolang/infer/.venv/bin/python
cd /data00/yinhaolang/LLMSim

export HF_HUB_OFFLINE=1
export TOKENIZERS_PARALLELISM=false
export OMP_NUM_THREADS=4
NPROC=${NPROC:-8}
DATA=${DATA:-data/windows/windows.jsonl}
OUT=${OUT:-ckpt/phase0_ddp8}
STEPS=${STEPS:-200}
BS=${BS:-2}
MAXLEN=${MAXLEN:-4096}

"$PY" -m torch.distributed.run --nproc_per_node="$NPROC" --master_port=29577 \
  --start-method=spawn \
  train/train_lora.py \
  --data "$DATA" --out "$OUT" --steps "$STEPS" --bs "$BS" --max-len "$MAXLEN" \
  --log-every 20 --eval-every 50 --val-frac 0.15
