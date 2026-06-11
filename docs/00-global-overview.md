# TAO 多核 CPU 仿真系统 —— 项目总览

> 统一模型输入输出契约请先看 [`global/SCHEMA.md`](SCHEMA.md)。
> 若本文件与实现细节存在差异，以 `SCHEMA.md` 和对应代码实现为准。

本项目目标是构造一个基于多核 TAO（Transformer-as-Oracle）的 CPU 仿真系统，
完整流水线由 **数据生成 → 模型训练 → 推理 / 验证** 三阶段组成。

- 当前外部方案文档：https://bytedance.larkoffice.com/docx/GxFldVdN2o0pejxZjZOcPWLanLh
- TAO 论文：https://bytedance.larkoffice.com/docx/S6GldLqeOof38dxKsx8cWKULnR2

---

## 1. 仓库目录速览

```
MTAO/
├── docs/                  # 项目级文档（含本文件、SCHEMA.md 入口在根 SCHEMA.md）
├── scripts/               # 顶层 0X_*.sh 入口与 env.sh
├── configs/               # 顶层流水线配置
├── gem5_patches/          # gem5 v25.1.0.1 补丁（bootstrap.sh 会 clone 上游 gem5/ 后自动应用）
├── datagen/               # 阶段 1：用 gem5 跑 detailed trace + oracle 标签 → parquet 数据集
│   ├── ml/                #   仓库内部最早的 baseline 训练（含 macro_pc_id 兼容分支）
│   ├── scripts/           #   工作负载采集脚本（W11..W15、3M/10M/50M 档）
│   ├── tools/             #   pack_to_parquet.py / build_inference_input.py
│   └── docs/, README.md
├── train/                 # 阶段 2：V10.3 训练交付包（生产训练入口）
│   ├── ml/                #   train.py / dataset.py / model.py / infer.py / eval.py
│   ├── run_train.sh       #   全量训练入口
│   └── run_smoke.sh       #   5 步烟囱测试
├── infer/                 # 阶段 3：端到端推理 driver + functional/label 边界 + ref_sim 客户端
│   ├── functional_trace/  #   从 records.micro 投影出 functional / labels 两套 parquet
│   ├── mesi_ref_sim/      #   pybind 后端 ref_sim_py.so（C++ MESI 参考仿真器）
│   ├── driver/            #   inference_driver / ref_sim_client / windowed_features / reference_clock
│   ├── ml/                #   V10.3 strict 推理脚本（与 train/ml 同源，但更严）
│   ├── scripts/           #   infer_from_functional.sh / validate_from_trace.sh
│   └── data/              #   样本 parquet（W11_stream_mix 等；不入 git）
└── ckpt/                  # 关键 ckpt 落地处（*.pt 默认 .gitignore 忽略）
```

---

## 2. 三阶段总流程

```
┌──────────────────────────────────────────────────────────────────────────────┐
│   阶段 1 · taogen          阶段 2 · tao_train         阶段 3 · infer          │
│  ───────────────────      ────────────────────       ───────────────────     │
│  gem5(O3 detailed)         GPU 训练（A100/H800）      部署侧 / 验证侧推理     │
│  + AtomicSimpleCPU(ref)    Two-level Embedding +     ref_sim_py + driver     │
│  + MESI ref_sim 探针       Causal Self-Attn ×6L      + Reference Clock       │
│         │                          │                          │              │
│         ▼                          ▼                          ▼              │
│   records.micro/            ckpt (.pt)               预测 jsonl + report     │
│   parquet (50M 行)          训练日志                 端到端 CPI 估算         │
└──────────────────────────────────────────────────────────────────────────────┘
            ▲                          │
            │     ckpt 同时被 infer/ml/infer.py 与 driver 加载                  │
            └──────────────────────────────────────────────────────────────────┘
```

**核心契约**

