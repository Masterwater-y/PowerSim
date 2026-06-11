# 系统架构

> 本文件给出 TAO 多核 CPU 仿真系统的整体架构、模块边界与数据流。
> 模块演进与详细 schema 见 [02-schema-contract.md](./02-schema-contract.md)。

---

## 1. 三阶段总图

```
┌──────────────────────────────────────────────────────────────────────────────┐
│   阶段 1 · taogen          阶段 2 · tao_train         阶段 3 · infer          │
│  ───────────────────      ────────────────────       ───────────────────     │
│  gem5(O3 detailed)         GPU 训练（A100/H 系列）    部署侧 / 验证侧推理     │
│  + AtomicSimpleCPU(ref)    Two-level Embedding +     ref_sim_py + driver     │
│  + MESI ref_sim 探针       Causal Self-Attn ×6L      + Reference Clock       │
│         │                          │                          │              │
│         ▼                          ▼                          ▼              │
│   records.micro/            ckpt (.pt)               预测 jsonl + report     │
│   parquet (50M 行)          训练日志                 端到端 CPI 估算         │
└──────────────────────────────────────────────────────────────────────────────┘
            ▲                          │
            │     ckpt 同时被 04_infer/ml/infer.py 与 driver 加载              │
            └──────────────────────────────────────────────────────────────────┘
```

## 2. 顶层目录与模块边界

```
tao_cpu_sim/
├── README.md                  # 入口（项目级总览 + 一键命令）
├── Makefile                   # build / test / smoke / clean
├── requirements.txt           # Python 依赖
├── docs/                      # 设计文档
│   ├── 01-architecture.md     # 本文件
│   ├── 02-schema-contract.md  # 字段契约与版本演进
│   ├── 03-workflow.md         # 端到端流程串接
│   ├── 04-quantum-parallel-coherence.md  # 性能核心方案
│   └── 05-perf-roadmap.md     # 性能优化路线
├── src/                       # 真实代码（symlink 到外层组件）
│   ├── 01_gem5/               # -> ../../gem5
│   ├── 02_taogen/             # -> ../../taogen
│   ├── 03_tao_train/          # -> ../../tao_train
│   └── 04_infer/              # -> ../../infer
├── scripts/                   # 端到端入口
│   ├── env.sh                 # 一键导出环境变量（PYTHONPATH / CUDA / TAO_*）
│   ├── 01_taogen_collect.sh   # 阶段 1
│   ├── 02_train.sh            # 阶段 2
│   ├── 03_validate.sh         # 阶段 3a 验证侧
│   ├── 04_infer.sh            # 阶段 3b 部署侧
│   └── 05_quantum_sweep.sh    # quantum Δt 误差扫描
├── configs/                   # 实验配置（quantum / model / dataset）
└── tests/                     # 集成测试 / smoke
```

**模块边界**：

- 阶段间通过 **parquet / pt** 文件契约通信，**不要互相 import**
- 阶段 1 → 阶段 2：`*.parquet`（hive 分区，按 workload）
- 阶段 2 → 阶段 3：`ckpt/*.pt`（含 `meta`+`config`）
- 阶段 3 内部：`functional.core<N>.parquet` + `labels.core<N>.parquet`

## 3. 关键数据流

```
┌────────────┐    records.micro     ┌─────────────┐
│  gem5 O3   │ ───────────────────► │  pack to    │
│  detailed  │ ─┐                   │  parquet    │
└────────────┘  │                   └──────┬──────┘
                │                          ▼
                │                   ┌─────────────┐
                │                   │ tao_train   │
                │                   │ Transformer │
                │                   └──────┬──────┘
                │                          │ ckpt.pt
                │                          ▼
   functional   │                   ┌─────────────────────────┐
   trace        │                   │  infer/driver           │
   (A 子集) ────┘ ────────────────► │  + LocalRefSim×N        │
                                    │  + CoherenceCoordinator │
                                    │  + Quantum scheduler    │
                                    └──────┬──────────────────┘
                                           ▼
                                    predicted.jsonl + report.json
```

## 4. 推理 driver 架构（重点）

旧架构（严格全序，单 ref_sim，B=1 forward）：

```
heapq[fetch_clock] → ref_sim → model.forward(B=1) → reference_clock → heap re-push
```

新架构（quantum-based parallel coherence，详见 [04-quantum-parallel-coherence.md](./04-quantum-parallel-coherence.md)）：

```
                    ┌──────────────── Phase 2: Reconcile ────────────────┐
                    │                                                     │
            ┌───────┴────────┐                                            │
Phase 1:    │ Coordinator    │  apply pending_events / snapshot           │
parallel    │ (LLC + dir)    │◄───────────────────────────────────────────┘
            └───────┬────────┘
                    │ snapshot broadcast
        ┌───────────┼───────────┐───────────┐
        ▼           ▼           ▼           ▼
   ┌────────┐ ┌────────┐  ┌────────┐  ┌────────┐
   │ Core0  │ │ Core1  │  │ Core2  │  │ Core3  │
   │ Local  │ │ Local  │  │ Local  │  │ Local  │
   │ refsim │ │ refsim │  │ refsim │  │ refsim │
   │ window │ │ window │  │ window │  │ window │
   │ model  │ │ model  │  │ model  │  │ model  │
   └────────┘ └────────┘  └────────┘  └────────┘
        │           │           │           │
        └─────┬─────┴─────┬─────┴─────┬─────┘
              ▼           ▼           ▼
                 model.predict_batch（多核 mini-batch 合并）
```

## 5. 部署 vs 验证

- **部署侧**（`scripts/04_infer.sh`）只读 `functional.core<N>.parquet`，
  不可见 `labels.core<N>.parquet`，反映真实生产环境
- **验证侧**（`scripts/03_validate.sh`）额外消费 labels，做：
  - ref_sim 17 字段 bit-exact 与 detailed records 对账
  - 端到端 CPI 与 oracle CPI 比较
  - PMU 报表

## 6. 移植性

- `src/` 全部用相对 symlink，移到新机器只需：
  1. `git clone` 或 `rsync` 全部源码
  2. `bash scripts/env.sh` 设环境变量
  3. `make build`（编译 gem5 + ref_sim_py.so）
  4. `make smoke`（5K rows 烟囱测试）
- 数据集（`*.parquet`）与 ckpt（`*.pt`）通过 [scripts/env.sh](../scripts/env.sh) 中的
  `TAO_DATA_ROOT` / `TAO_CKPT_ROOT` 解耦，不进 git
