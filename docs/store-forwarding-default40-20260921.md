# core.store_forwarding 实现与 buffer-114 精度复核（2026-09-21）

> **后续机制诊断（同日）：** [LBM buffer-TSO 误差分析](lbm-buffer-tso-error-mechanism-20260921.md)
> 已通过完整四核数干预定位主要来源：过保守的同组权限准备门禁让约 69%–73%
> 的普通 store 重新串行化。LRU 驻留证书候选将 LBM MAPE 从 117.84% 降至
> 9.97%、CPI MAE 从 1.129515 降至 0.113737，但 Graph500 控制回退，未推广。
> 本文原始矩阵不变；“跟不上延迟隐藏”仅是现象概括，不再作为根因分析。
> PMU 还有 prepare-vs-demand 的事件域差异，不能把大 gap 一律归为
> inclusion/coherence；详见后续报告的原生路径核对。

按用户明确要求，在 `interval_weave` 的 load 路径新增纯功能性 store-to-load 转发，
由开关 `core.store_forwarding` 控制，**默认关闭**。关闭时全部新分支为死代码，
模型与未加该开关时位级一致。显式 `--store-forwarding true`（或 `core.store_forwarding = true`）
开启，`--store-forwarding false` 回退。本轮未改动 buffer-TSO 机制
（`tso_store`/`needsTSO`/`storeInFlight`），未复活已删除的 `causal_read`，
未调整 DRAM 标量或放宽任何兼容性 guard，未把开关设为默认开启。

**结论。**
1. **store_forwarding 就绪且可回退**：默认关闭时位级一致，开启不引入系统性精度回退，
   也不违反硬守恒不变量（retired / memory_accesses / branch 关闭==开启 39/39）。
   是否默认开启仍待用户决定。

> **更新（2026-09-24）：** 用户已决定**默认开启**。默认翻转、全矩阵 CPI/PMU/吞吐
> 复核见 [store-forwarding-default-on-fullmatrix-20260924.md](store-forwarding-default-on-fullmatrix-20260924.md)：
> 38 例有效矩阵精度中性略带微弱正向（MAPE 7.113%、MAE 0.0584），硬守恒不变量保持，
> 吞吐无回退。
2. **CPI 精度必须在与 gem5 采集一致的 TSO 几何下评估**：20260918 矩阵是 gem5
   **full-system O3 + buffer-114**（`tso_store=buffer`, `SQ=114`, `needsTSO`）采集，
   其 `perf_like_cpi` 是**真实 O3 timing 真值**（下文铁证）。在**匹配的 buffer-114** 口径下复跑：
   全 39 案例 MAPE **17.30%**、CPI MAE **0.157**；**除 lbm 外 35 案例 MAPE 仅 5.81%（max 14.4%）**；
   **lbm 是真实大残差**（MAPE **117.8%**，73–153%），FastSim 的 buffer-TSO 建模跟不上
   gem5 114 深 store buffer 对访存的隐藏。