| 边界 | 生产者 | 消费者 | 物理介质 | Schema 来源 |
|---|---|---|---|---|
| Detailed trace + oracle | `taogen` (gem5 O3 + ref_sim 探针) | `tao_train/ml/dataset.py` | `*.parquet`（hive 分区，按 workload） | [taogen/README.md](datagen/README.md) |
| Functional trace（A 子集） | `infer/functional_trace/extract_from_records.py` | `infer/driver/inference_driver.py` | `functional.core<N>.parquet` | [infer/functional_trace/schema.py](infer/functional_trace/schema.py) |
| Oracle labels（仅验证用） | 同上 | `infer/scripts/validate_from_trace.sh` 末段 compare_pred_vs_truth | `labels.core<N>.parquet` | [infer/functional_trace/schema.py](infer/functional_trace/schema.py) |
| 单步预测（fl/el/mispred/head） | `infer/ml/infer.py` 或 `driver/inference_driver.py` | `driver/reference_clock.py` 推进时钟 | jsonl 或 in-process | [infer/ml/model.py](infer/ml/model.py) `_TaoOutputs` |

---

## 3. 阶段 1 · taogen 数据生成

**职责**：用改造过的 gem5 跑 O3CPU(detailed) + AtomicSimpleCPU(参考)，
通过 MESI ref_sim 探针抓取每条已提交指令的 17 字段输入特征 + 3 个 oracle 标签。

**关键目录与脚本**
- 工作负载采集：[taogen/scripts/run_w11_w15_10m_experiment.sh](datagen/scripts/run_w11_w15_10m_experiment.sh)（50M 均衡数据集）
- 三档 profile：[taogen/scripts/run_w11_w15_train.sh](datagen/scripts/run_w11_w15_train.sh)（1m / 3m / 10m）
- 列式打包：[taogen/tools/pack_to_parquet.py](datagen/tools/pack_to_parquet.py)
- Hold-out 推理输入构造：[taogen/tools/build_inference_input.py](datagen/tools/build_inference_input.py)

**输出 Schema（parquet 列，权威定义见 [taogen/README.md](datagen/README.md)；与 [simulator.hpp](infer/mesi_ref_sim/include/simulator.hpp) 1:1 对齐）**

| 类别 | 字段 | 来源 | 说明 |
|---|---|---|---|
| 标识 | `core_id, thread_id, micro_seq, seq_num, workload, binary` | gem5 records.micro | 样本主键（训练时按 thread_id+pos_in_thread 切窗） |
| Functional A 子集 | `macro_pc, micro_pc, vaddr, paddr, cacheline_addr, cacheline_paddr, size`, `is_load/store/atomic/branch/branch_cond/branch_indirect/call/return/int/fp/simd/serialize/microop/last_microop`, `n_src, n_dst`, `producer_dists[4], producer_classes[4]` | functional trace（atomic/重放可恢复） | **部署侧能消费的全部输入** |
| Family-1 OPCODE_LIKE | 14 个 is_* + n_src/n_dst/size | functional | bool flags + 计数 |
| Family-2 REGISTER_DEP | `d0..d3`（producer_dists 桶化）, `pc0..pc3`（producer_classes） | functional | 4 路 RAW 依赖 |
| Family-3 **MEM_COH（D-side）** | `mesi_before, coh_oracle, sharer_bucket, owner_dist, dirty_owner, path_class, inval_fanout, same_line_recent, oracle_source`（9 个 d-oracle）+ `d_mshr_depth, dtlb_hit, d_walker_levels, d_walker_dram_misses, d_bank_id`（5 个 P0-A d）+ `d_llc_set_residency, d_llc_set_lru_pos`（2 个 V10.3 A d）+ 4 个地址桶 `vaddr_bucket / paddr_bucket / cline_bucket / cline_p_bucket` | **ref_sim d-side**（gem5 detailed bit-exact） | 数据 cache + DTLB + Walker + L3 set 的"输入态" |
| Family-4 **I_SIDE** | `i_path_class, i_coh_oracle, i_mesi_before`（3 个 i-oracle）+ `i_group_head, i_group_pos`（2 个在线派生 group 特征）+ `i_mshr_depth, itlb_hit, i_walker_levels, i_walker_dram_misses, i_bank_id`（5 个 P0-A i）+ `i_llc_set_residency, i_llc_set_lru_pos`（2 个 V10.3 A i） | **ref_sim i-side**（vaddr 域，与 d-side 严格隔离）+ dataloader 在线派生 group 特征 | 指令 cache + ITLB + i-walker + L3-i set "输入态" |
| Family-5 CtxWindow | `mem_density_W64, branch_density_W64, unique_cl_W64, pc_freq_W64, bank_conflict_W64, cl_reuse_dist_log, time_since_last_branch_log` | packer 离线（严格因果） | W=64 窗口 |
| Family-6 DramFeats | `unique_cl_W256, unique_cl_W1024, dram_bank_id, dram_bank_freq_W256, dram_row_freq_W256` | packer 离线 | W256/W1024 长窗口 |
| Labels（detailed-only） | `fetch_latency, execution_latency, mispredicted, is_fetch_group_head` | gem5 detailed O3 | 训练目标；推理时不可见 |

