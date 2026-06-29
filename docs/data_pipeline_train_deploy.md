# Data Pipeline: Training And Deployment Eval

本文档整理当前 v10 / Qwen3-4B 路线从 raw trace 到训练、再到部署侧推理验证的数据流。重点是区分两类 cache，避免重复读慢路径或误用过期缓存。

## 0. 目录约定

统一 raw trace 入口放在：

```text
data/raw_trace_pool/
```

该目录只放 symlink，不复制 raw trace。正式实验优先使用 leaf raw root：

```text
data/raw_trace_pool/activecore_train/c01_seedA
data/raw_trace_pool/activecore_train/c04_seedA
data/raw_trace_pool/activecore_train/c08_seedA
data/raw_trace_pool/activecore_train/c16_seedA

data/raw_trace_pool/activecore_eval/c04_seedB_infer17
data/raw_trace_pool/activecore_eval/c06_seedC_infer17
data/raw_trace_pool/activecore_eval/c08_seedB_infer17
data/raw_trace_pool/activecore_eval/c16_seedB_infer17
data/raw_trace_pool/activecore_eval/c32_seedB_infer17
```

不要让训练脚本或 eval 脚本直接依赖散落的历史 `data/raw_*` 路径；需要新 raw trace 时先挂到 pool。

## 1. Raw Trace 是唯一源数据

每个 workload 的 raw trace 目录形态：

```text
<raw-root>/W_xxx/
  stats.txt
  uarch_profile.json
  counts.txt
  tao_trace/
    board.processor.cores0...records.micro.jsonl
    board.processor.cores0...labels.micro.jsonl
    board.processor.cores0...mem_events.jsonl
    ...
```

含义：

- `records.micro.jsonl`：functional trace，模型输入特征必须只从这里可推导的字段来。
- `labels.micro.jsonl`：gem5/TAO 标签，用于训练 PMU/CPI label 和 eval baseline，不是部署输入特征。
- `stats.txt`：full-run gem5 参考值。
- `mem_events.jsonl`：shared-system / coherence simulator 相关验证输入。

修改 raw trace 或重新采集后，下游所有派生格式都可能过期。

## 2. 部署侧 Raw Cache: aligned parquet

这是训练侧和部署侧可以共用的第一层 raw cache：

```text
<raw-root>/W_xxx/tao_trace/*.aligned.parquet
```

它由 `records.micro.jsonl + labels.micro.jsonl` 按 core 合并而成，避免每次 eval 或 build windows 时重新解析和 merge JSONL。当前代码会自动优先读 parquet：

- `data/roi_stats.py::load_workload_rows`
- `data/build_windows.py::load_core_files/read_aligned_parquet`
- `eval/eval_quota_cycles.py`

共用边界：

- 可以共用：同一个 raw root、同一个 workload、同一批 `records/labels` 生成的 `*.aligned.parquet`，训练 window builder 和部署 eval 都应直接读取它。
- 不能跨 raw root 共用：例如 `c08_seedA`、`c08_seedB`、`c16_seedB`、`c32_seedB` 都必须各自有自己的 parquet。
- 它不是训练 tensor cache：`max_len`、`BASE_MODEL`、checkpoint、nmin、window planner 改动不会改变 parquet。
- 它包含 label 列：训练侧可用来生成窗口 label；部署侧只能用 label 列做 ROI/window baseline 评估，不能把 label/latency/oracle 字段喂给模型输入特征。

构建命令：

```bash
nohup env RAW=data/raw_trace_pool/activecore_eval/c08_seedB_infer17 NUM_CORES=8 PARALLEL=4 OVERWRITE=0 \
bash scripts/prepare_quota_eval_cache.sh \
> logs/cache_eval_c08_seedB_infer17.nohup.log 2>&1 &
```

按核数替换：

```bash
RAW=data/raw_trace_pool/activecore_eval/c04_seedB_infer17 NUM_CORES=4
RAW=data/raw_trace_pool/activecore_eval/c06_seedC_infer17 NUM_CORES=6
RAW=data/raw_trace_pool/activecore_eval/c16_seedB_infer17 NUM_CORES=16
RAW=data/raw_trace_pool/activecore_eval/c32_seedB_infer17 NUM_CORES=32
```

建议 `PARALLEL`：

- c04/c06/c08：`PARALLEL=4`
- c16：`PARALLEL=4`
- c32：`PARALLEL=2`，避免同时读写太重。

何时重建：

- raw `records.micro.jsonl` 或 `labels.micro.jsonl` 改了。
- raw trace 重新采集了。
- parquet 文件数少于目标 core 数。
- converter schema 改了。

何时不需要重建：

- 换模型 checkpoint。
- 换 `BASE_MODEL`。
- 只改 `MAX_LEN` / `nmin` / eval window planner 参数。

当前覆盖状态（2026-06-29）：

```text
c06_seedC_infer17: 已有 aligned parquet
c08_seedB_infer17: 已有 aligned parquet
c04_seedB_infer17: 缺 aligned parquet
c16_seedB_infer17: 缺 aligned parquet
c32_seedB_infer17: 缺 aligned parquet
```

