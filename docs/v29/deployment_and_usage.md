# TCSim v29 部署与使用

本文给出 v29 E0 packed3 的端到端复现路径：环境 → gem5 patch/build → workload → raw
trace → aligned parquet → v29 cache/manifest → 8-GPU 训练 → oracle/free 推理 → 报告。

## 1. 版本和目录约定

建议工作区布局：

```text
<workspace>/
  TCSim/                  本仓库
  gem5/                   gem5 v25.1.0.1 + vendor/v29/gem5_patch overlay
```

所有命令默认从 TCSim 根目录执行：

```bash
cd /data00/yinhaolang/TCSim
export TCSIM_ROOT=$PWD
export PY=/data00/yinhaolang/infer/.venv/bin/python
export TORCHRUN=/data00/yinhaolang/infer/.venv/bin/torchrun
export GEM5_ROOT=/data00/yinhaolang/gem5
export PYTHON_LIBDIR=$("$PY" -c 'import sysconfig; print(sysconfig.get_config_var("LIBDIR"))')
export GEM5_PYTHONHOME=$("$PY" -c 'import sys; print(sys.base_prefix)')
export LD_LIBRARY_PATH="$PYTHON_LIBDIR:/opt/gcc-11.5.0/lib64:${LD_LIBRARY_PATH:-}"
```

不要把路径写成数据合同。上述环境变量只是当前机器示例；manifest、checkpoint 和 cache
内部合同由 schema/hash 决定。

## 2. 软件与硬件环境

训练/推理所需 Python 包：PyTorch、NumPy、PyArrow；PyYAML 推荐安装。当前验证环境是
Python 3.11.14、PyTorch 2.12.0+cu130、NumPy 2.4.6、PyArrow 24.0.0、PyYAML 6.0.3。
PyTorch 应按机器 CUDA/driver 安装，不在 `requirements-v29.txt` 中硬编码。

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-v29.txt
# 再按 CUDA 环境安装对应 torch wheel
```

正式推理前构建 v29 C++ fused context 后端：

```bash
"$PY" scripts/build_v29_context_native.py
TCSIM_CONTEXT_BACKEND=native "$PY" -c \
  'from tcsim.v29.dataset import CONTEXT_BUILDER; print(CONTEXT_BUILDER)'
# 应输出 cpp-fused-context-v4
```

`scripts/run_v29_eval_8gpu.sh` 和 `scripts/run_v29_window_parallel_4gpu.sh` 默认要求 native，
并会在扩展缺失或源码更新后自动重建。诊断时可显式设置
`TCSIM_CONTEXT_BACKEND=python` 使用逐项等价的 NumPy reference；`auto` 则在扩展不可用时
回退。真实 cache 的非 pytest 等价性与性能检查可运行：

```bash
"$PY" scripts/benchmark_v29_context_native.py \
  --cache <v29-trace-cache> --verify-samples 24 \
  --benchmark-samples 96 --warmup 5
```

实验性的 shared-K/V FlexAttention 后端可通过下面的参数启用：

```bash
CROSS_ATTENTION_BACKEND=flex_shared_kv bash scripts/run_v29_eval_8gpu.sh
# 或直接给 infer_v29.py 传 --cross-attention-backend flex_shared_kv
```

它只在活动窗口全部为完整 K=256 时使用共享 K/V block mask；partial/tail window 自动回退
`legacy`。H20 验收显示它可减少约 992 MB 临时显存，但 cross kernel 仅加速 1.127x，
所以 launcher 默认仍为 `legacy`。无需 GPU 的等价性门禁和有 GPU 时的 c32 kernel A/B
使用同一脚本：

```bash
"$PY" scripts/benchmark_v29_cross_attention.py
```

eval-only Q/R/K/V 单 GEMM 融合可独立启用，也可与 shared-K/V 叠加：

```bash
QRKV_PROJECTION_BACKEND=fused bash scripts/run_v29_eval_8gpu.sh