> **重要更正（2026-09-21，先读，作废本 doc 前一版的核心论断）。**
> 前一版曾判定 20260918 矩阵的 `perf_like_cpi` 是 "atomic 伪 CPI、无效"，并据此把 lbm
> 的大误差解释成 "口径错配假象、lbm 完全正常（3.22%）"。**该论断错误，现予作废。**
> 铁证（`gem5.log` 命令行 + stats）：
> - `--restore-cpu-type o3 --resume-at-roi`、`[fs-ckpt] action=restore … cpu=atomic->o3`、
>   `[fs-ckpt] switching CPU: atomic -> o3`：atomic 只是**启动/检查点 CPU**，**ROI 运行在 O3**；
> - `--cache-hierarchy mesi-three-level` 且 ROI stats 有真实 Ruby cache 活动
>   （lbm c04 `l1_controllers0.Dcache.m_demand_hits=1006568 / m_demand_misses=103939`）；
> - `[fs-ckpt] configured O3 TSO store=buffer buffer_size=114 effective_sq=114`；
> - lbm c04 `switch{0..3}.core.numCycles=5482679`，×4=21918322=`measured_cycles`，
>   `unknown_kernel_cycles=0`，`perf_like_cpi_user_plus_kernel=0.81732620245047`。
>
> `config.json: board.mem_mode=atomic_noncaching` 仅描述**启动核**；`gem5.log` 的
> "atomic core with Ruby → skip caching" 警告也只针对启动核。ROI 是带真实 MESI 三级
> Ruby cache 的 O3。**因此 0.817 是货真价实的 buffer-114 O3 timing 真值，不是伪 CPI。**
>
> lbm 早先出现的大误差，真实成因是 **TSO 几何错配**：生产配置
> `configs/gem5-fs-native-kernel.cfg` 解析为 **serial-32**（`store_wait_relief=true`、无 buffer），
> 却去对照 **buffer-114** 采集的 gem5 目标。lbm 极端 store-bound，gem5 自身
> serial-32 与 buffer-114 的 CPI 相差约 4×（lbm c04：serial 参考 3.231 vs buffer 参考 0.817）。
> 用 serial-32 模型比 serial-32 gem5 参考，lbm 看着"正常"（~7%）；一旦按采集口径改到
> buffer-114，真实残差才暴露出来。

## 机制形态（最小、贴合项目目标）

- 在 chunk 构建阶段按逐核程序序一次性判定：此处已知该 load 的提交字节范围与
  结构化 store 队列深度。每核维护一个 store 转发窗口，记录仍驻留 SQ 的普通 store
  的字节区间 `[addr, last_byte]`，容量由 `config_.effective_sq_entries()` 界定
  （serial 模式为 `sq_entries`=32；buffer 模式为 `tso_store_buffer_size`）。
- 一个纯 load 若其完整字节范围被某个更老的窗口内 store **完全覆盖**，即被标记为
  `forwarded_load`。replay 对该事件走合成路径 `kL1 / 1 周期 / kLocal`：不生成私有
  cache 或 Sequencer 请求，不产生 cache miss。**部分覆盖**不标记，保持既有 stall/重试语义。
- 窗口在序列化操作、内存屏障、原子操作与 locked RMW 处清空，转发绝不跨越排序边界。
- 仅时序建模。FST trace 不携带 store 数据值，本机制只改变该 load 的完成时序与
  是否发请求，不重建数据依赖，不动 buffer TSO。
- 复用 `forwarded_loads` 统计口径（`CoreCounters`），totals 与逐核数组均落地；
  逐核求和与 totals 精确一致。
- 落地位置：`include/fastsim/config.hpp`、`src/config.cpp`（含 `core.model!=interval_weave`
  的组合 guard）、`src/main.cpp`（配置回显 + 两处子命令 CLI 布尔）、
  `src/simulator.cpp`（判定块、窗口维护、`forwarded_load_replay` 及 3 处 replay/preview 入口 guard）、
  `include/fastsim/types.hpp`。

## 单元/回归验证

- `cmake --build build -- -j16` 通过；`./build/fastsim_tests` 打印 `all FastSim tests passed`，
  含新增 `test_store_forwarding_functional`：关闭时转发 0 次，开启时在 64 对合成
  store/load 上恰好命中 32 次（偶数 load 读同址、奇数读不相交），retirement 与
  memory_accesses 守恒，l1d 访问相应下降，周期不增加。
- Python 回归 `tests/test_store_forwarding.py`（`unittest`）：配置键存在且默认关闭、
  关闭==无该键位级一致、`forwarded_loads` totals==逐核求和、关闭恒为 0。

## 数据与口径

