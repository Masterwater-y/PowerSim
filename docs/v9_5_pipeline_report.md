# V9.5 多核 TAO 流水线方案报告（数据采集 → 数据集 → 模型）

> 基线：本仓库当前 main 分支源码（`taogen/`），与方案文档
> <https://bytedance.larkoffice.com/docx/PqRWdsGQpoBTsyxWhMocTw2Vn0f> §5 对齐。
> 所有事实仅引自仓库内代码与配置，未沿用任何已废弃的旧版描述。

---

## 0. 总览

V9.5 的流水线由三段强一致的环节构成：

```
┌────────────────────────────────────────────────────────────────┐
│ ① 数据采集  (gem5 + TaoTrace + ref_sim 校验)                    │
│    └─→ records.micro.jsonl / labels.micro.jsonl / mem_events    │
│                                                                  │
│ ② 数据集生产 (build_micro_dataset → balanced sample → parquet)  │
│    └─→ tmp/dataset_3m_pq/  →  tmp/dataset_144k_pq/              │
│                                                                  │
│ ③ 模型 + 训练 (TaoCoreTransformer + bf16 CPU autocast)          │
│    └─→ tmp/ckpt/tao_<ts>.{best,last,step*}.pt                   │
└────────────────────────────────────────────────────────────────┘
```

整套流水线坚持两个不可破坏的不变量：

1. **µop 粒度的样本编号** `(workload, core_id, thread_id, micro_seq, pos_in_thread)`
   是数据集主键，从 gem5 探针、parquet 行内排序到训练侧滑窗的锚点 `t`，
   全程同源。