CROSS_ATTENTION_BACKEND=flex_shared_kv \
QRKV_PROJECTION_BACKEND=fused \
bash scripts/run_v29_eval_8gpu.sh

"$PY" scripts/benchmark_v29_qrkv_projection.py
```

launcher 默认仍为 `separate`。`fused` 不修改 checkpoint state dict，训练模式自动回退原
四个 Linear；部署报告会分别记录 fused/separate 的逐层命中次数。

使用同一条 c32 trace 做 300-step baseline/optimized 端到端 A/B：

```bash
bash scripts/benchmark_v29_forward_rollout.sh
```

该 A/B 默认使用 `data/v30_gss_commit_dataset/manifest.json` 并设置
`GSS_PMU_ONLY=1`：v29 负责 timing/CPI，canonical GSS 只负责 cache-miss PMU。直接部署时
也可显式设置：

```bash
MANIFEST=data/v30_gss_commit_dataset/manifest.json \
GSS_PMU_ONLY=1 MODE=free TARGET_STRIDE=256 \
bash scripts/run_v29_eval_8gpu.sh
```

v30 Exposure-v1 + online GSS 的单卡 c32 A/B 使用：

```bash
bash scripts/launch_v30_gss_forward_rollout_benchmark_nohup.sh
```

gem5 构建还需要 Git、GCC/G++、Python 开发头、SCons。当前可复现环境使用 GCC 11.5、
SCons 3.0.1；新版本工具可用，但必须重新跑 smoke 和 decoder differential audit。
如果 `gem5.opt` 报 `libpython3.11.so.1.0` 缺失，先按上面的 `sysconfig.LIBDIR` 设置
`LD_LIBRARY_PATH`；如果提示找不到 platform-dependent libraries，再仅对 gem5 进程设置
`PYTHONHOME=$GEM5_PYTHONHOME`。不要把 `PYTHONHOME` 全局 export 后继续运行 venv Python，
也不要复制系统中另一 Python 版本的动态库。

正式 100M 配置面向 8 张 CUDA GPU；CPU 或单卡只建议 smoke。BF16、112M 参数模型和
checkpoint optimizer state 会占用显著显存/主存，正式训练前先确认 8 张卡均空闲。

## 3. gem5 patch 与构建

### 3.1 准备精确基线

```bash
git clone --branch v25.1.0.1 --depth 1 \
  https://github.com/gem5/gem5.git "$GEM5_ROOT"
git -C "$GEM5_ROOT" rev-parse HEAD
# 必须是 c8222cc67a399bfc01e8658dd14b30d5bfd634f9
```

如果目录已经存在，不要在含有其他研究修改的 checkout 上覆盖 patch；新建专用 checkout。

### 3.2 应用 overlay 并构建

```bash
JOBS=64 PYTHON=/usr/bin/python3.11 \
  bash vendor/v29/gem5_patch/apply_and_build.sh "$GEM5_ROOT"
```

脚本会：

1. 校验 gem5 commit 和归档文件 SHA256；
2. 安装 TaoTrace、BranchEvents、ROI hook、Ruby message-buffer 修复和 build option；
3. 用归档的 `shared/` 头编译 `X86_MESI_Three_Level`；
4. 生成 `$GEM5_ROOT/build/X86_MESI_Three_Level/gem5.opt`。

patch 的语义与文件说明见 `vendor/v29/gem5_patch/README.md`。不能使用 taogen 已提交的旧
`tao_trace.*` 代替：旧快照缺少 v28.1 逐核 ROI 和真实 branch functional 字段。

### 3.3 gem5 最小运行

先构建 workload（下一节），再运行 c1 smoke：

```bash
mkdir -p tmp/v29_gem5_smoke
PYTHONHOME="$GEM5_PYTHONHOME" \
"$GEM5_ROOT/build/X86_MESI_Three_Level/gem5.opt" \
  --outdir=tmp/v29_gem5_smoke --redirect-stdout --redirect-stderr \
  vendor/v29/gem5_patch/config/run_mt_mvp.py \
  --cmd vendor/v29/trace_collection/workloads/bin/v28_int_alu_dense \
  --workload-args 1 1 1 0 \
  --num-cores 1 --l2-size 1MiB --l3-size 8MiB \
  --num-l3-banks 8 --mem-channels 8 --require-roi --ff-atomic