> **重要更正**：
> - 之前的"MemCoh = ref_sim 输出"描述不准确。
>   `_MemCoh` 实际是 **D-side 一致性 + d-side 微架构状态**（共 16 + 4 个 emb 表），
>   `_ISide` 才是 **I-side**。当前生产基线 ckpt 实测为 **12 个 emb 表**：
>   10 个 i-side 基础字段 + `i_group_head` / `i_group_pos`。
> - **D-side / I-side 字段是对称的**：每个 i-side 字段都有同名 d-side 对应字段（除 `i_oracle_source` 已剔除）。
> - ref_sim 通过 `step()` 返回 `DSideOracle`、`stepIFetch()` 返回 `IFetchResult`，两路独立维护 LRU 视图。

**版本演进**：v2 → V9.5（17 字段 d/i 一致）→ V10.1（i-side 改造）→ V10.3（strict，剔除 macro_pc_id）。
详细演进记录见 [taogen/README.md](datagen/README.md)。

---

## 4. 阶段 2 · tao_train 模型训练

**职责**：消费 taogen 输出的 parquet，训练 Two-level Embedding + Causal MHA Transformer。

**关键文件**
- 训练入口：[tao_train/run_train.sh](train/run_train.sh)
- 烟囱测试：[tao_train/run_smoke.sh](train/run_smoke.sh)
- 数据加载：[tao_train/ml/dataset.py](train/ml/dataset.py)
- 模型：[tao_train/ml/model.py](train/ml/model.py)
- 训练脚本：[tao_train/ml/train.py](train/ml/train.py)
- 评估：[tao_train/ml/eval.py](train/ml/eval.py)

**模型骨架**
- 6 个特征族（OPCODE_LIKE / REGISTER_DEP / MEM_COH / I_SIDE / CtxWindow / DramFeats）→ d_model=256
- Causal multi-head self-attention 6 layer × 8 head, d_ff=1024
- 多任务头：
  `is_fetch_group_head`（cls）+ `fetch_latency(head 条件回归)` +
  `execution_latency`（reg）+ `mispredicted`（cls）
- `fetch_latency` 采用 zero-inflated / hurdle 方案：
  训练时用 `head` 做正样本 masked regression，并加
  `sigmoid(head_logit) * fetch_lat_pos` 的一致性损失；
  推理时只有 `head_hard=1` 才输出非零 `fetch_lat`
- 训练特性：SIGUSR1/SIGTERM 优雅 ckpt、自动估算 `mispred_pos_weight`、resume

**输入**：`*.parquet`（taogen 产物，hive 分区，按 workload）
**输出**：`tao_train/ckpt/*.pt`（ckpt + meta + config）
**默认产物**：[tao_train/ckpt/0602.pt](train/ckpt/0602.pt)

详细 V10.3 训练交付说明见 [tao_train/README.md](train/README.md)。

---

## 5. 阶段 3 · infer 推理 / 验证

infer 包含两条独立流水线：**部署侧推理**（只用 functional）与 **验证侧 6 步流水线**（带 oracle bit-exact 校验）。

### 5.1 Functional / Label 边界（部署 vs 验证）