2. **oracle 字段是 µop 输入属性，不是预测目标**。
   训练时它们由 gem5 探针给出；推理部署时由
   [mesi_ref_sim](file:///data00/yinhaolang/simulators/taogen/mesi_ref_sim) 现场重放产生。

---

## 1. 数据采集

### 1.1 探针：TaoTrace（gem5 patch）

实现位置：[gem5_patches/src/cpu/o3/probe](file:///data00/yinhaolang/simulators/taogen/gem5_patches/src/cpu/o3/probe)
([tao_trace.hh](file:///data00/yinhaolang/simulators/taogen/gem5_patches/src/cpu/o3/probe/tao_trace.hh) /
 [tao_trace.cc](file:///data00/yinhaolang/simulators/taogen/gem5_patches/src/cpu/o3/probe/tao_trace.cc) /
 [TaoTrace.py](file:///data00/yinhaolang/simulators/taogen/gem5_patches/src/cpu/o3/probe/TaoTrace.py))

每个核挂一个 `TaoTrace` 实例，输出 5 路 jsonl：

| 文件 | 内容 |
|---|---|
| `records.micro.jsonl`  | 每个 µop 的形态属性 + 数据/取指 oracle |
| `labels.micro.jsonl`   | 每个 µop 的 `fetch_tick / ready_tick / commit_tick / mispredicted` 等 |
| `mem_events.jsonl`     | 内存事件流（用于派生 oracle） |
| `sched.jsonl`          | 调度事件（用于稳态识别） |
| `diag.jsonl`           | 探针自检 |

回调钩子：`onCommit / onSquash / onExecute / onDataAccessComplete / onInstAccessComplete`，
真正写入 µop 训练样本的是 `emitMicroRecord(...)`。

#### 数据侧 oracle（`SharedAttr`，挂在内存型 µop 上）

`mesi_before, coh_oracle, sharer_bucket, owner_dist, dirty_owner,
path_class, inval_fanout, same_line_recent, oracle_source`

#### 取指侧 oracle（`InstSharedAttr`）

`i_mesi_before, i_coh_oracle, i_path_class, i_oracle_source`

#### `CoherenceAction` 8 类

`UNKNOWN(0) / L1_HIT(1) / REMOTE_HIT_CLEAN(2) / REMOTE_HIT_DIRTY(3) /
LLC_HIT(4) / DRAM(5) / WB_REQUIRED(6) / L2_HIT(7)`。

### 1.2 gem5 启动配置

[configs/run_mt_mvp.py](file:///data00/yinhaolang/simulators/taogen/configs/run_mt_mvp.py)：

- O3 CPU + Ruby `MESI_Three_Level`
- 每核挂一份 TaoTrace 探针；
- workload 用 `m5_work_begin / m5_work_end` 圈 ROI；
- 同步落一份 schema v2 的 `uarch_profile.json`，作为 oracle 计算与
  ref_sim 重放共用的"单一信源"。

[configs/single_core_arch_A.json](file:///data00/yinhaolang/simulators/taogen/configs/single_core_arch_A.json)：
8-wide O3、ROB=192，作为单核冒烟基准。

入口脚本：
- [scripts/run_experiment.sh](file:///data00/yinhaolang/simulators/taogen/scripts/run_experiment.sh) 一键 gem5 → ref_sim → 17/17 bit-exact + PMU 对比；
- [configs/step5_run_4workloads.sh](file:///data00/yinhaolang/simulators/taogen/configs/step5_run_4workloads.sh) / [step5_l3_4mib_smoke.sh](file:///data00/yinhaolang/simulators/taogen/configs/step5_l3_4mib_smoke.sh)。

### 1.3 4 个 workload（覆盖不同压力面）

| Workload | 压力面 | 源码 |
|---|---|---|
| W1 mt_compute_int | 纯 INT 算力 | [mt_compute_int.c](file:///data00/yinhaolang/simulators/taogen/workloads/mt_compute_int/mt_compute_int.c) |
| W2 mt_chase_dram  | 随机 pointer-chase，逼出 DRAM/LLC miss | [mt_chase_dram.c](file:///data00/yinhaolang/simulators/taogen/workloads/mt_chase_dram/mt_chase_dram.c) |
| W3 mt_micro_coh   | 轻量 coherence 触发 | [mt_micro_coh.c](file:///data00/yinhaolang/simulators/taogen/workloads/mt_micro_coh/mt_micro_coh.c) |
| W4 mt_coh_stress  | 重 coherence + 同 line 写穿 | [mt_coh_stress.c](file:///data00/yinhaolang/simulators/taogen/workloads/mt_coh_stress/mt_coh_stress.c) |

### 1.4 共享 µarch 模型 + ref_sim

为保证训练 oracle 与部署期 oracle 行为完全一致，两侧共享同一份配置和缓存模拟器：

- [shared/uarch_profile.hh](file:///data00/yinhaolang/simulators/taogen/shared/uarch_profile.hh)：手写 JSON 加载器（schema v2，`CacheCfg / TlbCfg / WalkerCfg / MshrCfg`）。
- [shared/lru_banked.hh](file:///data00/yinhaolang/simulators/taogen/shared/lru_banked.hh)：`BankedSetAssocLRU / TlbSim / PageWalkSim / MshrTracker`。
- [mesi_ref_sim](file:///data00/yinhaolang/simulators/taogen/mesi_ref_sim)：参考 MESI 状态机重放器，做 17/17 bit-exact 校验。

---

## 2. 数据集生产

### 2.1 流水线顺序（必须按此顺序）

```
gem5 + TaoTrace
   │  records.micro.jsonl / labels.micro.jsonl / mem_events.jsonl / uarch_profile.json
   ▼
tools/derive_mem_events.py            ── 合并 mem_events.merged.jsonl
   ▼
tools/extract_uarch_profile.py        ── 从 m5out/config.ini 反推 schema v2 uarch_profile.json
   ▼
tools/build_micro_dataset.py          ── V9.5 单源 detailed 投影
   │  每条样本 = { meta, input, uarch_context, labels }
   ▼
tools/check_micro_alignment.py        ── atomic_func vs detailed 1:1 对齐自检
   ▼
tools/sample_steady_balanced.py       ── 跨 workload 稳态窗均衡采样
   │  跳 core0、5%/5% head/tail skip、按行容量比例分配配额、stride 等距
   │  → samples_3m.jsonl
   ▼
scripts/pack_3m.sh  →  tools/pack_to_parquet.py
   │  hive 分区、zstd-3、dict、row_group=65536、按 (core,thread,seq) 重排
   │  → tmp/dataset_3m_pq/
   ▼
tools/subsample_dataset.py
   │  按 workload 等比缩放 + 组内 stride 采样，保留时序窗口
   │  → tmp/dataset_144k_pq/
```

入口脚本：
- [scripts/build_3m_dataset.sh](file:///data00/yinhaolang/simulators/taogen/scripts/build_3m_dataset.sh)
- [scripts/pack_3m.sh](file:///data00/yinhaolang/simulators/taogen/scripts/pack_3m.sh)

### 2.2 Parquet schema（与训练侧严格绑定）

实现见 [tools/pack_to_parquet.py](file:///data00/yinhaolang/simulators/taogen/tools/pack_to_parquet.py) 与
[ml/dataset.py](file:///data00/yinhaolang/simulators/taogen/ml/dataset.py)。

| 列分组 | 列 | 类型 | 备注 |
|---|---|---|---|
| 标识 | `core_id` | i8 | |
|       | `thread_id` | i16 | |
|       | `micro_seq` | i64 | 全核唯一 |
|       | `pos_in_thread` | i32 | thread 内 0-based 位置 |
| 14 BOOL | `is_load, is_store, is_atomic, is_branch, is_branch_cond, is_branch_indirect, is_call, is_return, is_int, is_fp, is_simd, is_serialize, is_microop, is_last_microop` | i8 | |
| 形态 SMALL_INT | `n_src, n_dst, size` | i16 | 训练侧 clamp 到 0..15 |
| 数据侧 oracle | `mesi_before, coh_oracle, sharer_bucket, owner_dist, dirty_owner, path_class, inval_fanout, same_line_recent, oracle_source` | i16 | **是输入，不是标签** |
| 取指侧 oracle | `i_path_class, i_coh_oracle, i_mesi_before, i_oracle_source` | i16 | **是输入，不是标签** |
| 地址 U64 | `macro_pc, micro_pc, vaddr, paddr, cacheline_addr` | u64 | |
| 寄存器依赖 | `d0..d3` | i32 | 距上一个生产者的 µop 距离 |
|             | `pc0..pc3` | i16 | 生产者类，0..6 合法，255=sentinel |
| Macro PC token | `macro_pc_id` | i32 | 动态小词表 < 2²⁰ |
| **Labels（仅 6 列）** | `fetch_tick / ready_tick / commit_tick / mispredicted / fetch_latency / execution_latency` | i64/i8 | tick 列**仅用于派生 latency**，不进训练 |

存储布局：`workload=<NAME>/part-000.parquet`，每 workload 一文件，
行内严格按 `(core_id, thread_id, micro_seq)` 升序，并重新填充 `pos_in_thread`。
压缩 `zstd-3`，开启 dict encoding，row_group_size=65536。
同时落 `vocab.json`（`macro_pc → id`）与 `meta.json`
（`schema_version=v9_5_pq_v1, n_total, workloads, workload_rows,
workload_threads, context_len_recommended=128, producer_arity=4`）。

### 2.3 子采样（3M → 144K，约 1h/epoch）

[tools/subsample_dataset.py](file:///data00/yinhaolang/simulators/taogen/tools/subsample_dataset.py)：

- 按 workload 等比缩放（保持 W1/W2/W3/W4 行数比例 → mispred pos% 不变）；
- partition 内按 `(core_id, thread_id)` 分组，组内按 `pos_in_thread` 排序后
  **stride 等距采样**，**不破坏时序窗口连续性**（这是允许 ParquetWindowDataset
  仍能切出有效上下文的前提，因此**禁止使用 WeightedRandomSampler / 上采样**）。

实测：3M 数据集采到 144,000 行，mispred 正样本占比 0.356%（vs 原集 0.348%，几乎无偏）。

---

## 3. 训练侧数据集（窗口构造）

实现：[ml/dataset.py](file:///data00/yinhaolang/simulators/taogen/ml/dataset.py)

### 3.1 滑窗与对齐

- `DatasetSpec.context_len = 128`，**左 pad**（`attn_mask=0` 表示 pad 位）；
- 每个 `(workload, core_id, thread_id)` 段是最小连续单元；
- 锚点 `t` **不允许跨 thread** 取上下文。

### 3.2 桶化辅助

| 函数 | 输入 | 输出 |
|---|---|---|
| `hash_addr_bucket(arr, n_bucket=16)` | `vaddr / paddr / cacheline_addr` | 64-bit 地址 `>> 6`（cacheline 对齐）→ splitmix64 → mod 16 |
| `bucketize_dist(d)` | 寄存器距离 `d0..d3` | 9 bins：`[0,1,2,3,4-7,8-15,16-31,32-63,64+]` |
| 生产者类裁剪 | `pc0..pc3` | sentinel 255 → 7，clip 到 0..7（vocab 16 富余） |

### 3.3 输入特征 / 训练标签清单

```python
FEATURE_COLS = (
    SCALAR_BOOL                # 14 个 bool
  + SCALAR_SMALL_INT           # 形态 + 数据 oracle + 取指 oracle 的 16 个 i16
  + ['macro_pc_id']
  + ['d0','d1','d2','d3']      # 桶化后入 dist_emb (9)
  + ['pc0','pc1','pc2','pc3']  # 入 pc_emb (16)
  + ['vaddr','paddr','cacheline_addr']  # 入 addr_emb (16)
)

LABEL_COLS = ('fetch_latency', 'execution_latency', 'mispredicted')   # 仅此 3 项
```

`latency` 默认做 `log1p` 变换（`label_log1p=True`）。

### 3.4 关键工程优化

- `__init__` 期间通过 `pq.read_table(memory_map=True)` 一次性把所有列预读为
  numpy（3M × 50 列 ≈ 600 MB 常驻），DataLoader worker 通过 fork 共享只读页 →
  零拷贝、低 CPU；
- `collate(...)` 把所有 feature 列 stack 为 long tensor，`attn_mask` 转 bool。

---

## 4. 模型构建（TaoCoreTransformer）

实现：[ml/model.py](file:///data00/yinhaolang/simulators/taogen/ml/model.py)

### 4.1 `TaoConfig` 默认值

| 维度 | 值 |
|---|---|
| `d_model` | 256 |
| `d_feat` | 64 |
| `n_layer` | 6 |
| `n_head` | 8 |
| `d_ff` | 1024 |
| `dropout` | 0.1 |
| `context_len` | 128 |
| `macro_pc_vocab` | 数据集动态注入：`max(vocab+16, 512)` |
| `addr_bucket / dist_bucket / pc_vocab / mesi_vocab / coh_vocab / path_vocab` | 16 / 9 / 16 / 8 / 8 / 8 |
| `w_fetch / w_exec / w_mispred` | 1.0 / 1.0 / 0.5 |
| `mispred_pos_weight` | 训练器自动估算 |
| `mispred_focal_gamma` | 0.0（默认关 focal） |

### 4.2 双层 Embedding（按特征族分组 → 线性合并）

四族每族产出 `d_feat=64`：

| 子模块 | 输入字段 | 输出 |
|---|---|---|
| `_OpcodeLike` | 14 个 bool（每位独立 2 行 emb） + `n_src,n_dst,size`（各 16 行 emb，clamp 0..15） + `macro_pc_id`（独立 emb） | concat → Linear → d_feat=64，过 LN |
| `_RegisterDep` | 4 路 `(d_i, pc_i)`：`dist_emb(9)` ⊕ `pc_emb(16)` | concat 4 路 → Linear → d_feat=64 |
| `_MemCoh` | 9 个数据侧 oracle（统一 16 行 emb） + 3 个地址桶（vaddr/paddr/cline，各 16 行 emb） | concat → Linear → d_feat=64 |
| `_ISide` | 4 个取指侧 oracle（16 行 emb） | concat → Linear → d_feat=64 |

`TwoLevelEmbedding`：`concat(f1,f2,f3,f4)` → `Linear(4·d_feat → d_model=256)` → LayerNorm。

### 4.3 Transformer Encoder（Pre-LN + causal + padding）

- 可学习位置编码 `_PosEmb = nn.Embedding(context_len, d_model)`；
- `_MHA`：8 头，使用 `F.scaled_dot_product_attention`，自己组装
  `attn_mask = padding_mask | causal_mask`（注意 SDPA 约定 True=保留，
  实现里最终传入 `~attn_mask`）；
- `_Block`：Pre-LN，残差结构 `x = x + drop(attn(ln1(x))); x = x + drop(ff(ln2(x)))`；
- 共 6 层 block，最后 `ln_f`。

### 4.4 多任务输出头（仅 3 个，对齐方案 §5.2）

取最后一个 token（锚点 `t`）的 hidden 出 3 个头：

```python
fetch_lat     = ReLU(Linear(d_model, 1))
exec_lat      = ReLU(Linear(d_model, 1))
mispred_logit =      Linear(d_model, 1)   # sigmoid 在 BCE-with-logits 内部完成
```

> **没有 `path_class / coh_oracle / i_path_class` 等头**——这些都是输入特征，
> 在训练期由探针给出，在部署期由 ref_sim 现场重放给出。

### 4.5 多任务损失

```
loss = w_fetch  * MSE(fetch_lat,     fetch_lat_t)
     + w_exec   * MSE(exec_lat,      exec_lat_t)
     + w_mispred* BCEWithLogits(mispred_logit, mispred,
                                pos_weight=mispred_pos_weight)
```

- `pos_weight` 由 `estimate_mispred_pos_weight(...)` 自动扫描 parquet 估算（≈ 287 on 3M 集）；
- `mispred_focal_gamma > 0` 时启用 focal-BCE：`(1-p_t)^gamma * BCE`，pos 仍乘 `pos_weight`；
- 标签端 `fetch/exec_latency_t = log1p(latency)`；`mispred ∈ {0,1}`。

> **正负样本失衡只在 loss 层面解决**（`pos_weight` ± focal）。
> 不允许使用 WeightedRandomSampler 或上采样——它们会破坏 µop 时序窗口连续性。

---

## 5. 训练机制

实现：[ml/train.py](file:///data00/yinhaolang/simulators/taogen/ml/train.py) +
[scripts/train_run.sh](file:///data00/yinhaolang/simulators/taogen/scripts/train_run.sh) /
[scripts/train_smoke.sh](file:///data00/yinhaolang/simulators/taogen/scripts/train_smoke.sh)。

### 5.1 默认超参

| 超参 | 默认 |
|---|---|
| 数据 | `tmp/dataset_144k_pq` |
| `bs / ctx` | 128 / 128 |
| `steps` | 200（smoke） |
| `lr / wd` | 3e-4 / 0.01 |
| `warmup` | `0.03 × steps`（训练脚本里折算） |
| 优化器 | `AdamW(betas=(0.9, 0.95))` |
| LR schedule | `LambdaLR cosine warmup`（warmup 线性升 → 余弦衰减到 10%） |

### 5.2 Sapphire Rapids CPU 加速

- 在 **import torch 之前** 设：
  `OMP_NUM_THREADS=32 / MKL_NUM_THREADS=32 / KMP_AFFINITY=granularity=fine,compact,1,0 / PYTHONUNBUFFERED=1`；
- BF16：`torch.amp.autocast(device_type='cpu', dtype=torch.bfloat16)`，
  实测 `bs=64` 下 47.7 vs 39.4 samples/s（**1.21× 加速**）；
- 启动脚本前缀：`numactl --cpunodebind=0 --membind=0`，绑定单 NUMA；
- 可选 `--compile` 走 `torch.compile`（默认关）。

### 5.3 实时日志

- `logging` 模块 + 自定义 `_FlushFileHandler`：每条记录后 `flush()` + `os.fsync(fileno)`；
- 同时挂 `StreamHandler(sys.stderr)`（stderr 行缓冲 + train_run.sh 用 `python -u + stdbuf -oL -eL`）；
- 不再依赖 stdout 缓冲，**`tail -f` 立刻可见**。

### 5.4 Checkpoint：原子写 + 三层兜底

| 类型 | 触发 | 文件 |
|---|---|---|
| 周期 ckpt | `--save-every` 步（默认 = 1 epoch）+ `--keep-last K` 滚动 | `<base>.step<N>.pt` |
| 最佳 ckpt | `ema_loss < best_loss × 0.999` | `<base>.best.pt` |
| 最后一帧 | `try/finally` 保证 | `<base>.last.pt` |
| 应急快照 | `kill -USR1 <pid>` | `<base>.sigusr1.step<N>.pt` |
| 优雅退出 | `SIGTERM/SIGINT` → 写完 last.pt 再 exit | `<base>.last.pt` |

- 原子写：先写 `.tmp`，再 `os.replace(tmp, path)`；
- 状态完整性：每份 ckpt 都打包
  `{model, optim, sched, step, ema_loss, best_loss, cfg, args, rng}`，
  `rng` 含 `torch_cpu / numpy / python` 三套；
- `--resume PATH` 全量恢复，bit-exact 续训。

`scripts/train_run.sh` 启动时若指定 `RESUME=...`：

- 路径不存在 → exit 2；
- 取出 ckpt 中的 `step`，若 `STEPS ≤ ckpt.step` 直接拒绝运行，
  并打印一个可直接 copy 的 `EPOCHS=N` 推荐值（避免 train.py 立刻
  进入 finally 写 last.pt 后退出的"假续训"陷阱）。

### 5.5 状态外化（非侵入式监控）

每 `--log-every` 步原子写一份 `<base>.status.json`：

```json
{
  "pid": 12345,
  "step": 6000,
  "total_steps": 16000,
  "ema_loss": 2.34,
  "best_loss": 1.81,
  "lr": 2.24e-4,
  "elapsed_s": 4321.5,
  "samples_per_sec": 38.6,
  "updated_ts": 1716830000.0
}
```

外部观察只需 `cat status.json`，零侵入、零开销。

### 5.6 一键脚本入口

| 脚本 | 用途 | 时长 |
|---|---|---|
| [train_smoke.sh](file:///data00/yinhaolang/simulators/taogen/scripts/train_smoke.sh) | 30 步冒烟，验证 dataset/model/loss/ckpt 链路 | ≤ 60s |
| [train_run.sh](file:///data00/yinhaolang/simulators/taogen/scripts/train_run.sh)     | 1 epoch ≈ 1h 的常规训练 | 由 `EPOCHS` 控制 |

支持的环境变量：
`EPOCHS / BS / CTX / LR / WORKERS / NUM_THREADS / N_ROWS / WARMUP_FRAC /
SAVE_EVERY_EPOCH / SAVE_EVERY / KEEP_LAST / RESUME / DATA / PYBIN`。

---

## 6. 不变量与禁忌（Do / Don't）

### Do

- ✅ µop 主键 `(workload, core_id, thread_id, micro_seq, pos_in_thread)` 全程同源；
- ✅ oracle 字段在训练时由探针给出，在推理时由 [mesi_ref_sim](file:///data00/yinhaolang/simulators/taogen/mesi_ref_sim) 重放给出；
- ✅ 子采样必须保留 `(core_id, thread_id)` 内的 `pos_in_thread` 顺序；
- ✅ 类不平衡只用 `pos_weight` ± `focal_gamma` 解决；
- ✅ ckpt 全部走原子写 + RNG 完整状态。

### Don't

- ❌ 不要把 `path_class / coh_oracle / i_path_class` 当作输出头训练
  （它们是输入；这是 V9.5 与早期方案最关键的差异）；
- ❌ 不要在 mispred 维度上采样或 WeightedRandomSampler，会破坏窗口连续性；
- ❌ 不要在 `scripts/train_run.sh` 外层再用 `tee $LOG`，会和 train.py 内部的
  FileHandler 写同一个文件，导致**每行复制两份**；
- ❌ 不要在 import torch 之后改 `OMP_NUM_THREADS`，没用。

---

## 7. 关键文件清单（绝对路径）

数据采集：

- [tao_trace.hh](file:///data00/yinhaolang/simulators/taogen/gem5_patches/src/cpu/o3/probe/tao_trace.hh)
- [tao_trace.cc](file:///data00/yinhaolang/simulators/taogen/gem5_patches/src/cpu/o3/probe/tao_trace.cc)
- [TaoTrace.py](file:///data00/yinhaolang/simulators/taogen/gem5_patches/src/cpu/o3/probe/TaoTrace.py)
- [run_mt_mvp.py](file:///data00/yinhaolang/simulators/taogen/configs/run_mt_mvp.py)
- [single_core_arch_A.json](file:///data00/yinhaolang/simulators/taogen/configs/single_core_arch_A.json)
- [step5_run_4workloads.sh](file:///data00/yinhaolang/simulators/taogen/configs/step5_run_4workloads.sh)
- [step5_l3_4mib_smoke.sh](file:///data00/yinhaolang/simulators/taogen/configs/step5_l3_4mib_smoke.sh)
- [run_experiment.sh](file:///data00/yinhaolang/simulators/taogen/scripts/run_experiment.sh)
- 4 个 workloads/[mt_*.c](file:///data00/yinhaolang/simulators/taogen/workloads)
- [shared/uarch_profile.hh](file:///data00/yinhaolang/simulators/taogen/shared/uarch_profile.hh)
- [shared/lru_banked.hh](file:///data00/yinhaolang/simulators/taogen/shared/lru_banked.hh)
- [mesi_ref_sim/src/main.cc](file:///data00/yinhaolang/simulators/taogen/mesi_ref_sim/src/main.cc)
- [mesi_ref_sim/include/simulator.hpp](file:///data00/yinhaolang/simulators/taogen/mesi_ref_sim/include/simulator.hpp)

数据集生产：

- [tools/derive_mem_events.py](file:///data00/yinhaolang/simulators/taogen/tools/derive_mem_events.py)
- [tools/extract_uarch_profile.py](file:///data00/yinhaolang/simulators/taogen/tools/extract_uarch_profile.py)
- [tools/build_micro_dataset.py](file:///data00/yinhaolang/simulators/taogen/tools/build_micro_dataset.py)
- [tools/check_micro_alignment.py](file:///data00/yinhaolang/simulators/taogen/tools/check_micro_alignment.py)
- [tools/sample_steady_balanced.py](file:///data00/yinhaolang/simulators/taogen/tools/sample_steady_balanced.py)
- [tools/pack_to_parquet.py](file:///data00/yinhaolang/simulators/taogen/tools/pack_to_parquet.py)
- [tools/subsample_dataset.py](file:///data00/yinhaolang/simulators/taogen/tools/subsample_dataset.py)
- [scripts/build_3m_dataset.sh](file:///data00/yinhaolang/simulators/taogen/scripts/build_3m_dataset.sh)
- [scripts/pack_3m.sh](file:///data00/yinhaolang/simulators/taogen/scripts/pack_3m.sh)

模型 / 训练：

- [ml/dataset.py](file:///data00/yinhaolang/simulators/taogen/ml/dataset.py)
- [ml/model.py](file:///data00/yinhaolang/simulators/taogen/ml/model.py)
- [ml/train.py](file:///data00/yinhaolang/simulators/taogen/ml/train.py)
- [scripts/train_run.sh](file:///data00/yinhaolang/simulators/taogen/scripts/train_run.sh)
- [scripts/train_smoke.sh](file:///data00/yinhaolang/simulators/taogen/scripts/train_smoke.sh)