- 数据：`tmp/tso-buffer114-common-end-baseline40-20260918/formal/source/`，
  10 负载 × C4/C8/C16/C32。可用矩阵 39 案例（C32 不含 777.zstd_r；
  `uarch-baseline-c08-857.namd_s` formal-v1 失败，改用已审计的替代证据目录
  `tmp/native-prefetch-fix-namd-c08-exact10m-20260920/`）。本轮未新跑 gem5、未重采、未截短输入；
  既有矩阵此前用于采集正确性审计（identity/common-end/native 守恒/zero-unknown 通过）。
  gem5 采集为 **full-system O3 + buffer-114**（见上文更正铁证）。
- **CPI 精度必须用与采集一致的几何复跑**。生产链
  `configs/gem5-fs-native-kernel.cfg` 默认解析为 **serial-32**（`store_wait_relief=true`、无 buffer），
  与本矩阵的 buffer-114 采集口径**不一致**；直接用它 replay 会对 store-bound 负载产生
  几何错配误差。本轮 CPI 复跑显式对齐采集几何：
  `--tso-store buffer --tso-store-buffer-size 114 --store-wait-relief false`。
- **gem5 CPI 真值**：`oracle/kernel_events.json:aggregate.perf_like_cpi_user_plus_kernel`
  （O3 timing，宏指令口径，`unknown_kernel_cycles=0`）。**FastSim 预测**：
  `scope_metrics.perf_like_cpi`（活跃周期 / 各自完成宏指令数）。逐案例 FastSim 分母
  与 gem5 `user_plus_kernel_retired_instructions` 相对偏差 < 0.0003%（common-end 边界对齐，
  3–31 条指令 / 千万级），未把 gem5 时间或分母注入 FastSim。CPI 绝对误差与 CPI MAE 单位为
  cycles/宏指令，非百分比。
- 每案例在同一 buffer-114 配置、同一冻结 trace 下，`--measurement-scope user-plus-kernel`
  关闭/开启各跑一次。开关对照的逐核指令/UOP、warmup 人口一致。

## 硬不变量（39 案例矩阵，全部通过）

| 门禁 | 结果 |
|---|---|
| 全部案例 `status=ok`（buffer-114 复跑） | 39/39 |
| `retired_instructions` 关闭==开启 | 39/39 |
| `memory_accesses`（horizon 分类）关闭==开启 | 39/39 |
| `branch_misses` 关闭==开启 | 39/39 |
| `forwarded_loads` totals==逐核求和 | 39/39 |
| 关闭时 `forwarded_loads`==0（惰性） | 39/39 |
| 几何全部 buffer/114/active/relief-off | 39/39 |

转发在每个负载上都命中（占 memory_accesses 比例 min 0.15% / mean 13.95% / max 41.86%）。
`l1d`/`private_l2`/`llc` 计数在 OFF→ON 会下降，这是**转发的预期效果**（被转发 load
跳过 cache 访问），不是守恒违反；lbm c04/c08 因转发命中全部落在 L1 命中集内，
cache miss 计数不变。硬守恒（retired/mem/branch）严格成立。

## 逐负载 CPI 精度（buffer-114，O3 timing 真值，本轮实跑）

> gem5 参考 = `kernel_events.json:aggregate.perf_like_cpi_user_plus_kernel`（buffer-114 O3）。
> FastSim = buffer-114 复跑（`--tso-store buffer --tso-store-buffer-size 114 --store-wait-relief false`），
> 关闭 store_forwarding（OFF）作为精度基线。CPI MAE 单位 cycles/宏指令。

