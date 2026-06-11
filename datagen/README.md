# taogen — TAO multi-core training-data generator

> 本子模块涉及的跨阶段字段、模型输入输出与 ckpt 口径，统一以
> [global/SCHEMA.md](SCHEMA.md) 为准。
> 若本文档与 `global/SCHEMA.md`、实际代码实现冲突，以后者为准。

`taogen` 是一套**多核 CPU 微架构训练数据生成与一致性验证**的最小可复现实验工程，对应 V9.5 A2/A3 + L3-size 修复版。

它包含：

1. **gem5 detailed probe** — `TaoTrace` SimObject，钩 O3 CPU 的 `ppDataAccessComplete` / `ppInstAccessComplete`，按 micro-op 与 cache 事件双粒度落 schema v2 训练数据；
2. **shared µarch oracle** — `shared/{lru_banked.hh, uarch_profile.hh}`，多核 banked LRU + TLB + 4 级 page walker + MSHR 跟踪，由 `uarch_profile.json` 单源配置，禁止任何 hardcode；
3. **mesi_ref_sim** — C++17 cache/coherence 重放器，与 oracle 共用同一份 `uarch_profile.json`，跑出来的 `mem_events` 与 oracle **17/17 bit-exact**（13 旧字段 + 4 个 i-side 字段）；
4. **PMU 对账脚本** — 对照 ruby `L2Cache_Controller.NP.L1_GETS / ISS.Mem_Data` 真值，4 workload 平均 acc% **≈ 90.65%**；
5. **4 workload microbench** — `mt_compute_int / mt_chase_dram / mt_micro_coh / mt_coh_stress`。

---

## 1. 项目结构

```
taogen/
├── docs/                    # 设计文档、修复日志、实验报告
├── gem5_patches/            # 落到 gem5 v23.0 上的源码补丁（probe）
│   └── src/cpu/o3/probe/
├── shared/                  # oracle 与 ref_sim 共用的 C++ 头（schema v2）
├── mesi_ref_sim/            # C++17 重放器
├── configs/                 # gem5 配置 + 实验脚本
├── tools/                   # 数据集构建 / profile 提取
├── workloads/               # 4 个 microbench 源码
└── scripts/
    ├── install.sh           # 一键拉 gem5 + 打 patch + 编译
    └── run_experiment.sh    # 一键跑 4 workload 验证
```

---

## 2. 环境要求

- Linux x86_64
- GCC ≥ 11（gem5 v23.0 要求）
- Python ≥ 3.8
- CMake ≥ 3.13
- SCons ≥ 3.x
- 网络可达 GitHub（拉 gem5）

可选：96 核机器跑 `JOBS=96` 编译 gem5（约 3-5 min）。

---

## 3. 快速开始（complete reproduction）

```bash
# 1) clone
git clone <minesim repo> minesim && cd minesim
git checkout taogen          # 孤儿分支
cd taogen

# 2) 一键安装：拉 gem5、打 patch、编译 gem5/ref_sim/workloads
bash scripts/install.sh
# 默认拉到 ./gem5（被 .gitignore 忽略）；如需放别处：
#   bash scripts/install.sh /path/to/gem5

# 3) 一键跑 4 workload
bash scripts/run_experiment.sh
# 输出在 ./tmp/run_<ts>/{W1..W4}_*/，SUMMARY.txt 显示 17/17 bit-exact
```

预期 `SUMMARY.txt`：

```
[W1_compute_int] dside: matched 3011 | mismatched 0 ; iside: matched 28787   | mismatched 0
[W2_chase_dram]  dside: matched 235643 | mismatched 0 ; iside: matched 552766 | mismatched 0
[W3_micro_coh]   dside: matched 40689  | mismatched 0 ; iside: matched 95254  | mismatched 0
[W4_coh_stress]  dside: matched 77574  | mismatched 0 ; iside: matched 229471 | mismatched 0
```

每个 workload 目录里 `pmu.log` 给出 13 metric × oracle/ref_sim/ruby 三方对账：

```
cache.llc.load_misses             785      785   OK    873  89.92
cache.llc.store_misses            236      236   OK    223  94.17
uncore_cha:TOR_INSERTS.IA_MISS_DRD 785     785   OK    873  89.92
...
bit-exact metrics (oracle vs ref_sim): 13/13
```

---

## 4. 设计要点（详见 `docs/`）

### 4.1 数据样本粒度（V9.1+ 单源 detailed 投影）

- **macro 流（mem_events.jsonl）**：1 行 / cache 事件（demand load/store 完成，或 ifetch 完成），用于 ref_sim bit-exact 校验。
- **micro 流（records.micro.jsonl）**：1 行 / O3 µop retire，用于训练；每条 µop 旁挂载 4 个 d-side + 4 个 i-side 上下文字段。
- 两条流由同一个 `TaoTrace` probe 同时产出，共享同一个 oracle 状态机推进。

