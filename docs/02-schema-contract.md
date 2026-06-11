# Schema 契约与版本演进

> 本文档是项目文档树中的 schema 镜像页。
> 细节级 source of truth 以 [`global/SCHEMA.md`](SCHEMA.md) 为准。
> 若本文件与 `global/SCHEMA.md`、真实 ckpt、真实代码实现冲突，以后者为准。

---

## 1. 当前决策

当前项目必须明确区分两套 schema：

- **生产基线 schema**
  - 锚定 ckpt：[tao_v10_3_ma16.best.pt](train/ckpt/tao_v10_3_ma16.best.pt)
  - 这是当前最新完整训练并已做正式评估的主基线
- **vNext schema**
  - 锚定当前工作区 `tao_train/ml` 代码
  - 包含新的 branch-mispred 设计，但尚未产出新的完整生产 ckpt

## 2. 共同边界

### 2.1 部署边界（A 子集）

| 类别 | 字段 | 是否部署侧可见 |
|---|---|---|
| 标识 | `core_id, thread_id, micro_seq, seq_num, workload, binary` | 是 |
| Functional A 子集 | `macro_pc, micro_pc, vaddr, paddr, cacheline_addr, cacheline_paddr, size`, `is_load/store/atomic/branch/branch_cond/branch_indirect/call/return/int/fp/simd/serialize/microop/last_microop`, `n_src, n_dst`, `producer_dists[4], producer_classes[4]` | **是** |
| ref_sim 输出 d-side / i-side | 见 §2.2 | 推理时由 ref_sim 实时生成（部署侧可见） |
| Labels（detailed-only） | `fetch_tick, ready_tick, mispredicted, fetch_latency, execution_latency, is_fetch_group_head` | **否**（仅验证用） |

补充语义：
- `is_fetch_group_head` 是 fetch latency 的显式门控标签。
- `fetch_latency` 是 zero-inflated 目标：仅当 `is_fetch_group_head=1` 时才有物理意义；
  推理侧若 `head_hard=0`，最终输出 `fetch_latency=0`。

### 2.2 ref_sim 输出 D/I 对称表

每条已 commit 的 d-访问 / i-fetch 上分别返回一组结构体，字段语义严格对称：

| 维度 | D-side（Family-3 MEM_COH） | I-side（Family-4 I_SIDE） | 是否保留 |
|---|---|---|---|
| 一致性视图 MESI | `mesi_before` | `i_mesi_before` | ✅ 两侧 |
| 命中层级 | `coh_oracle, path_class` | `i_coh_oracle, i_path_class` | ✅ 两侧 |
| 跨核共享态 | `sharer_bucket, owner_dist, dirty_owner, inval_fanout, same_line_recent` | (i-side 无：fetch 只读) | ✅ d；i 天然没有 |
| MSHR 队列深度 | `d_mshr_depth` | `i_mshr_depth` | ✅ 两侧 |
| TLB 命中 | `dtlb_hit` | `itlb_hit` | ✅ 两侧 |
| Walker level / DRAM miss | `d_walker_levels, d_walker_dram_misses` | `i_walker_levels, i_walker_dram_misses` | ✅ 两侧 |
| Cache bank | `d_bank_id` | `i_bank_id` | ✅ 两侧 |
| L3 set residency / LRU pos | `d_llc_set_residency, d_llc_set_lru_pos` | `i_llc_set_residency, i_llc_set_lru_pos` | ✅ 两侧 |
| 地址桶 | `vaddr_bucket, paddr_bucket, cline_bucket, cline_p_bucket` | (i-side 已隐含) | ✅ d 4 个；i 0 个 |
| oracle 来源指示 | `oracle_source` | `i_oracle_source` | d 保留；i 作为 dead feature 不进入当前送模口径 |
| fetch-group 派生 | — | `i_group_head, i_group_pos, i_group_bkt` | 只在 dataloader / 推理侧按具体版本使用 |

