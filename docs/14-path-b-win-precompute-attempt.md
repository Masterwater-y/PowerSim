# 14 - 路径 B（win.* 离线 precompute）尝试与回退记录

> 编写日期：2026-06-10
> 状态：**已回退**。改动全部撤销，仓库回到 docs/13 Phase 1（FASTENC）落地后的状态。
> 关联：docs/13 §4 Phase 2 第一次实施尝试，本次结果决定下一步必须直接攻 phase1a 主体下沉，不能绕路。

## 1. 目标与背景

按 docs/13 §4 Phase 2，要把 phase1a 的 CPU 串行段（约 30.8% 的 wall）从 GIL 单线程扩到 16 核真并行，前提是先消除 `phase1a_probe` 内每行调用 `OnlineWindowFeatures.derive_before_update + update` 的 GIL 抢占。

路径 B 的设计：复用已 PASS 的 FASTENC 同款"离线 precompute → quantum loop 内查表"思路，把 win.* 的 12 个特征**离线串行算完**，运行时 phase1a 改成 numpy 切片塞进 `enc_fast.mat`，从而：

1. 消除 win 状态机的 GIL 持占
2. 让 ThreadPool 接管 phase1a 时不再被 GIL 排队

## 2. 实施快照（已回退）

主要改动：

- 新建 `infer/driver/window_precompute.py`：per-core 串行重放 `OnlineWindowFeatures`，输出 `[n_rows, 12] int32` 矩阵
- `CoreState` 加字段 `win_precomp`
- `phase1a_probe` FASTENC 分支增加 `win_precomp is not None` 查表分支
- `_build_cores` 末尾根据 `TAO_INFER_WIN_PRECOMP=1` 触发预计算
- quantum loop 前增加 `phase1a_auto_safe`：win_precomp + timing-functional 时自动把 `phase1a_workers` 升到 cores 数

灰度开关：`TAO_INFER_FASTENC=1 TAO_INFER_WIN_PRECOMP=1`，关闭即完全回退。

## 3. 实测结果（W11_stream_mix）

bit-exact 验证：所有 3 轮 md5 完全一致，cpi/precision/recall/pmu 12/12 全部对得上（路径 B **数值正确**）。

| 配置 | num_cores | wall_s | rps | vs baseline |
|---|---|---|---|---|
| FASTENC baseline (`fastenc_on_121941`) | 16 | 152.77 | **10473.3** | 1.00× |
| winprecomp + auto-ThreadPool（首版）| 16 | 162.94 | 9819.6 | **0.94×** ❌ |
| winprecomp 单线程 + dyn 单切片写（修复版）| 16 | 168.42 | 9500.1 | **0.91×** ❌ |
| winprecomp + auto-ThreadPool | 4 | — | 5900.6 | ≈ 5862.5 (持平) |

4 cores 上持平、16 cores 上反而**退化 6-9%**——与预期"+5% 持平、再 ThreadPool 上 +20%"完全相反。

## 4. 退化根因

逐项剖析为什么路径 B 在 16c 单线程下反而比 baseline 慢。

### 4.1 启动 overhead 不可忽略

`precompute_window_features` 启动时按 core 串行重放 OnlineWindowFeatures（含 deque/defaultdict/计数器全套 Python 操作）一遍。16 核 × 100k = 1.6M 行 ≈ **+10s wall**。

baseline 路径下 win 状态机是和 phase1a 其它工作"边算边用"，没有独立的预热段；路径 B 把它前置成了"先全量算完再开跑"，启动期是新的 wall 来源。

### 4.2 numpy ↔ Python 边界开销吞掉收益

baseline 单行 dyn 拼装：
```python
dyn = [oracle.get(kk, 0) for kk in oracle_keys]      # 16× dict.get
dyn += [win_attrs[kk] for kk in win_keys]            # 12× dict.get
```

路径 B 单行 dyn 拼装：
```python
dyn = [oracle.get(kk, 0) for kk in oracle_keys]      # 16× dict.get
dyn.extend(int(v) for v in win_precomp[i])           # 12× np.int32 → Python int
```

`int(np.int32_scalar)` 比 `dict.get(str_key)` **更慢**（Python C-API 走 numpy scalar 协议层，每行 +12 次额外边界穿越）。

### 4.3 "win 状态机本身只占很小一部分"

之前 fastenc_prof 实测 phase1a 总占 14.4%，其中 win.* 调用占的 wall 远没有 30.8% 那么大——这个 30.8% 是把 zip/tolist/batch_probe Python 循环/group_features_idx/mat 赋值**全部加在一起**的"phase1a 内 CPU 串行段"。

把 12 列下沉只能砍掉其中很小一片，**没有任何分支独立大到能补偿启动 overhead 与边界开销**。

### 4.4 ThreadPool 在主体未下沉时是负收益