权威定义见 [infer/functional_trace/schema.py](infer/functional_trace/schema.py)：
- `FUNCTIONAL_TRACE_COLS`：部署侧唯一可见列（A 子集），包括 macro_pc/vaddr/paddr/is_*/producer_* 等
- `LABEL_COLS`：仅用于离线验证的 detailed-only 真值（fetch_tick/ready_tick/mispredicted）

[infer/functional_trace/extract_from_records.py](infer/functional_trace/extract_from_records.py) 把 records.micro 投影出
`functional.core<N>.parquet` 与 `labels.core<N>.parquet` 两份独立文件，确保部署侧绝不泄漏标签。

### 5.2 Driver 子组件

| 模块 | 职责 |
|---|---|
| [ref_sim_client.py](infer/driver/ref_sim_client.py) | 通过 importlib 加载 `ref_sim_py.so`，提供 `on_ifetch / on_mem_access / on_commit` |
| [windowed_features.py](infer/driver/windowed_features.py) | OnlineWindowFeatures：W64/W256/W1024 严格因果派生；`derive_before_update` 读，`update` 写 |
| [reference_clock.py](infer/driver/reference_clock.py) | scheme 3：`fetch_clock += fl; ready_clock = max(rc, fc + el)` |
| [inference_driver.py](infer/driver/inference_driver.py) | 多核全局调度（heapq by fetch_clock）、串接 ref_sim + window + ModelPredictor |

`inference_driver.py` 支持三种 ModelPredictor 模式（仅 driver 调度方式不同，时钟逻辑一致）：
- `--label-driven`：用 labels.parquet 真值代替模型，验证 driver 自身正确性
- `--mock-model`：固定/随机预测，做基础联调
- `--ckpt`：加载 V10.3 ckpt，真实模型推理

### 5.3 部署侧入口

[infer/scripts/infer_from_functional.sh](infer/scripts/infer_from_functional.sh)
```
trace_dir/records.micro
    │ extract_from_records.py
    ▼
functional.core<N>.parquet  (+ labels.core<N>.parquet 如带验证)
    │ inference_driver.py --ckpt <pt>
    ▼
predicted.jsonl + report.json
```

### 5.4 验证侧 6 步流水线

[infer/scripts/validate_from_trace.sh](infer/scripts/validate_from_trace.sh)
```
1. derive_mem_events           从 records.micro 派生 mem_events
2. ref_sim                     用 mesi_ref_sim 跑 oracle 17 字段
3. bit-exact check             ref_sim 输出 vs detailed records 17 字段必须完全一致
4. build_inference_input       拼接 functional + ref_sim 输出为模型输入
5. ml/infer.py                 单步预测 fetch_latency/execution_latency/mispredicted/head
6. compare_pred_vs_truth + synthesize_cpi + pmu_report
```

---

## 6. Schema 统一说明【重要】

详细字段口径以 [SCHEMA.md](SCHEMA.md) 为准；本节只保留项目总览层面的摘要，避免与实现细节再度漂移。

### 6.1 两层 schema

当前项目必须区分两套 schema：

- **生产基线 schema**
  - 锚定 ckpt：[tao_v10_3_ma16.best.pt](train/ckpt/tao_v10_3_ma16.best.pt)
  - 该 ckpt 对应完整训练结束状态：[tao_v10_3_ma16.status.json](train/ckpt/tao_v10_3_ma16.status.json)
  - 实测 `I_SIDE` embedding 包含 12 个键：
    - 10 个 i-side 基础字段
    - `i_group_head`
    - `i_group_pos`
  - 不含：
    - `i_group_bkt`
    - `i_oracle_source`

- **vNext schema**
  - 锚定当前工作区 `tao_train/ml` 代码
  - 已加入 branch-mispred 设计：
    - `mispred_mask`
    - branch-control 有效位
    - branch-only 统计与 count error 输出
  - 但尚未形成新的“完整训练完成 + 正式验收”的生产基线 ckpt

### 6.2 I-side 当前现实

当前真实可运行口径不是“去掉全部 `i_group_*`”，而是：

- 保留 `i_group_head`
- 保留 `i_group_pos`
- 去掉 `i_group_bkt`
- 去掉 `i_oracle_source`