## 3. 生产基线 schema

### 3.1 生产基线 checkpoint

- [tao_v10_3_ma16.best.pt](train/ckpt/tao_v10_3_ma16.best.pt)
- 状态文件：[tao_v10_3_ma16.status.json](train/ckpt/tao_v10_3_ma16.status.json)
- 全量验证：[tao_v10_3_ma16.best.full_val.ddp.eval.json](train/ckpt/tao_v10_3_ma16.best.full_val.ddp.eval.json)

### 3.2 生产基线 I_SIDE

当前生产基线 ckpt 实测 `I_SIDE` embedding 包含 12 个键：

- `i_path_class`
- `i_coh_oracle`
- `i_mesi_before`
- `i_group_head`
- `i_group_pos`
- `i_mshr_depth`
- `itlb_hit`
- `i_walker_levels`
- `i_walker_dram_misses`
- `i_bank_id`
- `i_llc_set_residency`
- `i_llc_set_lru_pos`

并且：

- 不含 `i_group_bkt`
- 不含 `i_oracle_source`

### 3.3 生产基线解读

- `mispred_logit` 存在，但不应被误解释为“已按 branch-control 口径重训完成的头”
- 对现有生产 ckpt，应以：
  - ckpt 实测 embedding
  - 现有部署行为
  - 已完成训练与评估结果
  共同定义其生产语义

## 4. vNext schema

当前工作区 `tao_train/ml` 代码已经引入下一代 branch-mispred 设计：

- `mispred_mask`
- `mispred_valid = is_branch && (is_last_microop || !is_microop)`
- branch-only 统计与 count error 输出

这代表：

- 训练 / 评估 / 推理代码已经向下一代 schema 演进
- 但新的完整生产 ckpt 还没有生成

## 5. 历史版本演进

```
v2 → V9.5（17 字段 d/i 一致） → V10.1（i-side 改造） → V10.3（strict，剔除 macro_pc_id） → vNext（branch-mispred）
```

阶段性结论：

- `V10.1` 引入了 `i_group_*`
- 当前生产基线实际保留 `i_group_head / i_group_pos`
- 彻底移除全部 `i_group_*` 仍然只是未来升级目标，不是当前现实

## 6. 跨阶段契约清单

| 边界 | 生产者 | 消费者 | 物理介质 |
|---|---|---|---|
| Detailed trace + oracle | `02_taogen` (gem5 O3 + ref_sim 探针) | `03_tao_train/ml/dataset.py` | `*.parquet`（hive 分区） |
| Functional trace（A 子集） | `04_infer/functional_trace/extract_from_records.py` | `04_infer/driver/inference_driver.py` | `functional.core<N>.parquet` |
| Oracle labels（验证用） | 同上 | `scripts/03_validate.sh` 末段 | `labels.core<N>.parquet` |
| Cut-window baseline（固定） | `scripts/_build_cut_baseline.py` | `driver` 验证 / 实验报告 | `cut_baseline.json` |
| 单步预测（fl/el/mispred/head） | `04_infer/ml/infer.py` 或 driver | `driver/reference_clock.py` | jsonl 或 in-process tensor |

## 7. ckpt 契约

当前真实 checkpoint 格式来自训练代码的 `collect_state()`：

```python
{
  "model": model_state_dict,
  "optim": optimizer_state_dict,
  "sched": scheduler_state_dict,
  "step": int,
  "ema_loss": float,
  "best_loss": float,
  "cfg": TaoConfig.__dict__,
  "args": vars(args),
  "rng": {
    "torch_cpu": ...,
    "numpy": ...,
    "python": ...,
  },
}
```

说明：
- 旧文档中的 `model_state/meta/config` 口径已过时
- 现有推理与评估代码都按 `ck["model"]` 和 `ck["cfg"]` 读取

---

详细字段、loss、输出与 vNext 设计请直接参考 [global/SCHEMA.md](SCHEMA.md)。