因此 c04/c16/c32 全量 eval 前应先跑 `prepare_quota_eval_cache.sh`。

## 3. 训练侧 Windows 数据集

训练不是直接从 raw trace 读 batch，而是先离线构建固定窗口：

```text
raw trace -> windows.jsonl -> windows.maxlen32768.tensor_cache -> train_lora.py
```

当前 v10 训练集：

```text
data/windows_v10_attn_tq_train600_seedA_c01_c04_c08_c16/windows.jsonl
data/windows_v10_attn_tq_train600_seedA_c01_c04_c08_c16/windows.maxlen32768.tensor_cache/
```

当前统计：

```text
total samples = 40566
c01 = 10187
c04 = 10183
c08 = 10152
c16 = 10044
```

窗口构造入口：

```bash
/data00/yinhaolang/infer/.venv/bin/python data/build_windows.py \
  --raw data/raw_trace_pool/activecore_train/c08_seedA \
  --out data/windows_v10_attn_tq_train600_c08 \
  --tq-max-len 32768 \
  --tq-target-windows 600 \
  --tq-min-uops-per-core 256 \
  --per-workload-cap 600 \
  --cache-max-len 32768 \
  --jobs 8
```

当前训练路线应分别对 `c01/c04/c08/c16 seedA` 并行构建，再合并：

```bash
mkdir -p data/windows_v10_attn_tq_train600_seedA_c01_c04_c08_c16
cat \
  data/windows_v10_attn_tq_train600_c01/windows.jsonl \
  data/windows_v10_attn_tq_train600_c04/windows.jsonl \
  data/windows_v10_attn_tq_train600_c08/windows.jsonl \
  data/windows_v10_attn_tq_train600_c16/windows.jsonl \
  > data/windows_v10_attn_tq_train600_seedA_c01_c04_c08_c16/windows.jsonl
```

如果 `build_windows.py` 构建单个子集时已经自动生成了子集 cache，合并后的总训练集仍必须重新生成总 cache，因为 cache metadata 绑定了 `jsonl_path/jsonl_size/jsonl_mtime_ns`。

## 4. 训练侧 Tensor Cache

训练侧第二层 cache：

```text
windows.maxlen32768.tensor_cache/
  manifest.pt
  shard-00000.pt
  ...
```

它保存 tokenized `input_ids/query_pos/is_uop/uop_fields/side_feats/attn_feats/labels/denoms` 等 tensor。训练脚本设置了 `require_cache=True`，缺 cache 或 cache metadata 不匹配会直接失败，不会静默重建。

构建命令：

```bash
nohup /data00/yinhaolang/infer/.venv/bin/python scripts/prepare_dataset_cache.py \
  --base-model Qwen/Qwen3-4B \
  --data data/windows_v10_attn_tq_train600_seedA_c01_c04_c08_c16/windows.jsonl \
  --max-len 32768 \
  --format tensor \
  --jobs 16 \
  --lines-per-shard 512 \
  > logs/cache_v10_attn_qwen3_4b_train600_seedA_c01_c04_c08_c16.nohup.log 2>&1 &
```

重建触发条件：

- `windows.jsonl` 内容、大小或 mtime 改了。
- `max_len` 改了。
- tokenizer/base model 改了，导致 tokenizer len 或 special token 布局变化。
- `PMU_KEYS`、label version、side feature keys、attention feature keys、`MAX_CORES` 改了。
- `train/dataset.py` 的 cache schema/`feat_version` 改了。

不需要重建：

- 只换训练 step 数。
- 只从已有 checkpoint 续训。
- 只改 eval 侧 `MAX_LEN`。

注意：4B 训练仍使用 `--max-len 32768`，因为当前训练数据和 tensor cache 是 32k 构建的。Qwen3-4B 部署 eval 可以用 40960，但这不改变训练 cache。

## 5. 训练

训练读取 `windows.jsonl` 并强制加载同目录 tensor cache：

```bash
nohup env CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7 \
PYTHONUNBUFFERED=1 HF_HUB_OFFLINE=1 \
/data00/yinhaolang/infer/.venv/bin/torchrun \
  --standalone --nproc_per_node=8 \
  train/train_lora.py \
  --base-model Qwen/Qwen3-4B \
  --data data/windows_v10_attn_tq_train600_seedA_c01_c04_c08_c16/windows.jsonl \
  --out ckpt/v10_attn_qwen3_4b_c01_c04_c08_c16_8000 \
  --steps 8000 \
  --bs 1 \
  --grad-accum 1 \
  --max-len 32768 \
  --val-frac 0.15 \
  --log-every 20 \
  --eval-every 500 \
  --eval-batches 0 \
  --num-workers 2 \
  > logs/train_v10_attn_qwen3_4b_c01_c04_c08_c16_8000.log 2>&1 &
```

Checkpoint 目录形态：

```text
ckpt/.../
  head_best.pt
  lora_best/
    adapter_model.safetensors
    adapter_config.json
```

当前脚本只保存 best，不保存每步 latest。中断后能验证的最新落盘权重是最近一次 `save_best_done` 对应的 checkpoint。