| Workload | n | gem5 CPI 区间 | CPI MAE | MAPE% | max APE% | bias% |
|---|---:|---|---:|---:|---:|---:|
| 706.stockfish_r | 4 | 0.59–0.70 | 0.00843 | 1.36 | 3.77 | −0.86 |
| 710.omnetpp_r | 4 | 0.74–0.81 | 0.06246 | 8.03 | 9.05 | −8.03 |
| 777.zstd_r（无 C32） | 3 | 0.64–0.66 | 0.04553 | 6.93 | 8.21 | +6.93 |
| 782.lbm_r | 4 | 0.82–1.50 | 1.12952 | **117.84** | **153.05** | +117.84 |
| 803.sph_exa_s | 4 | 0.40–0.57 | 0.01103 | 2.18 | 3.55 | +1.19 |
| 811.tealeaf_s | 4 | 0.51–1.00 | 0.09813 | 13.51 | 14.36 | −13.51 |
| 816.nab_s | 4 | 0.45–0.64 | 0.02017 | 3.64 | 5.07 | −3.64 |
| 854.graph500_s | 4 | 1.31–2.69 | 0.07688 | 3.88 | 5.71 | −3.88 |
| 857.namd_s（含替代 c08） | 4 | 0.44–0.48 | 0.04810 | 10.46 | 13.50 | −10.46 |
| 881.neutron_s | 4 | 1.66–1.94 | 0.04360 | 2.54 | 4.80 | +2.28 |
| **全 39 案例** | 39 | — | **0.15718** | **17.30** | **153.05** | +13.89 |
| **除 lbm（35 案例）** | 35 | — | **0.04605** | **5.81** | **14.36** | — |

聚合诊断（OFF，Type-7 百分位）：全 39 案例 P50 5.07% / P90 26.17% / P99 143.80% /
WAPE 20.88% / signed bias +13.89%。**除 lbm 外 35 案例** P50 4.80% / P90 12.68% /
P99 14.33% / max 14.36%。开启 store_forwarding 后聚合几乎不变（全 39 MAPE 17.32%，
除 lbm MAPE 5.82%）。

**要点。**
- **匹配几何下非 lbm 负载精度良好**：35 案例 MAPE 5.81%、max 14.4%，最大离群是 tealeaf/namd
  的低估尾（≈−13%）。
- **lbm 是真实大残差**，不是口径假象：全 4 个核数 73–153% **高估**，转发占访存 40–42%。
  gem5 的 114 深 store buffer 把 lbm 的 store 延迟隐藏得极彻底（serial→buffer CPI 降 74.7%），
  FastSim 的 buffer-TSO 只隐藏了约一半（serial 3.452 → buffer 2.068，降 40.1%），
  故对这条极端 store-bound 负载残留系统性高估。这是 buffer-TSO **时序建模**的下一步靶点，
  与 `core.store_forwarding` 无关（转发对 lbm CPI 影响 < 0.4%）。

### serial-32 与 buffer-114 两套 gem5 参考的对账

lbm 早先"~7% 正常"来自 **serial-32 对 serial-32** 的一致比较，并非错误数据——只是**问错了几何**。
两套 gem5 参考都是有效采集，几何不同：

| lbm 案例 | serial-32 gem5 参考 | buffer-114 gem5 参考 | FastSim serial-32 | FastSim buffer-114 |
|---|---:|---:|---:|---:|
| c04 | 3.231 | 0.817 | 3.452（+6.8%） | 2.068（+153%） |
| c08 | 3.202 | 0.822 | — | 1.880（+129%） |
| c16 | 3.528 | 0.952 | — | 2.058（+116%） |
| c32 | 3.906 | 1.503 | — | 2.606（+73%） |

- **serial-32 冻结护栏**（`configs/cpi-guardrail-baseline-v1.json`，12 案例，band ±1.0pp）
  仍以 serial-32 gem5 参考为准，用于 store_forwarding 的**非回退**门禁（见下节），其 lbm
  "正常"是 serial-vs-serial 的正确结论。
- **buffer-114 矩阵**用于回答"按 SPR 对齐的 buffer-114 目标，FastSim 精度如何"——答案是
  非 lbm 好、lbm 有真实残差。两者不矛盾，测的是不同 TSO 几何。

## store_forwarding OFF-vs-ON 影响（39 案例，buffer-114，perf_like_cpi）

