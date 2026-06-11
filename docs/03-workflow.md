# 端到端流程串接

> 本文档给出从空机器到产出端到端预测报告的完整命令链。
> 默认根目录：`MTAO`
>
> functional trace 驱动的 cache/coherence 属性与 gem5/Ruby oracle 的语义边界、
> timing-aware functional ref-sim 改造方案见
> [06-timing-aware-functional-refsim.md](06-timing-aware-functional-refsim.md)。

---

## 0. 一次性环境准备

```bash
cd <MTAO>/tao_cpu_sim

# 1) 激活 yinhaolang conda 环境
conda activate yinhaolang   # 或 source /root/miniconda3/envs/yinhaolang/bin/activate

# 2) 设环境变量（PYTHONPATH / CUDA / TAO_*）
source scripts/env.sh

# 3) 一次性 build（gem5 + ref_sim_py.so）
make build
```

`scripts/env.sh` 导出的关键变量：

| 变量 | 用途 | 默认值 |
|---|---|---|
| `TAO_ROOT` | 项目根 | `$(pwd)` |
| `TAO_DATA_ROOT` | parquet 数据根 | `$TAO_ROOT/datagen/data`、`$TAO_ROOT/infer/data` |
| `TAO_CKPT_ROOT` | ckpt 根 | `$TAO_ROOT/train/ckpt` |
| `TAO_INFER_DEVICE` | 推理设备 | `cuda` |
| `TAO_INFER_CUDA_DEVICES` | 可见 GPU | `0` |
| `TAO_QUANTUM_CYCLES` | 默认 quantum Δt | `256` |

## 1. 阶段 1 · 数据生成

```bash
# 50M 行均衡数据集（W11..W15，每个 10M）
bash scripts/01_taogen_collect.sh \
     --workloads W11 W12 W13 W14 W15 \
     --rows-per-workload 10000000

# 产物：$TAO_DATA_ROOT/{W11..W15}_*/parquet/*.parquet
```

烟囱档（3M 行）：

```bash
bash scripts/01_taogen_collect.sh --profile 3m
```

## 1b. 阶段 1.5 · 验证数据切片与固定 baseline

当目标是做 `driver` 侧验证，而不是继续训练时，推荐把 raw gem5 run
切成固定窗口数据集。**从这一阶段开始，`cut_baseline.json` 是固定产物，不是可选附件。**

推荐入口：

```bash
# 例：W11-W15 四核，每核前 100K records
bash scripts/07_validate_w11_w15_100k.sh \
     --workloads "W11_stream_mix W12_stencil2d W13_graph_walk W14_branch_state W15_indirect" \
     --rows-per-core 100000 \
     --infer-device cpu
```

这条链会依次执行：

```
1. _slice_trace_prefix.py         raw run -> tao_trace 窗口切片
2. extract_from_records.py        tao_trace -> functional_parquet / labels_parquet
3. _build_cut_baseline.py         tao_trace + stats.txt -> cut_baseline.json
4. inference_driver.py            functional_parquet -> infer.jsonl / report.json
5. _driver_eval_against_trace.py  driver 输出 vs cut-window truth -> 误差摘要
```

其中第 1~3 步构成**固定的数据准备阶段**。即使暂时不跑 driver，
生成出的数据目录也必须至少包含：

```
<dataset_dir>/
├── tao_trace/
├── functional_parquet/manifest.json
├── labels_parquet/labels.core{0..3}.parquet
├── slice_summary.json
├── cut_baseline.json
├── uarch_profile.json
└── stats.txt               # full-run 参考，不是 cut-window baseline
```

`cut_baseline.json` 的口径约定：

- 它是 **cut-window-derived baseline**
- `CPI` 来自 labels 的 `first_fetch_tick / last_commit_tick` 近似换算
- `branch_mispred` 来自 cut-window 内已提交 branch 的 `labels.mispredicted`
- `stats.txt` 只保留为 **full-run reference**，不能直接替代 `cut_baseline.json`

## 2. 阶段 2 · 模型训练

```bash
# 全量训练（默认 100K 步，自动 mispred_pos_weight 估算）
bash scripts/02_train.sh

# 烟囱测试（5 步）
bash scripts/02_train.sh --smoke

# 自定义 ckpt 名
bash scripts/02_train.sh --run-name 0603_v10_3
# 产物：$TAO_CKPT_ROOT/0603_v10_3.pt + .log
```

## 3. 阶段 3a · 验证侧（带 oracle bit-exact，旧链路）

6 步流水线：

```bash
bash scripts/03_validate.sh \
     --trace-dir <gem5_records_micro_dir> \
     --ckpt $TAO_CKPT_ROOT/0602.pt
```

流水线步骤：