续训时：

- `--init-ckpt` 指向已有 best checkpoint 目录。
- `--skip-train-batches` 用来跳过已消费的 sampler 位置。
- `--steps` 是本次新增 optimizer step 数，不是总 step。

## 6. 部署侧推理验证

部署侧 eval 不读训练 windows，也不读训练 tensor cache。它从 raw trace/parquet 开始，在线切窗：

```text
raw trace / aligned parquet
  -> eval/eval_quota_cycles.py online planner
  -> per-window model inference
  -> aggregate CPI/PMU vs ROI baseline
```

主入口：

```bash
scripts/eval_parallel.sh
```

当前默认：

```text
MAX_LEN=40960        # 部署侧推理上下文，适配 Qwen3-4B
TRAIN_MAX_LEN=32768  # 训练上下文，仅用于日志提示
DT_TARGET=8000
DT_MAX=12000
nmin=256            # eval_quota_cycles.py 默认
nmin_floor_min=128
```

4B eval 必须显式传：

```text
BASE_MODEL=Qwen/Qwen3-4B
CKPT=<4B checkpoint dir>
```

示例：

```bash
nohup env HF_HUB_OFFLINE=1 \
CKPT=ckpt/v10_attn_qwen3_4b_c01_c04_c08_c16_8000_resume2500 \
BASE_MODEL=Qwen/Qwen3-4B \
RAW=data/raw_trace_pool/activecore_eval/c08_seedB_infer17 \
TAG=v10_qwen3_4b_step3500_c08_seedB_full_ctx40960 \
GPUS=0,1,2,3,4,5,6,7 \
MAX_WINDOWS=0 \
PROGRESS_EVERY=30 \
bash scripts/eval_parallel.sh \
> logs/eval_v10_qwen3_4b_step3500_c08_seedB_full_ctx40960.nohup.log 2>&1 &
```

如果要复现旧 32k 上下文结果，显式加：

```bash
MAX_LEN=32768
```

部署侧 eval cache 只要求 raw parquet cache；不需要也不会使用 `windows.maxlen32768.tensor_cache`。

## 7. Smoke Test 顺序

每次改 schema、模型加载、cache、planner 后按这个顺序做：

1. 语法检查：

```bash
/data00/yinhaolang/infer/.venv/bin/python -m py_compile \
  data/build_windows.py train/dataset.py train/train_lora.py \
  eval/eval_quota_cycles.py scripts/prepare_dataset_cache.py
bash -n scripts/eval_parallel.sh
bash -n scripts/prepare_quota_eval_cache.sh
```

2. raw parquet cache 检查：

```bash
find -L data/raw_trace_pool/activecore_eval/c08_seedB_infer17 \
  -name '*.aligned.parquet' | wc -l
```

期望数是 `workload_count * core_count`。17 个 workload 下：

```text
c04: 68
c06: 102
c08: 136
c16: 272
c32: 544
```

3. 训练 tensor cache 检查：

```bash
/data00/yinhaolang/infer/.venv/bin/python - <<'PY'
import torch
p = "data/windows_v10_attn_tq_train600_seedA_c01_c04_c08_c16/windows.maxlen32768.tensor_cache/manifest.pt"
m = torch.load(p, map_location="cpu")
print(m["format"], m["total_samples"])
print(m["meta"])
PY
```

4. deployment eval smoke：

```bash
nohup env HF_HUB_OFFLINE=1 \
CKPT=ckpt/v10_attn_qwen3_4b_c01_c04_c08_c16_8000_resume2500 \
BASE_MODEL=Qwen/Qwen3-4B \
RAW=data/raw_trace_pool/activecore_eval/c08_seedB_infer17 \
TAG=smoke_c08_ctx40960 \
GPUS=0,1,2,3,4,5,6,7 \
MAX_WINDOWS=2 \
PROGRESS_EVERY=20 \
bash scripts/eval_parallel.sh \
> logs/eval_smoke_c08_ctx40960.nohup.log 2>&1 &
```

只有 smoke 通过后再跑 full eval。

## 8. 快速判断该补哪种 cache

| 场景 | 需要补 aligned parquet | 需要补 tensor cache |
| --- | --- | --- |
| 训练新模型，windows.jsonl 未变 | 否 | 否 |
| 训练新模型，base model/tokenizer 变 | 否 | 是 |
| 训练集 windows.jsonl 重新生成 | 可选但建议已有 | 是 |
| eval 新 raw trace | 是 | 否 |
| eval 同 raw trace 换 ckpt | 否 | 否 |
| eval 从 32768 改 40960 | 否 | 否 |
| 改 functional feature/schema | 训练 raw 需重新 build windows | 是 |
| 改 label/PMU key/schema | 训练 raw 需重新 build windows | 是 |

原则：

- 训练只认 `windows.jsonl + tensor_cache`。
- 部署侧 eval 只认 `raw trace/aligned parquet + checkpoint`。
- `aligned.parquet` 加速 raw 读取和 labels merge。
- `tensor_cache` 加速训练 batch 读取，不能用于部署侧在线切窗。
