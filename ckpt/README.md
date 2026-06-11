# MTAO ckpt/

This directory is the **landing zone for key checkpoints**. It is intentionally
kept under version control as a folder, but all `*.pt` artifacts are excluded
by `.gitignore` (see project root).

## Layout

```
ckpt/
├── README.md                          # this file
├── tao_v10_3_ma16.best.pt             # 当前最佳 (best val) checkpoint
├── tao_v10_3_ma16.last.pt             # 训练最后一步 ckpt (resume 用)
├── tao_v10_3_ma16.status.json         # 训练状态记录（最佳指标 / 路径）
└── iteration_best/                    # 各代次最佳归档（model iteration history）
    ├── v10_2_base_tao_v10_3_ma16.best.pt
    ├── v10_3_fetchdecomp.best.pt
    ├── v10_4_softgate_mlp.best.pt
    ├── v10_5_candidate_smoke.best.pt
    └── v10_5_candidate_carry_smoke.best.pt
```

各代次特性见 [`docs/09-model-iteration-history.md`](../docs/09-model-iteration-history.md)。

## 用法

训练脚本 (`train/run_train.sh`, `datagen/scripts/train_run.sh` 等) 默认按
`TAO_CKPT_ROOT` 环境变量（由 `scripts/env.sh` 设置为 `$TAO_ROOT/ckpt`）
落地新 ckpt：

```bash
source scripts/env.sh
# TAO_CKPT_ROOT=/abs/path/to/MTAO/ckpt
bash train/run_train.sh
```

推理脚本（`scripts/04_infer.sh`、`infer/scripts/infer_from_functional.sh`）
默认从 `$TAO_CKPT_ROOT/tao_v10_3_ma16.best.pt` 读取。可通过
`--ckpt /abs/path/to.pt` 显式覆盖。

## 在冷目录恢复 ckpt

由于 `*.pt` 不入 git，冷目录 clone 后 `ckpt/` 仅有本 README。请按以下任一方式恢复：

1. **从对象存储拉取**（推荐）：

   ```bash
   # 例：内部对象存储 / S3 / OSS
   <your-fetch-cmd> <remote>/MTAO_ckpt/ ./ckpt/
   ```

2. **从已有训练机器同步**：

   ```bash
   rsync -a <user>@<host>:/path/to/MTAO/ckpt/ ./ckpt/
   ```

3. **从零重训**：

   ```bash
   bash scripts/02_train.sh
   ```

## .gitignore 行为

项目根 `.gitignore` 规则：

```
*.pt
*.pth
*.ckpt
ckpt/**
!ckpt/README.md
!ckpt/.gitkeep
```

含义：`ckpt/` 内除 `README.md` 与 `.gitkeep` 之外的一切都被忽略，包括
`iteration_best/` 目录及其内容。如需把某个**特别小**的元数据文件
（如 `status.json`）纳入 git，请显式 `git add -f`。
