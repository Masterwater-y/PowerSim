# 当前训练/推理回退到 v9

当前主线已回退到 v9 口径：

- tokenizer 新增 token 数：`738`
- checkpoint label version：`v9_l2_no_mshr_no_iside`
- 训练数据：`data/windows_v9_tq_train600_all/windows.jsonl`
- 默认 checkpoint：`ckpt/v9_tq_train600_8gpu_4000_resume1840_fastskip`
- 模型结构：Qwen3-0.6B + LoRA + composite uop encoder + `tstart_proj` + `side_proj`
- 当前不启用 v13 Gated SideMLP、v14 head-only loss、v12 attention feature tokens

## 保留的效率路径

回退的是模型/标签/loss 方案，不回退 v10-v14 中不改变 v9 语义的效率基础设施：

- `aligned parquet` raw cache 继续保留，用于训练 windows 构建和部署侧 eval 快速读取。
- 训练数据 cache 默认优先使用 `windows.maxlen32768.tensor_cache/`；如果不存在，则兼容旧的 `windows.maxlen32768.ids_cache/`。
- `scripts/prepare_dataset_cache.py` 支持 `--format tensor|ids`、`--base-model` 和并行 `--jobs`。
- `train/train_lora.py` 支持显式 `--cache-path` 和 `--base-model`，避免 0.6B/4B 或多份 cache 混用。
- `scripts/finalize_v9_parquet_cache.sh` 默认重建 tensor cache。

## 训练

```bash
nohup bash scripts/run_current_train.sh \
  > logs/train_current_v9.nohup.log 2>&1 &
```

等价于：

```bash
nohup bash scripts/run_v9_train_qwen3_0p6b.sh \
  > logs/train_v9_tq_train600_4000_rerun.nohup.log 2>&1 &
```

默认参数：

```text
DATA=data/windows_v9_tq_train600_all/windows.jsonl
OUT=ckpt/v9_tq_train600_8gpu_4000_rerun
STEPS=4000
GPUS=0,1,2,3,4,5,6,7
MAX_LEN=32768
BS=1
GRAD_ACCUM=1
EVAL_EVERY=200
use_tstart=1
```

## 推理验证

```bash
nohup bash scripts/run_current_eval_sweep.sh \
  > logs/eval_current_v9_sweep.nohup.log 2>&1 &
```

等价于：

```bash
nohup bash scripts/run_v9_eval_sweep.sh \
  > logs/eval_v9_sweep.nohup.log 2>&1 &
```

默认跑 `c04 c08`，使用 seedB：

```text
CKPT=ckpt/v9_tq_train600_8gpu_4000_resume1840_fastskip
CORES=04 08
MAX_LEN=32768
MAX_WINDOWS=0
```

复现历史 c06：

```bash
nohup env CORES=06 bash scripts/run_v9_eval_sweep.sh \
  > logs/eval_v9_c06_seedC.nohup.log 2>&1 &
```