### 4.2 schema v2（17 字段）

| 类 | 字段 | 来源 |
|---|---|---|
| 旧 d-side (13) | path_class / coh_oracle / mesi_before / mesi_after / oracle_source / cache_level / writeback / shared_count / l2_hit / dirty / ... | onDataAccessComplete |
| 新 i-side (4)  | i_path_class / i_coh_oracle / i_mesi_before / i_oracle_source                                                            | onInstAccessComplete |

不支持的 µarch 配置 **fail-fast**（schema v2 + `uarch_profile.json` 强校验）。

### 4.3 µarch 参数化（A3）

`uarch_profile.json` 是 oracle 与 ref_sim 共用的**单一真值源**，例如：

```json
{
  "cache": {
    "l1d": {"size_b": 32768,  "assoc": 8,  "num_banks": 1},
    "l1i": {"size_b": 32768,  "assoc": 8,  "num_banks": 1},
    "l2":  {"size_b": 262144, "assoc": 8,  "num_banks": 1},
    "l3":  {"size_b": 8388608, "assoc": 16, "num_banks": 4}
  },
  "tlb":         {"dtlb": {...}, "itlb": {...}},
  "page_walker": {"page_size_bits": 12, "levels": 4},
  "mshr":        {"l1d": 16, "l2": 32, "l3": 64}
}
```

**重要语义**：`cache.l3.size_b` 是**总容量**（≠ gem5 stdlib `MESIThreeLevelCacheHierarchy.l3_size` 的 per-bank 语义；`configs/run_mt_mvp.py` 写入时已 ×`num_l3_banks` 折算）。

### 4.4 关键修复（V9.5 post A2/A3）

- **A2 — i-cache real probe**：`onInstAccessComplete` 对每条 i-line miss 推进 oracle L1I/L2/L3 LRU + iTLB + walker + i-MSHR；带出 4 个 i-side schema 字段。
- **A3 — 参数化 page walker**：`shared/lru_banked.hh::PageWalkSim` 4 级 x86 页表，所有参数由 profile 驱动。
- **L3 size 语义对齐**：W2_chase_dram LLC load_misses 从 27881 (`-2968%`) 修正到 788（**89.55%**）。
- **PMU 聚合口径**：ruby `NP.L1_GETS` 同时含 demand + ifetch，`pmu_report.py` 聚合时纳入 ifetch 行后口径对齐。

详细分析见：

- `docs/post_a2_a3_report.md` — 修复前/后对比
- `docs/baseline_pre_a2_a3.md` — A2/A3 之前的 baseline
- `docs/v2_to_v5_alignment_log.md` — V2→V5 对齐演进
- `docs/PROGRESS_SUMMARY.md` — V9.5 整体计划
- `docs/knowledge.md` — 参考材料笔记

---

## 5. 实验结果摘要（4 workload × 13 metric）

| Metric | W1 | W2 | W3 | W4 |
|---|---:|---:|---:|---:|
| oracle ↔ ref_sim bit-exact | 13/13 | 13/13 | 13/13 | 13/13 |
| i-side mem_events bit-exact | 28 787 | 552 766 | 95 254 | 229 471 |
| cache.llc.load_misses (vs ruby) | **89.92%** | **89.55%** | **92.13%** | **91.01%** |
| cache.llc.store_misses (vs ruby) | 94.17% | 99.98% | 90.62% | 98.16% |
| uncore_cha:TOR_INSERTS.IA_MISS_DRD | 89.92% | 89.55% | 92.13% | 91.01% |
| uncore_cha:DIR_LOOKUP.SNP（近似） | 75.51% | 80.00% | 94.72% | 65.05% |

修复前 cache.llc.load_misses 平均 acc% ≈ 41.55%，修复后 **≈ 90.65%**。

---

## 6. License

仅限内部研究使用。

---

## 7. 50M Dataset Workflow

针对 `W11..W15` 并行采集得到的 `final_balanced_50000000_pq`，可直接使用以下脚本：

```bash
# 1) 数据体检
python3 tools/check_dataset_health.py \
  --data MTAO/datagen/tmp/06031920/final_balanced_50000000_pq

# 2) 启动训练（可用 DATA=... 覆盖默认数据目录）
bash scripts/train_50m_dataset.sh
```

其中：

- `tools/check_dataset_health.py` 会检查关键 schema、`is_fetch_group_head` 占比、
  `fetch/execution_latency` 分布，以及“相同局部输入签名”下的标签波动。
- `scripts/train_50m_dataset.sh` 默认调用 `ml/train.py`，训练当前 head-gated
  fetch latency 方案，并使用 `dataset.py` 在线派生 `is_macro_head` /
  `uop_pos_in_macro`。