> 本节是 **FastSim 关闭 vs 开启的自比较**（`cpi_off` vs `cpi_on`，同 buffer-114 配置同冻结 trace）。

`|Δcpi rel|` 平均 0.181%、最大 1.326%；方向双向（20 更快 / 19 更慢 / 0 不变，阈值 0.001%），
均值 +0.002%，无系统性偏置。转发本身从不增加工作量；微小周期变化来自被转发 load 的
1 周期完成对逐核 memory-order clamp 的扰动。Graph500 仍是最敏感负载（与既有 C1/C13 敏感性一致），
其 C4 为最大变化 −1.326%。

| Workload | 四核数 rel% (min..max) | 均值 rel% |
|---|---|---:|
| 706.stockfish_r | −0.043 .. +0.062 | +0.002 |
| 710.omnetpp_r | −0.042 .. +0.045 | −0.014 |
| 777.zstd_r（无 C32） | −0.155 .. +0.498 | +0.168 |
| 782.lbm_r | −0.367 .. +0.174 | −0.005 |
| 803.sph_exa_s | −0.269 .. +0.294 | −0.018 |
| 811.tealeaf_s | +0.034 .. +0.263 | +0.127 |
| 816.nab_s | −0.038 .. +0.025 | −0.012 |
| 854.graph500_s | −1.326 .. +1.236 | −0.122 |
| 857.namd_s（含替代 c08） | −0.240 .. +0.143 | −0.033 |
| 881.neutron_s | −0.064 .. −0.005 | −0.028 |

逐案例完整表见 `tmp/sf-validate/buffer114-cpi-report.json`（forwarded_loads、fwd%mem、
cpi_off/on、Δcpi、守恒标记、几何回显）。

## 冻结 CPI 护栏（12 案例，serial-32 gem5 宏指令 CPI，band=±1.0pp）

用 `configs/cpi-guardrail-baseline-v1.json`（含冻结 serial-32 gem5 宏指令 CPI 与有向 signed-error band）
在**默认 serial-32** 配置下关闭/开启各跑一次，作为 store_forwarding 的非回退门禁。
**开启在 band 内 12/12，守恒全部成立。**

- store_forwarding 专属 signed-error 位移（ON−OFF）最大 **0.198pp**（band 宽 ±1.0pp）；
  转发**未把任何案例推出 band**。
- 唯一 OFF 越界案例 `formal-32c-782.lbm_r`（OFF signed +6.47% vs band 下沿）是
  **与 store_forwarding 无关的既有二进制漂移**（冻结基线用了不同 binary sha256），
  开启不改变其越界性质；此护栏仅用于判定"开启不引入回退"，结论成立。

（本护栏是 serial-32 口径，与上文 buffer-114 精度矩阵测的是不同几何，互不替代。）

## 逐计数器 PMU 误差（39 案例聚合，native Ruby/SLICC；ON−OFF 变化）

> PMU 真值取 native Ruby/SLICC（`native-summary-core*.json`，
> `cache_reference=native-ruby-slicc-controller-actions`，`path_class_cache_scored=False`），
> branch 为 `taotrace-retired-bpred-v1` 代理；经
> `tools/run_native_kernel_fastsim_validation.py` 关闭/开启各跑一次，先过身份门禁。
> **口径声明：非硬件-formal 认证；LLC 为 diagnostic 映射，余为 proxy。**
> 已实测验证：branch / l1d / private_l2 / llc / memory_accesses / retired 在
> serial-32、buffer-114-OFF、buffer-114-ON **三者逐案例完全一致**——PMU 计数由 native
> cache 语义决定，与 TSO 几何和 store_forwarding 均无关。故 PMU 只需一次权威 native pass，
> 不随 buffer-114 复跑改变。