```

至少检查：

```bash
test -f tmp/v29_gem5_smoke/uarch_profile.json
test -f tmp/v29_gem5_smoke/tao_trace/roi_boundaries.jsonl
find tmp/v29_gem5_smoke/tao_trace -name '*.records.micro.jsonl' -size +0
find tmp/v29_gem5_smoke/tao_trace -name '*.labels.micro.jsonl' -size +0
rg 'first WORKBEGIN -> switch Atomic -> O3\+Ruby' tmp/v29_gem5_smoke/simout.txt
```

`--ff-atomic` 是 ROI 前用 AtomicSimpleCPU 快速启动；它不表示 ROI 内允许 atomic UOP。
正式合同同时要求 `ff_atomic_verified=true` 和 `roi_atomic_uops=0`。

## 4. Workloads

v29 使用 23 个 v28 business workload：16 个 train/base + 7 个 heldout variant。它们由
同一份 `v28_business_proxy.c` 通过编译宏生成，ABI 为：

```text
./workload <nthreads> <scale> 1 <seed>
```

构建静态二进制：

```bash
make -C vendor/v29/trace_collection/workloads/v28 all
file vendor/v29/trace_collection/workloads/bin/v28_int_alu_dense
```

默认使用 `-O2 -static -pthread -msse2`。若系统缺少静态 libc，需要安装对应开发包；不要
直接改成动态链接后沿用旧结果，因为启动路径和 ROI 前状态会变化。

机器可读 workload 划分在 `configs/v28_business_workloads.json`。命名中的 `v28` 是 trace
suite 来源，模型/evaluator 仍是 v29。

## 5. Raw trace 采集与对齐

### 5.1 单个 core-count slice

```bash
GEM5_ROOT="$GEM5_ROOT" PY="$PY" \
NUM_CORES=4 SEED=0 PARALLEL=4 FF_ATOMIC=1 \
STRICT_NATURAL_ROI=1 RUN_TO_COMPLETION=1 REUSE_PROBE_IF_SUFFICIENT=0 \
TARGET_PER_CORE=750000 MIN_ACCEPT_PER_CORE=500000 MAX_ACCEPT_PER_CORE=1000000 \
L2_SIZE=1MiB L3_SIZE=8MiB NUM_L3_BANKS=8 MEM_CHANNELS=8 \
OUT_BASE="$TCSIM_ROOT/data/raw_v28_1_business_a2_sharedzipf_seed0_c04" \
bash vendor/v29/trace_collection/scripts/collect_v28_workloads.sh all
```

正式数据需要 seed0 的 c1/c4/c8/c16/c32，以及 seed1 的 c4/c8/c16/c32。采集器会先 probe、
估算 scale、检查每核行数并行运行 workload。不要把 c1 放进 validation/deployment headline。

### 5.2 批量编排

项目已有经过使用的批量编排器：

```bash
TSIM_ROOT="$TCSIM_ROOT/vendor/v29/trace_collection" \
DATASET_TAG=v28_1_business_a2_sharedzipf \
CORES_LIST="1 4 8 16 32" SEEDS=0 MODE=all \
COLLECT_PARALLEL=23 CONVERT_PARALLEL=23 \
bash scripts/tmp/run_v28_business_serial_cores_collect.sh
```

该脚本会对每个 slice 采集、调用 `convert_trace_to_aligned_parquet.py`、检查期望文件数，
可在对齐成功后删除体积更大的 raw records/labels JSONL。首次部署建议保留原 JSONL，直到
audit、cache build 和 smoke 全部通过。

### 5.3 Raw 硬门禁

```bash
"$PY" scripts/audit_v28_raw_dataset.py \
  --root-glob "$TCSIM_ROOT/data/raw_v28_1_business_a2_sharedzipf_seed*_c*" \
  --sample-regions 9 --sample-uops-per-core 8192 \
  --contract-file configs/v28_business_workloads.json \
  --out data/v28_1_business_a2_sharedzipf_raw_audit.json