```
1. derive_mem_events           records.micro → mem_events
2. ref_sim                     mesi_ref_sim → oracle 17 字段
3. bit-exact check             ref_sim 输出 vs detailed records 17 字段必须完全一致
4. build_inference_input       拼接 functional + ref_sim 为模型输入
5. ml/infer.py                 单步预测 fl/el/mispred
6. compare_pred_vs_truth + synthesize_cpi + pmu_report
```

## 4. 阶段 3b · 部署侧（仅 functional）

```bash
# 模式 A：真实 ckpt 推理（生产模式）
bash scripts/04_infer.sh \
     --trace-dir <gem5_records_micro_dir> \
     --ckpt $TAO_CKPT_ROOT/0602.pt \
     --mode ckpt \
     --quantum-cycles 256

# 模式 B：label-driven（用 labels 真值替代模型，校验 driver 自身）
bash scripts/04_infer.sh --trace-dir <...> --mode label

# 模式 C：mock-model（固定预测，最小联调）
bash scripts/04_infer.sh --trace-dir <...> --mode mock
```

## 4b. 当前推荐验证口径（统一走 driver）

后续只要说“验证”，默认指这条口径：

1. 输入边界：`functional_parquet`
2. baseline：同目录固定产物 `cut_baseline.json`
3. 执行器：`driver/inference_driver.py`
4. 输出：
   - 吞吐量：`report.json + time.txt`
   - 误差：`driver_validation_summary.json`

注意：

- 不再把 `stats.txt` 当作 cut-window baseline
- `stats.txt` 仅作为 full-run 参考保留
- `ml/infer.py` 属于离线验证工具链，不是默认验证入口

## 4c. PMU 对齐现状与下一步（2026-06-05）

当前关于 `driver PMU vs gem5 oracle PMU` 的结论，统一记录如下。

### 已确认结论

1. **i-side 不能作为当前 PMU 对账对象**
   - `functional_parquet` 是 retire/commit 序列，不是真实 ifetch 流。
   - 因此 driver 无法从 `macro_pc` 反推出 oracle 同语义的 ifetch 事件频率。
   - 当前口径下，PMU 对账只看 **d-side**；i-side PMU 已从 driver 对账中剔除。

2. **d-side 理论上可以对齐**
   - 对 d-side 来说，`functional_parquet` 已提供 `core_id / paddr / cacheline_paddr / is_load / is_store`。
   - 因此 `functional trace` 本身**不是** d-side 对齐的硬限制。

3. **ROI baseline 不适合作为 PMU 对账基线**
   - 早先使用 ROI 数据集时，oracle 窗口起点已经带有 warm cache / directory 状态。
   - driver 从空状态 replay 同一窗口，会天然产生分布偏差。
   - 后续用于 PMU 对账的 baseline，必须优先使用 `taogen/scripts/run_inference_baseline.sh` 产出的 **no-ROI / full-trace** run。

4. **no-ROI baseline 仍未让 driver d-side 自动收敛**
   - 已用 W11 no-ROI baseline 跑过 `scripts/07_validate_w11_w15_100k.sh`。
   - 结果：`driver_pmu_eval.bit_exact_metrics = 0 / 12`，d-side 仍未与 oracle 对齐。
   - 代表性偏差：
     - `llc.load_misses: 8464 vs 1`
     - `cha.tor_inserts.ia_miss_drd: 8464 vs 1`
     - `llc.store_misses: 3866 vs 0`
     - `l1d.store_misses: 4439 vs 5009`
     - `l2.misses: 14506 vs 15021`
     - `cha.dir_lookup.snp: 1715 vs 1569`

### 当前判断

到这里可以明确：

- **d-side 不能对齐的根因，已经不是 ROI baseline，也不是 functional trace 输入层级不够。**
- 当前问题在 **driver/ref_sim 的 d-side coherence 语义实现**，与 gem5 `tao_trace.cc` 产出的 `coh_oracle`/PMU 语义不一致。

### 接下来要做的事

1. **对齐 d-side coherence 分类规则**
   - 对照 `taogen/gem5_patches/src/cpu/o3/probe/tao_trace.cc`
   - 检查 `tao_cpu_sim/infer/mesi_ref_sim/include/simulator.hpp` 中 d-side `stepImpl()` / `coh_oracle` 判定
   - 逐分支找出 `REMOTE_HIT_* / LLC_HIT / DRAM / WB_REQUIRED` 的映射差异

2. **补 driver 路径的 d-side coh 直方图诊断**
   - 统计 driver replay 后的 load/store `coh_oracle` 直方图
   - 与 oracle `all_mem_events.merged.jsonl` 的 request `coh_oracle` 直方图逐类对比
   - 先对齐 `coh` 分布，再谈 PMU bit-exact

