# TCSim v29 运维、恢复与排障

## 1. 标准产物目录

```text
configs/v29_100m.yaml                  基线模型/训练配置
data/v29_global_time_dataset/          packed caches + manifest
ckpt/tcsim_v29_packed3_100m_8gpu_60k/ best.pt last.pt metrics.json
logs/<v29-eval-run>/                    report、trace logs、worker state
tmp/                                    项目内临时 smoke/cache
vendor/v29/                             外部源码快照，不放生成数据
```

正式脚本、PID、日志不要放系统 `/tmp`；可删除的测试产物放项目 `tmp/`，便于确认归属。

## 2. 启动前检查

```bash
git status --short
test -f data/v29_global_time_dataset/manifest.json
test -f ckpt/tcsim_v29_packed3_100m_8gpu_60k/best.pt
/data00/yinhaolang/infer/.venv/bin/python -m pytest \
  tests/test_v29.py tests/test_v29_inference.py -q
nvidia-smi --query-gpu=index,memory.used,utilization.gpu --format=csv,noheader
```

再检查 checkpoint/cache 合同。最安全的方法是运行 1 trace/10-step smoke；loader 会验证
schema、feature dimensions、predictor/resource decoder hash 和 horizon 集。

## 3. 训练恢复

`best.pt` 用于部署，`last.pt` 用于恢复。恢复必须保持相同 manifest、config、world size 和
output directory：

```bash
RESUME_CKPT=ckpt/tcsim_v29_packed3_100m_8gpu_60k/last.pt \
MANIFEST=data/v29_global_time_dataset/manifest.json \
CONFIG=configs/v29_100m.yaml \
OUT=ckpt/tcsim_v29_packed3_100m_8gpu_60k \
STEPS=60000 GPUS=0,1,2,3,4,5,6,7 NPROC=8 \
bash scripts/run_v29_ddp8.sh
```

不要把 `--init-checkpoint` 当成 resume；它不恢复 optimizer、step、best/history。不要手工
复制另一实验的 `best.pt` 到基线目录。

## 4. 推理恢复

```bash
RESUME=1 OUT=logs/<same-run> \
CKPT=ckpt/tcsim_v29_packed3_100m_8gpu_60k/best.pt \
MANIFEST=data/v29_global_time_dataset/manifest.json \
SPLITS=seed0_inference MODE=free CORE_COUNTS=4,8,16,32 \
bash scripts/run_v29_eval_8gpu.sh
```

`.worker_state/*.traces.jsonl` 是恢复事实源。只有 checkpoint ID、trace length、mode、stride、
step limits、context builder、window parallel 和诊断开关完全一致才跳过已完成 trace。
更换任一参数时新建 OUT，不要手工编辑 worker state。

## 5. 常见故障

### gem5 commit 不匹配

`apply_and_build.sh` 只接受 v25.1.0.1。新建精确 checkout；不要去掉 commit gate。

### gem5 找不到 `uarch_profile.hh` / `lru_banked.hh`

必须通过归档 build 脚本编译，或手工设置：

```bash
export TAOGEN_SHARED=$PWD/vendor/v29/gem5_patch/shared
```

随后重新构建 `gem5.opt`，运行时设置变量不能修复已经缺少源码的二进制。

### gem5 启动时报 `libpython3.11.so.1.0` 缺失

gem5 链接的是构建时 Python。用同一个 Python 查询库目录：

```bash
PY=/path/to/build-time/python3.11
PYTHON_LIBDIR=$("$PY" -c 'import sysconfig; print(sysconfig.get_config_var("LIBDIR"))')
export LD_LIBRARY_PATH="$PYTHON_LIBDIR:/opt/gcc-11.5.0/lib64:${LD_LIBRARY_PATH:-}"
GEM5_PYTHONHOME=$("$PY" -c 'import sys; print(sys.base_prefix)')
PYTHONHOME="$GEM5_PYTHONHOME" ../gem5/build/X86_MESI_Three_Level/gem5.opt --build-info
```

如果仍失败，说明运行 Python 与构建 Python 不同，应重新构建 gem5。`PYTHONHOME` 只给
gem5 进程，不能全局 export 到后续训练/推理 Python。

### TaoTrace 空文件

