# TAO V10.3 训练交付包

> 本子模块涉及的训练输入、模型特征族、评估输出与 ckpt 口径，统一以
> [global/SCHEMA.md](SCHEMA.md) 为准。
> 若本文档与 `global/SCHEMA.md`、当前生产 ckpt 或实际代码实现冲突，以后者为准。

## 内容
```
deliverable_v10_3/
├── ml/
│   ├── __init__.py
│   ├── dataset.py      # ParquetWindowDataset / DatasetSpec / collate / FEATURE_COLS
│   ├── model.py        # TaoConfig / TaoCoreTransformer (V10.3 6 族 emb + 6×8 attn + 4 头)
│   ├── train.py        # GPU/CPU 自适应 + bf16 AMP + ckpt
│   └── infer.py
├── data/final_balanced_50000000_pq/
│   ├── meta.json       # schema_version=v10_3_pq_a_b_c, dram_cfg
│   ├── vocab.json      # macro_pc / segment_id 等离散表
│   └── workload=W{11,12,13,14,15}/part-000.parquet  # 5×10M = 50M 行
├── requirements.txt
├── run_smoke.sh        # 5 步烟囱测试
└── run_train.sh        # 全量训练
```

## 数据集事实
- 50,000,000 行 parquet（5 个 workload × 10M 行 balanced 采样）
- schema_version: `v10_3_pq_a_b_c`
- dram_cfg: `{banks_per_channel: 16, row_size_b: 8192}`（uarch_profile 单源）
- 77 列特征（含 V10.3 9 个新字段）+ 标签 `fetch_latency / execution_latency / mispredicted / is_fetch_group_head`
- D-side packet 路径 oracle ↔ ref_sim 100% bit-exact 验证通过

## 在 GPU 机器上启动训练

### 1) 准备环境（一次性）
```bash
# 需要 Python 3.10/3.11
pip install torch --index-url https://download.pytorch.org/whl/cu121
pip install pyarrow numpy
# 或：pip install -r requirements.txt
```

### 2) 烟囱测试（< 30 秒）
```bash
cd deliverable_v10_3
bash run_smoke.sh
```
跑通即证明 dataset / model / forward / backward / ckpt save 全链路 OK。

### 3) 全量训练（默认 100K 步）
```bash
cd deliverable_v10_3
bash run_train.sh
# 透传任何 --xxx 参数会覆盖默认，例：
bash run_train.sh --bs 512 --steps 200000 --lr 5e-4
```
- 默认 `bs=256 ctx=128 steps=100000 lr=3e-4 warmup=2000 workers=8 bf16=on`
- ckpt 输出到 `./ckpt/tao_v10_3.pt`，每 2000 步保存，保留最近 5 个
- 若存在 `MTAO/datagen/tmp/06031920/final_balanced_50000000_pq`，`run_train.sh`
  与 `run_smoke.sh` 会优先使用这份重建后的 50M 数据；也可用 `DATA=/path/...`
  显式覆盖。

### 4) 推理 / 评估
```bash
python -m ml.infer --ckpt ./ckpt/tao_v10_3.pt --data ./data/final_balanced_50000000_pq --bs 256
```

## 调参建议（按显存）
| GPU | bs | workers |
|---|---|---|
| RTX 3090 / 4090 (24GB) | 256 | 8 |
| A100 40GB | 512 | 16 |
| A100 80GB / H100 | 1024 | 16 |

## 关键细节
- `train.py` 已支持 `device.type ∈ {cpu, cuda}` 自适应；GPU 上 bf16 AMP 默认开启（Ampere+ 架构最优）。
- DataLoader 按 hive 分区流式读取，不会一次性 load 50M 到内存。
- 模型 ~5–8M 参数，单 GPU 完全够用。
- 训练损失：`MSE_fetch(head-only) + 0.25·MSE_fetch_cons + MSE_exec + 0.5·BCE_mispred + 0.1·BCE_head`
- `fetch_latency` 采用 head-gated 训练/推理语义：仅 `head=1` 时输出非零 fetch latency。
- dataloader 会在线派生 `is_macro_head / uop_pos_in_macro / i_group_head / i_group_pos`，
  不再依赖绝对 `macro_pc_id` 作为模型输入。
- mispred 自动 `pos_weight`（dataset 估出 ~7×）+ 可选 focal γ。

## 训练集 / 验证集切分
```bash
cd <MTAO>/tao_train
/root/miniconda3/envs/yinhaolang/bin/python tools/split_train_val.py \
  --input-root MTAO/datagen/tmp/06031920/final_balanced_50000000_pq \
  --out-root MTAO/datagen/tmp/06031920/final_balanced_50000000_pq_split_95_5
```
- 默认方案：`95/5`、按 `(workload, core_id, thread_id, chunk_id)` 稳定 hash 切分
- 默认 `chunk_size=262144`、`guard_band=1024`
- 输出目录：
  - `.../train/workload=W*/part-000.parquet`
  - `.../val/workload=W*/part-000.parquet`
  - `.../split_report.json`
  - `.../split_report.md`
- 脚本会同时复制 `vocab.json`、重写 `train/meta.json` 和 `val/meta.json`
- 报告会检查 `workload` 占比、`is_fetch_group_head` / `mispredicted` 正例率、
  `fetch_latency` / `execution_latency` 抽样分位数，以及关键布尔特征分布

## 包大小
- `data/`：约 1.0 GB（50M 行 zstd 压缩 parquet）
- `ml/`：约 70 KB
- 总计约 1.0 GB；强烈建议 zstd/tar 后传输：
```bash
tar -cf - deliverable_v10_3 | zstd -T0 > deliverable_v10_3.tar.zst
# 解压：zstd -dc deliverable_v10_3.tar.zst | tar -xf -
```