```

必须满足：每核一对 begin/end、同步 ROI start、end 后无记录、records/labels 对齐、真实
branch functional 字段自洽、branch miss 只落在退休 control UOP、每核 0.5M–1M UOP、
full UOP-CPI ≤ 10、ROI atomic UOP=0。失败时不要用 `--overwrite` 绕过。

## 6. 构建 v29 cache 与 manifest

正式一键入口：

```bash
RAW_ROOT_GLOB="$TCSIM_ROOT/data/raw_v28_1_business_a2_sharedzipf_seed*_c*" \
OUT=data/v29_global_time_dataset WORKERS=64 \
PY="$PY" bash scripts/build_v29_full.sh
```

等价显式命令：

```bash
"$PY" scripts/build_v29_dataset.py \
  --raw-root-glob "$TCSIM_ROOT/data/raw_v28_1_business_a2_sharedzipf_seed*_c*" \
  --out data/v29_global_time_dataset \
  --contract-file configs/v28_business_workloads.json \
  --horizons 16,32,64,128,256,512,1024 \
  --sample-period-cycles 64 --block-cycles 65536 \
  --min-uops-per-core 500000 --max-uops-per-core 1000000 \
  --max-full-uop-cpi 10 --workers 64 \
  --seeds 0,1 --core-counts 1,4,8,16,32 --fail-fast
```

产物：

```text
data/v29_global_time_dataset/
  manifest.json
  traces/<raw-root>/<workload>/
    meta.json
    sample_cursors.npy sample_ticks.npy
    cores/<core-id>/
      fields.npy resource.npy resource_compact.npy
      commit_tick.npy branch_miss.npy ...
```

当前完整 manifest 分区为 train=80、validation=64、development_heldout=28、
seed0_inference=115、deployment_inference=92、final_untouched=0。训练只接受 manifest 的非泄漏
划分；`--allow-unpartitioned-cache-root` 只允许诊断/smoke。

构建后检查：

```bash
"$PY" -c 'import json; p=json.load(open("data/v29_global_time_dataset/manifest.json")); print(p["schema_version"], {k: len(v) for k,v in p["splits"].items()})'
```

需要部署时完全不带 label，可用 `scripts/build_v29_functional_cache.py` 从 functional
parquet 生成容器；磁盘中不得出现 `commit_tick.npy`、`branch_miss.npy`。

## 7. Smoke

代码单测：

```bash
"$PY" -m pytest tests/test_v29.py tests/test_v29_inference.py -q
```

真实 trace 的 build → backward → 1-step train → checkpoint → oracle/free smoke：

```bash
"$PY" scripts/smoke_v29_end_to_end.py \
  --trace-dir data/raw_v28_1_business_a2_sharedzipf_seed0_c01/W_v28_int_alu_dense/tao_trace \
  --out tmp/v29_e2e_smoke --max-samples 16 --device cpu