检查 workload 是否真正执行 `m5_work_begin/end`、运行是否带 `--require-roi`、
`--ff-atomic` 后是否打印第一次 WORKBEGIN 切换、TaoTrace 是否挂在 switch O3 cores。
`roi_boundaries.jsonl` 比 records 更适合作为第一诊断点。

### 每核 trace 行数不一致或 end 后仍有记录

通常是旧的全局 ROI gate、workload 没有 WORKEND+quiesce，或 simulator 在最后一个
WORKEND 后继续执行 join/futex。必须使用归档的当前 TaoTrace 与 `run_mt_mvp.py`，重采 raw；
后处理不能可靠修复。

### raw audit 报 branch 字段错误

旧 taogen patch 曾把 target 写成自身 PC、history 固定移入 1。该数据不能转成 v29 合同，
必须用当前 overlay 重采。

### `no matching raw roots`

目录名必须含 `seedN_cXX`，且 workload 目录使用合同中的 `W_v28_*` 名。先展开 glob：

```bash
find data -maxdepth 1 -type d -name 'raw_v28_1_business_a2_sharedzipf_seed*_c*' | sort
```

### manifest status/quality fail

检查 manifest 的 `quality`、缺失 root/workload、duplicate roots、hardware profile violations、
build failures。不要直接改 JSON；修复 raw 或参数后重建。

### checkpoint/cache contract mismatch

常见原因是拿 long-history cache 加载 E0、改变 horizons/sample period、替换 predictor/decoder、
或用旧 packed2 checkpoint。按 checkpoint 记录的 config/contract 找回对应 cache；不能
`strict=False` 绕过。

### CUDA OOM

先保持 `batch_samples=1`，确认 BF16、8 ranks、每 rank 一卡；设置
`PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`。不要通过减 K 或 d_dyn 继续加载原
checkpoint，因为模型结构合同已改变。

### DDP rendezvous/端口冲突

使用 `scripts/run_v29_ddp8.sh` 的 `torchrun --standalone`，不要同时设置遗留固定
`MASTER_PORT`。确认没有上一轮 torchrun 残留进程。

### inference no-progress

检查日志中的 predicted gaps、finite/monotonicity、min-step warnings、active cores 和 cursor。
`min_step_cycles` 不能用来强行抬高 delta；这会越过未完成事件。重复 no-progress 达到
上限应作为模型/状态错误失败，而不是静默跳 UOP。

### 只有 pooled CPI 好看

同时检查 per-core endpoint、signed bias、cursor-interval drift 和 workload-equal groups。
核间正负误差会互相抵消，pooled aggregate 不是 trajectory 验收。

## 6. 回归测试矩阵

修改 v29 模型/数据/推理至少执行：

```bash
python -m pytest tests/test_v29.py tests/test_v29_inference.py -q
python -m pytest tests/test_invariants.py tests/test_deployment_inference.py -q
```

修改 gem5 decoder 或 resource mapping 再执行：

```bash
python scripts/audit_v29_decoder_differential.py \
  --trace-dir <one-tao-trace-dir> --gem5-root ../gem5 \
  --samples 10000 --out tmp/v29_decoder_audit.json
```

修改 context 向量化/cache 后要比较旧/新 pressure、summary、dynamic、relation 逐元素，
同 checkpoint/cursor 输出一致，并分别跑 c8/c32 至少 300-step smoke。

## 7. 数据和版本保护

- raw、aligned parquet、packed cache、checkpoint、report 都是不同层的事实源，不相互覆盖；
- 不删除 raw JSONL，直到 aligned audit 和新 cache 完整通过；
- 不把 `--overwrite` 当成修复工具；
- seed1 已用于 development evidence，不再称为 untouched final；
- v29 E0、long-history、frozen probe、LLMSim adapter、v30 使用独立目录和 run tag；
- vendor 归档更新时同步 `PROVENANCE.md` 与 SHA256，并重新跑 gem5 smoke。

## 8. 已知限制

E0 对大多数 base workload 已形成稳定基线，但 Redis heldout 仍约 40% 误差，branch heldout
误差也较高。long-history 和 frozen probe 证明存在部分 memory signal，但没有形成可靠的
跨 workload 改进。因此“v29 最优”指当前仓库中整体最稳的已完成版本，不表示所有
workload 达到生产精度，也不应把实验分支结果合并进基线 headline。