因此当前生产基线的 `Family-4 I_SIDE` 实际是 **12 个 emb 表**，不是旧文档里的 10 个。

### 6.3 未来目标

如果未来要彻底去掉 `i_group_head / i_group_pos`，那应被视为一次新的 schema 升级：

- 需要同步修改训练代码
- 需要同步修改推理代码与 driver
- 需要重新训练并产出新的正式 ckpt

在新的正式 ckpt 产出前，不应把这个目标口径误写成“当前现实”。

---

## 7. 端到端流程串接（一条命令链）

下面给出从空集群到产出端到端预测报告的完整命令序列。
（假设 gem5 已 build，ref_sim_py.so 已编译到 [infer/mesi_ref_sim/build/](infer/mesi_ref_sim/build)。）

### 7.1 阶段 1：生成 50M 训练集（taogen）

```bash
cd <MTAO>/taogen
# 一次性采集 W11..W15 五个 workload，每个 10M，共 50M 行
bash scripts/run_w11_w15_10m_experiment.sh
# 产物：taogen/data/{W11..W15}_*/parquet/*.parquet
```

### 7.2 阶段 2：训练 V10.3 ckpt（tao_train）

```bash
cd <MTAO>/tao_train
# 默认 100K 步，自动 mispred_pos_weight 估算，SIGUSR1 优雅 ckpt
bash run_train.sh
# 产物：tao_train/ckpt/<run_name>.pt + .log
# 烟囱测试（5 步）：
bash run_smoke.sh
```

### 7.3 阶段 3a：验证侧（带 oracle bit-exact 校验）

```bash
cd <MTAO>/infer
bash scripts/validate_from_trace.sh \
     --trace-dir <gem5_records_micro_dir> \
     --ckpt MTAO/ckpt/<your>.pt
# 产物：predicted.jsonl + cpi_compare.json + pmu_report.json
# 6 步流水线见第 5.4 节
```

### 7.4 阶段 3b：部署侧（仅 functional，无标签）

```bash
cd <MTAO>/infer
# 模式 A：真实 ckpt 推理
bash scripts/infer_from_functional.sh \
     --trace-dir <gem5_records_micro_dir> \
     --ckpt MTAO/ckpt/<your>.pt \
     --mode ckpt

# 模式 B：label-driven（用 labels 真值替代模型，校验 driver 自身）
bash scripts/infer_from_functional.sh --trace-dir <...> --mode label

# 模式 C：mock-model（固定预测，最小联调）
bash scripts/infer_from_functional.sh --trace-dir <...> --mode mock
```

### 7.5 一键自检：用现成样本数据快速跑通

```bash
# 仓库自带一个已抽取好的 functional/labels parquet
ls MTAO/infer/data/W11_stream_mix/
#   functional_parquet/manifest.json
#   labels_parquet/labels.core{0..3}.parquet

# 直接用 label-driven 模式，无需 ckpt：
cd <MTAO>/infer
python -m driver.inference_driver \
    --functional-dir data/W11_stream_mix/functional_parquet \
    --labels-dir    data/W11_stream_mix/labels_parquet \
    --label-driven \
    --out /tmp/tao_demo_out
```

---

## 8. 常见维护操作 cheat-sheet

| 想做的事 | 改哪里 |
|---|---|
| 增加新特征列 | `infer/ml/dataset.py FEATURE_COLS` + `_ISide.KEYS`/对应 `_FXxx` 嵌入族 + 同步 `tao_train/ml`（基线对齐） |
| 改时钟语义（fl/el 关系） | [reference_clock.py](infer/driver/reference_clock.py) 单点修改 |
| 切换 ref_sim 后端 | [ref_sim_client.py](infer/driver/ref_sim_client.py) `PybindBackend` |
| 调整 W64/W256/W1024 派生 | [windowed_features.py](infer/driver/windowed_features.py) |
| 新增 workload | [taogen/scripts/](datagen/scripts) 仿照 `run_w11_w15_*` |
| 调试 ckpt 不兼容 | 先看 [infer/ml/infer.py](infer/ml/infer.py) `STRICT_CKPT_CFG` 与 `_ckpt_compat`；再检 6.1 表格 |