```

如果 raw 目录只保留 aligned parquet，可直接用正式 cache 做训练/推理小步检查。

## 8. 训练

基线配置为 `configs/v29_100m.yaml`：K=256，7 horizons，sequence length/stride=4，
`d_static=768`、`d_dyn=960`、15 heads、8 layers、FFN=3840、dropout=0.1、BF16。

8-GPU 60k 复现命令：

```bash
MANIFEST=data/v29_global_time_dataset/manifest.json \
CONFIG=configs/v29_100m.yaml \
OUT=ckpt/tcsim_v29_packed3_100m_8gpu_60k \
STEPS=60000 GPUS=0,1,2,3,4,5,6,7 NPROC=8 \
TORCHRUN="$TORCHRUN" \
bash scripts/run_v29_ddp8.sh
```

断点恢复：

```bash
RESUME_CKPT=ckpt/tcsim_v29_packed3_100m_8gpu_60k/last.pt \
OUT=ckpt/tcsim_v29_packed3_100m_8gpu_60k STEPS=60000 \
bash scripts/run_v29_ddp8.sh
```

`RESUME_CKPT` 恢复 model、optimizer、step、history 和 RNG 相关训练状态；
`INIT_CHECKPOINT` 用于新 probe 或显式声明的 architecture student 从基线权重冷启动，
两者互斥。普通基线训练不要用 `INIT_CHECKPOINT`。

### 8.1 从头端到端训练 hierarchical-latent

32-latent/core 模型全部随机初始化，不导入 canonical v29 或旧 latent 权重，不使用
任何外部模型监督，也不分阶段冻结。所有 112M 参数从 step 1 同时更新。latent 三段
attention 保留独立 LayerNorm 和训练期 FP32；跨核 residual 只使用原有
relation/state 动态 `cross_gate`，没有额外 scalar gate。

配置文件为 `configs/v29_latent32_scratch_100m.yaml`。

```bash
bash scripts/launch_v29_latent32_train_nohup.sh
```

默认训练至 90,000 step，输出
`ckpt/tcsim_v29_latent32_scratch_100m_8gpu_90k`；学习率从 0 线性 warmup 2K step
至 `1e-4`，随后 cosine decay，90K 时达到 `1e-5`。step
60,000 额外保存不被覆盖的 `step_60000.pt`。通过以下命令观察日志（启动器会打印实际
日志路径）：

```bash
tail -f logs/v29_latent32_train_*.nohup.log
```

先运行 5,000-step 稳定性 pilot：

```bash
STEPS=5000 bash scripts/launch_v29_latent32_train_nohup.sh
```

确认 5K 全程有限且 validation 正常后，在同一输出目录保留 optimizer 和 LR schedule
状态续跑：

```bash
RESUME_CKPT=ckpt/tcsim_v29_latent32_scratch_100m_8gpu_90k/last.pt \
STEPS=90000 bash scripts/launch_v29_latent32_train_nohup.sh
```

初次训练禁止设置 `INIT_CHECKPOINT`；只有该 scratch 链路自身生成且通过 finite guard
的 checkpoint 才能通过 `RESUME_CKPT` 继续。

训练循环在 forward、DDP backward/all-reduce 和 optimizer step 后执行全 rank 一致的
finite guard；首次异常会写 `nonfinite_step_<step>_<stage>.json` 并同时终止所有 rank。
保存 `best.pt`、`last.pt` 或 milestone 前还会检查模型及 Adam state，拒绝污染
checkpoint。

scratch checkpoint 已内嵌
`cross_attention_backend=hierarchical_latent` 和 `cross_latent_count=32`，评估脚本不指定
`CROSS_ATTENTION_BACKEND` 时会遵循 checkpoint 配置。

### 8.2 从头训练 Query-preserving remote-K/V16

精度优先的后续结构保留全部 target UOP Query，只将每个远端核心压缩为 8 个 positional
和 8 个 learned content K/V anchors，并只在第 4、8 层执行 cross attention。实现、复杂度、
H20 微基准、cache 合同和完整门禁见
`docs/v29/query_preserving_kv_design_and_training.md`。

正式 60K 训练使用已有 pass-quality cache，启动器会先审计 heldout 未泄漏，然后后台启动
8 GPU 训练：

```bash
bash scripts/launch_v29_query_kv16_scratch_60k_nohup.sh
```

默认输出为 `ckpt/tcsim_v29_query_kv16_scratch_100m_8gpu_60k`，step 30,000 永久保存
`step_30000.pt`。

关键输出：`best.pt`、`last.pt`、`metrics.json`。已完成的 60K scratch 实验中最优
`best.pt` 是 step 55000，validation total 0.461152。完整训练、吞吐量、CPI 和 heldout
结果见 `docs/v29/latent32_scratch_results.md`。checkpoint 必须同时匹配 cache contract、predictor hash、
resource decoder hash、horizons 和 schema。

## 9. 推理与评估

### 9.1 单 trace/单进程入口

```bash
"$PY" scripts/infer_v29.py \
  --ckpt ckpt/tcsim_v29_packed3_100m_8gpu_60k/best.pt \
  --manifest data/v29_global_time_dataset/manifest.json \
  --splits seed0_inference --out logs/v29_smoke \
  --mode free --device cuda --amp-dtype bf16 --sdpa-backend auto \
  --core-counts 4 --workloads W_v28_int_alu_dense \
  --max-free-steps 100 --target-stride 256 \
  --min-step-cycles 4 --max-step-cycles 1024 \
  --max-no-progress-steps 64 --progress-every 20
