# MTAO — TAO Multi-core CPU Simulator

> 一份用 Transformer-as-Oracle (TAO) 替代多核 CPU 详细计时仿真的端到端工作仓。
> 流水线：**datagen → train → infer**，统一在 `MTAO/` 一个根下管理。

完整规范请见 [SCHEMA.md](SCHEMA.md) 和 [docs/00-global-overview.md](docs/00-global-overview.md)。

---

## 目录布局

```
MTAO/
├── README.md              # 本文件
├── SCHEMA.md              # 全局 IO 契约
├── Makefile               # 顶层一键编排（make bootstrap / build / smoke / bench）
├── bootstrap.sh           # 冷目录一键重建环境
├── requirements.txt       # 顶层 python 依赖（聚合 train/infer 的关键包）
├── .gitignore
│
├── docs/                  # 项目级文档（00-overview / 01-arch / 02-schema / 03-workflow / ...）
├── scripts/               # 顶层 0X_*.sh 流水线入口与 env.sh
├── configs/               # 顶层流水线配置（quantum_default.yaml 等）
│
├── datagen/               # 阶段 1：用 gem5 跑 detailed trace + oracle parquet
│   ├── gem5_patches/      #   gem5 v25.1.0.1 patch 集（probe + ROI hook + build_opts）
│   ├── mesi_ref_sim/      #   bit-exact 重放器 (C++)
│   ├── workloads/         #   微基准 (mt_*, holdout_*)
│   ├── scripts/install.sh #   clone gem5 + apply patch + 构建 gem5/ref_sim/workloads
│   ├── tools/             #   pack_to_parquet / build_inference_input / sample_*
│   ├── ml/                #   仓库内基线训练（保留）
│   └── docs/, README.md
│
├── train/                 # 阶段 2：V10.3 训练交付
│   ├── ml/                #   train.py / model.py / dataset.py / eval.py
│   ├── run_train.sh, run_smoke.sh
│   └── requirements.txt
│
├── infer/                 # 阶段 3：端到端推理 + functional/label 边界 + ref_sim 客户端
│   ├── driver/            #   inference_driver / ref_sim_client / windowed_features
│   ├── functional_trace/  #   records.micro -> functional / labels parquet
│   ├── mesi_ref_sim/      #   pybind 后端 ref_sim_py.so
│   ├── ml/                #   V10.3 strict 推理脚本
│   ├── scripts/install.sh #   编译 ref_sim_py.so + pip install
│   └── README.md, requirements.txt
│
└── ckpt/                  # 关键 ckpt 落地处（*.pt 不入 git；见 ckpt/README.md）
```

---

## 冷目录一键重建

```bash
git clone <this-repo> MTAO
cd MTAO

# 全套（首次推荐，会 clone 上游 gem5 v25.1.0.1 并应用 patch + 编译全部）
PYTHON=/path/to/python3.11 bash bootstrap.sh

# 或者跳过 gem5（只构建 infer / 安装 train deps，常用于纯推理/训练场景）
SKIP_GEM5=1 bash bootstrap.sh
```

`bootstrap.sh` 会：

1. `git clone --branch v25.1.0.1 https://github.com/gem5/gem5.git MTAO/gem5`（已被 .gitignore 忽略）
2. 套用 `MTAO/datagen/gem5_patches/`（TaoTrace + BranchEvents + ROI hook + `build_opts/X86_MESI_Three_Level`）
3. 构建 `gem5.opt X86_MESI_Three_Level` + `mesi_ref_sim` + workloads ELF
4. 在 `MTAO/infer/mesi_ref_sim/build/` 下构建 `ref_sim_py*.so`
5. 安装 `train/requirements.txt` + `infer/requirements.txt`

---

## 主要环境变量

```bash
source MTAO/scripts/env.sh
# 自动导出：
#   TAO_ROOT         = MTAO 根
#   TAO_DATAGEN_ROOT = $TAO_ROOT/datagen
#   TAO_TRAIN_ROOT   = $TAO_ROOT/train
#   TAO_INFER_ROOT   = $TAO_ROOT/infer
#   TAO_GEM5_ROOT    = $TAO_ROOT/gem5
#   TAO_CKPT_ROOT    = $TAO_ROOT/ckpt
```

---

## 跑通验收 (3 阶段)

```bash
source scripts/env.sh

# 阶段 1：采集 detailed trace + oracle parquet
bash scripts/01_taogen_collect.sh           # 详细参数见 datagen/scripts/

# 阶段 2：训练（5 步烟囱可用 train/run_smoke.sh）
bash scripts/02_train.sh

# 阶段 3：推理 / 验证
bash scripts/04_infer.sh
```

或者用 Makefile 顶层快捷：

```bash
make bootstrap     # 冷目录一键
make build         # gem5/ 已 clone 后重新构建
make smoke         # label-driven 5K 烟囱
make bench         # ckpt-driven 5K benchmark (单 GPU)
```

---

## 关于 ckpt

`ckpt/` 目录被 `.gitignore` 排除（`*.pt`），仅保留目录占位与说明。
关键 ckpt（如 `tao_v10_3_ma16.best.pt`）请通过外部存储分发后放入 `ckpt/`，再

```bash
export TAO_CKPT_ROOT=$PWD/ckpt
```

详见 [ckpt/README.md](ckpt/README.md)。

---

## 详细文档入口

- 项目总览：[docs/00-global-overview.md](docs/00-global-overview.md)
- 架构：[docs/01-architecture.md](docs/01-architecture.md)
- 数据契约：[docs/02-schema-contract.md](docs/02-schema-contract.md)
- 端到端工作流：[docs/03-workflow.md](docs/03-workflow.md)
- Quantum / 多核：[docs/04-quantum-parallel-coherence.md](docs/04-quantum-parallel-coherence.md)
- 性能 roadmap：[docs/05-perf-roadmap.md](docs/05-perf-roadmap.md)
- timing-aware functional ref_sim：[docs/06-timing-aware-functional-refsim.md](docs/06-timing-aware-functional-refsim.md)
- best ckpt + infer/eval：[docs/07-best-ckpt-infer-eval.md](docs/07-best-ckpt-infer-eval.md)