| Counter | 映射 | OFF MAPE% | ON MAPE% | ΔMAPE | OFF WAPE% | ON WAPE% | OFF bias% | ON bias% |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| branch_misses | proxy | 3.518 | 3.518 | 0.000 | 1.555 | 1.555 | −0.379 | −0.379 |
| l1d_tag_misses | proxy | 29.825 | 29.800 | −0.026 | 19.881 | 19.879 | +19.181 | +19.179 |
| private_l2_tag_misses | proxy | 81.499 | 81.421 | −0.078 | 29.736 | 29.733 | +29.033 | +29.027 |
| llc_tag_misses | diagnostic | 179.052 | 179.052 | +0.000 | 131.706 | 131.707 | +131.706 | +131.707 |

- **branch_misses 位级不变**（转发不改控制流；ΔMAPE=0）。
- **l1d/priv-L2 轻微改善**：转发命中的 load 跳过 cache 访问，OFF→ON 预测 miss 略降，
  MAPE 各降 0.026 / 0.078pp。
- **llc 基本不变**（诊断口径）。cache 计数的大 gap（priv-L2/LLC）是既有的
  inclusion/coherence 语义差（native 把 upgrade/merged/remote-supply 记为不相交结果），
  与本开关无关，此处不作模型修正。

## 复现

```bash
cmake --build build -- -j16 && ./build/fastsim_tests
/root/miniconda3/envs/yinhaolang/bin/python3 -m unittest tests.test_store_forwarding -v

# buffer-114 逐负载 CPI 精度 + OFF/ON（与采集几何一致，本轮实跑）：
/root/miniconda3/envs/yinhaolang/bin/python3 tmp/sf-validate/run_buffer114_cpi.py --jobs 39
#   -> tmp/sf-validate/buffer114-cpi-report.json（含 agg_off/agg_on 与逐案例）

# serial-32 冻结护栏 + 回归/守恒（store_forwarding 非回退门禁）：
/root/miniconda3/envs/yinhaolang/bin/python3 tmp/sf-validate/run_sf_validation.py --jobs 16

# native PMU（真值 native Ruby/SLICC），关闭/开启各一次；PMU 与 TSO 几何无关：
/root/miniconda3/envs/yinhaolang/bin/python3 tools/run_native_kernel_fastsim_validation.py \
  --source-root tmp/tso-buffer114-common-end-baseline40-20260918/formal/source \
  --fastsim build/fastsim --config configs/gem5-fs-native-kernel.cfg \
  --output-dir tmp/sf-validate/native-off --expected-cases 38 --jobs 12
/root/miniconda3/envs/yinhaolang/bin/python3 tools/run_native_kernel_fastsim_validation.py \
  --source-root tmp/tso-buffer114-common-end-baseline40-20260918/formal/source \
  --fastsim build/fastsim --config tmp/sf-validate/gem5-fs-native-kernel-sf-on.cfg \
  --output-dir tmp/sf-validate/native-on --expected-cases 38 --jobs 12
# c08-857.namd_s 替代目录单独并入（native-{off,on} 与 buffer114 runner 均已含），达 39/39 覆盖。
```

## 后续（尚未执行）

- **lbm buffer-114 残差是下一步靶点**：FastSim 的 buffer-TSO 时序建模只隐藏了约一半
  gem5 114 深 store buffer 的 store 延迟。应按访存契约的组件优先级——先做 store 提交
  可见性 / occupancy / ordering 的成对事件对账，再看条件延迟分布，最后才用 CPI 分布定论——
  排查 buffer-TSO 的 store-drain 时序。**不得**用 CPI 单独把误差归到某个组件。
- 是否把 `core.store_forwarding` 设为默认开启，待用户决定。buffer-114 与 serial-32 两套
  口径均显示开启近中性、无系统性回退；默认开启前建议对 Graph500 等敏感负载做
  warmup/measurement 分离复核。
- 本机制为时序-only；若未来 trace 携带 store 数据值，可评估是否扩展到值级转发，
  但当前 FST 无此信息，保持时序判定。