phase1a 内仍有 5 处持 GIL 的 Python 调用：`zip(numpy.tolist...)` × 9、`sim.batch_probe(cid, fields_list)` Python 循环、`group_features_idx` 逐行调用、`oracle.get` 列表推导、`mat[i, ...] = dyn` 切片赋值。

16 个线程进来后，根本拿不到真正并行度——它们在同一个 GIL 上排队，反而比单线程多了 ~10s 的 ThreadPool 调度 overhead。这就是首版 -6.2% 的来源。

## 5. 决定与回退

**回退路径 B**。理由：

1. 单线程下数值正确但吞吐 -9.3%
2. 启动 overhead + numpy/Python 边界开销在 phase1a 主体未下沉前无法补偿
3. ThreadPool 在 GIL bound 主体下不可能有正收益
4. 路径 B 增加 ~150 行代码与一个新文件，但当前阶段无净收益，**留着只会把后续真正的优化路径搞浑**

回退动作（已完成）：
- 删除 `infer/driver/window_precompute.py`
- 移除 `inference_driver.py` 中：`from window_precompute import ...`、`CoreState.win_precomp`、`phase1a_probe` win_precomp 查表分支、`_build_cores` 中 `TAO_INFER_WIN_PRECOMP` 入口、quantum loop 前 `phase1a_auto_safe` 自动并行钩子
- `inference_driver.py` 回到 docs/12 Phase 1（FASTENC）落地后的状态，bit-exact 不变

## 6. 经验教训

1. **GIL 瓶颈的"片段下沉"是负收益陷阱**：只下沉小段（win 12 列）但保留主体（zip/tolist/batch_probe/group_features_idx）持 GIL，ThreadPool 上线就是负收益，单线程就是边界开销。**要下沉就要一次到位**。

2. **离线 precompute 不是免费**：FASTENC 之所以赚到 1.50×，是因为它把"逐行特征 dict 构造 + 编码查表 + Token 拼接"整段重链路替换成了 numpy block 写。win precompute 只换了一段（12 列），主链路没变，反而引入边界穿越 + 启动 wall。

3. **phase1a 主体的 GIL 大头不在 win.***：之前我把 30.8% CPU 串行段的全部责任都挂在 win 上，是误判；实际更大的占比来自 zip/tolist/batch_probe Python 循环。这是下一步必须解决的真正核心。

4. **bit-exact ≠ 性能正确**：md5 对得上只能保证数值，不能保证 wall。性能验证必须独立做。

## 7. 下一步（接 docs/13 §4 Phase 2 重新规划）

不能再做"win 单点下沉"。phase1a 主体必须**整段**下沉。两个候选：

### 候选 A：纯 numpy 化 phase1a 内层（推荐先做）

把 phase1a 内层（除 group_features_idx 外）改成 numpy block 操作：
- `fields_list` 的 9 个 `tolist + zip` → 直接传 SoA 列引用给 batch_probe
- `batch_probe` 改成"返回 numpy [n, 16] 矩阵"，不再返回 list-of-dict
- oracle 段直接 `mat[lo:hi, dyn_lo:dyn_lo+16] = oracle_block`

预期收益：phase1a 主体 GIL 持占从 16 行 × 多次 dict 操作 → 1 次 block 操作。配 ThreadPool 真并行 16c × 工作。

风险：`group_features_idx` 是 stateful（更新 `prev_macro_pc/prev_i_cl/last_*_pos`），仍需逐行 Python 调用，可能成为新瓶颈——需要先 profile 确认其单独占比，再决定要不要把它也 numpy 化（无 deque、纯计数器，比 win 容易 vectorize）。

### 候选 B：C++ pybind 整段下沉（gil_scoped_release）

把 `phase1a_probe` 内层（包含 batch_probe 调用、win 状态机、group 状态机、enc 写入）整段移到 C++，与 mesi_ref_sim 同款 `gil_scoped_release` 释放 GIL。

风险：~400-500 行 C++ 移植，3 个 stateful 子模块（OnlineWindowFeatures、group 状态、batch_probe oracle）必须严格对齐 Python 实现，bit-exact 风险高。

### 决策建议

先走候选 A。理由：
- numpy 化只动 Python 文件，bit-exact 风险低（沿用 FASTENC 同款"block 写"思路）
- 一旦发现 group_features_idx 等仍卡 GIL，再走候选 B 不迟
- 候选 A 落地后 ThreadPool 才有上线价值，到时实测 16c 收益 → 决定要不要进 C++

下一份文档：实施候选 A 时再写 `docs/15-phase1a-bulk-numpy-sinking.md`。

## 8. 状态总结

| 项 | 状态 |
|---|---|
| 路径 B 代码 | **已回退**（删除 window_precompute.py + 撤销 driver 改动） |
| FASTENC（Phase 1） | 仍生效，10473 rps @ 16c W11 |
| docs/13 §4 Phase 2 | 改"win 单点下沉" → "phase1a 主体 numpy 化"，pending |
| docs/13 §4 Phase 3-4 | 不变（pinned h2d / 多卡分担），仍依赖 Phase 2 完成 |