```

`free` 是部署主结果，只能用预测 cursor 构造下一步上下文。`oracle` 是 teacher-conditioned
head 诊断；`both` 同时运行。设置 `max_free_steps` 的结果是 prefix smoke，不能写成完整
ROI headline。

### 9.2 8-GPU 全量评估

```bash
CKPT=ckpt/tcsim_v29_packed3_100m_8gpu_60k/best.pt \
MANIFEST=data/v29_global_time_dataset/manifest.json \
OUT=logs/v29_packed3_free_s256_seed0_seed1_c04_c08_c16_c32_full \
SPLITS=seed0_inference,deployment_inference,development_heldout \
MODE=free CORE_COUNTS=4,8,16,32 TARGET_STRIDE=256 \
MAX_FREE_STEPS=0 RESUME=1 GPUS=0,1,2,3,4,5,6,7 \
bash scripts/run_v29_eval_8gpu.sh
```

launcher 按 trace 分片到 GPU，结果写入 `trace_logs/` 和 `.worker_state/`，最后由
`scripts/merge_v29_reports.py` 生成 `report.json`、`report.txt`。`RESUME=1` 只复用 checkpoint
ID 和完整 evaluation contract 一致的已完成 trace。

### 9.3 单 trace 多 GPU

默认 `WINDOW_PARALLEL_MODE=serial` 最稳妥。`unconditional` 和 `speculative` 是重叠窗口
加速模式，必须同时指定 lane devices、shift 和 process context backend。它们改变
evaluation/resume contract，不能与 serial worker state 混用。详细语义见
`docs/v29_single_trace_multi_gpu_window_parallel.md`。

## 10. 结果验收

正式结果至少同时看：

- complete full-ROI trace 数与 UOP/macro 覆盖率；
- workload-equal micro/macro ROI-CPI relative error；
- per-core endpoint/makespan、MAPE 分位数和 signed bias；
- branch miss count/rate、BCE/AUC/Brier/校准；
- exact-once、no-progress、monotonicity violation；
- free-running cursor interval drift，而不只看 aggregate CPI；
- UOP/s、step latency、context/model/D2H/scheduler 分项、CPU/GPU cache。

当前 packed3 报告的全 workload macro ROI mean error 为 c4 5.08%、c8 4.62%、c16
4.45%、c32 5.27%；heldout 和 Redis 明显更差。它是当前最优稳定基线，不代表所有业务
机制已经解决。完整评价见 `docs/v29_packed3_checkpoint_evaluation_report.md`。

## 11. 最短使用路径

已有 cache 和 checkpoint 时，日常用户只需：

```bash
cd /data00/yinhaolang/TCSim
"$PY" -m pytest tests/test_v29.py tests/test_v29_inference.py -q

CKPT=ckpt/tcsim_v29_packed3_100m_8gpu_60k/best.pt \
MANIFEST=data/v29_global_time_dataset/manifest.json \
SPLITS=seed0_inference MODE=free CORE_COUNTS=4,8 \
MAX_FREE_STEPS=100 GPUS=0,1 \
bash scripts/run_v29_eval_8gpu.sh
```

需要重建整条流水线时，从第 3 节开始，不能跳过 raw audit、manifest 分区或 checkpoint
合同检查。