3. **对齐 PMU 聚合规则**
   - 对照 `taogen/mesi_ref_sim/scripts/pmu_report.py`
   - 修正 `tao_cpu_sim/infer/mesi_ref_sim/src/quantum.cc` 的 `accumulateD()`
   - 确保 `l1d.* / l2.misses / llc.* / cha.*` 的 set 判断与 oracle 一致

4. **验证顺序**
   - 先只复测 W11 no-ROI 100K
   - d-side `coh` 和 PMU 收敛后，再扩到 W12-W15

### 当前推荐命令

采 no-ROI baseline：

```bash
cd <MTAO>/taogen

OUT=MTAO/datagen/tmp/w11_no_roi_100k_$(date +%Y%m%d_%H%M%S)
GCC11_LIB=/opt/gcc-11/lib64 \
PY38_LIB=/root/miniconda3/envs/yinhaolang/lib \
WORKLOAD=W11_stream_mix \
WL_ARGS_W11="4 47 256 1 11" \
bash scripts/run_inference_baseline.sh "$OUT"
```

跑 driver 验证：

```bash
cd <MTAO>/tao_cpu_sim

PYTHON_BIN=/root/miniconda3/envs/yinhaolang/bin/python \
TAO_INFER_DEVICE=cuda \
RAW_ROOT=MTAO/datagen/tmp/<no-roi-run>/runs \
RUN_ROOT=MTAO/runs/w11_no_roi_driver_$(date +%Y%m%d_%H%M%S) \
bash scripts/07_validate_w11_w15_100k.sh \
  --workloads "W11_stream_mix" \
  --rows-per-core 100000 \
  --quantum-cycles 4096 \
  --k-max 128 \
  --batch 512
```

## 5. 阶段 3c · Quantum Δt 误差扫描

```bash
bash scripts/05_quantum_sweep.sh \
     --trace-dir <gem5_records_micro_dir> \
     --ckpt $TAO_CKPT_ROOT/0602.pt \
     --deltas 1,128,256,512,1024
```

输出：

```
runs/quantum_sweep_<timestamp>/
├── delta_1/      # 严格全序 baseline
├── delta_128/
├── delta_256/
├── delta_512/
├── delta_1024/
├── compare.json  # 各 Δt 与 baseline 的 CPI / fl / el / mispred 偏差
└── ips.json      # 各 Δt 单卡吞吐
```

## 6. 一键自检（用仓库自带样本）

```bash
ls $TAO_ROOT/infer/data/W11_stream_mix/
#   functional_parquet/manifest.json
#   labels_parquet/labels.core{0..3}.parquet
#   cut_baseline.json

# label-driven 模式（不需要 ckpt）
make smoke
# 等价于：
# python -m driver.inference_driver \
#     --functional-dir infer/data/W11_stream_mix/functional_parquet \
#     --labels-dir    infer/data/W11_stream_mix/labels_parquet \
#     --label-driven  --out runs/smoke_out
```

## 7. 移植到新机器

```bash
# 1) 拷贝整个 MTAO 目录（已自包含 datagen / train / infer / scripts / docs / ckpt）
rsync -a MTAO/ <dst>:/path/to/MTAO/

# 2) 在新机器创建 conda 环境并装依赖
conda create -n yinhaolang python=3.11
conda activate yinhaolang
pip install -r tao_cpu_sim/requirements.txt

# 3) 重新编译 native 部分
cd tao_cpu_sim
make build

# 4) 烟囱测试
make smoke
```

## 8. 常见维护操作 cheat-sheet

| 想做的事 | 改哪里 |
|---|---|
| 增加新特征列 | `infer/ml/dataset.py FEATURE_COLS` + `_ISide.KEYS`/`_FXxx` 嵌入族 + 同步 `tao_train/ml` |
| 改时钟语义（fl/el 关系） | [infer/driver/reference_clock.py](../infer/driver/reference_clock.py) 单点修改 |
| 切换 ref_sim 后端 | [infer/driver/ref_sim_client.py](../infer/driver/ref_sim_client.py) `PybindBackend` |
| 调整 W64/W256/W1024 派生 | [infer/driver/windowed_features.py](../infer/driver/windowed_features.py) |
| 新增 workload | [datagen/scripts/](../datagen/scripts) 仿照 `run_w11_w15_*` |
| 调试 ckpt 不兼容 | 先看 [infer/ml/infer.py](../infer/ml/infer.py) `STRICT_CKPT_CFG` 与 `_ckpt_compat` |
| 调 quantum Δt | `scripts/04_infer.sh --quantum-cycles <N>` 或 `scripts/05_quantum_sweep.sh` |
