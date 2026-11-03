# private-L2 PMU 异常：参考来源修复（2026-09-16）

后续评估统一遵循 [评估口径与报告规范](accuracy-reporting-contract.md) 第 0、8 节的
数据源对照表、独立身份门禁、维护命令及发布检查清单。本文件保留本次事件的历史证据。

维护 40 案所报 private-L2 miss MAPE 16.7585%、P99 125.7652% 来自错误的
cache 参考字段。修复验证入口后，同一批 FastSim 原始结果的 MAPE 为 **3.4008%**、
P99 为 **17.7623%**、WAPE 为 **2.1627%**。本次没有修改缓存一致性、延迟或计数，
也没有实施 `shared_line_l2_refill`。剩余的建模误差仍然存在。

## 根因与证据

临时入口 `tmp/baseline-40-20260916/run_baseline.py` 原来的
`oracle_cpi_and_pmu()` 累加 `oracle/kernel-events-core*.json` 的
`pmu_user_plus_kernel`，把其中 `l2_misses` 当成原生 Ruby tag miss。
这些文件声明 `pmu_source=taotrace-path-class-v3`；字段名不是原生语义证明。

冻结 gem5 的来源是
`tmp/se-fs-paired-c4-20260820/gem5-se-isolated/src/`：

- `cpu/o3/probe/tao_trace.cc::accountCplDataPmu()` 根据 `a.path_class`
  累加旧 `l2_hits/l2_misses`。同文件的归因代码使用 TaoTrace 自己的 LRU 视图，
  不是 SLICC 私有缓存状态机的原生命中记录。
- `mem/ruby/protocol/MESI_Three_Level-L1cache.sm` 的 `uu_profileHit`、
  `uu_profileMiss`、`uu_profileUpgrade` 将真实控制器事件按请求身份写入 native
  sideband；本协议的 L1 controller 对应物理 private L2。
- `oracle/native-summary-core*.json:native_ruby_pmu_by_scope` 才是这里应当
  使用的原生分层人口。user-plus-kernel 汇总 user、syscall、page_fault、irq、
  scheduler，排除 idle/unknown；已有审计器负责生命周期、分层及作用域守恒。
- 维护工具 `tools/run_native_kernel_fastsim_validation.py` 的
  `build_pmu_comparison()` 原本已经正确选源；临时脚本绕过了它。

nab C32 的同窗证据：

| 事件 | 旧 path-class 代理 | 原生 Ruby/SLICC | FastSim | FastSim 对原生相对误差 |
|---|---:|---:|---:|---:|
| private-L2 tag miss | 13,208 | 38,254 | 36,314 | −5.0714% |
| private-L2 hit | 80,494 | 68,654 | 68,025 | −0.9162% |
| LLC tag miss | 5,263 | 230 | 243 | +5.6522% |
| LLC remote supply | 0 | 12,182 | 13,692 | +12.3953% |

原生 private-L2 另有 4,421 次 permission upgrade。
若定义数据命中比例为 `hits / (hits + tag_misses)`，gem5 为 **64.2178%**、
FastSim 为 **65.1961%**；不能将升级计数混入分母后与二分类命中率比较。
旧代理的 85.9% 不是真实 Ruby 命中率。“gem5 没有 remote_supply”也被同窗原生
计数直接否定。

FastSim `src/cache.cpp::PrivateHierarchy::access()` 在 L1 miss 时调用
`access_l2()`，其中 `l2_.access()` 已在 miss 上装入行。生产
`src/simulator.cpp::replay_memory_event()` 随后才进入共享层选择 remote supply。
因此当前实现不存在此处所猜测的“remote supply 不重驻留私有 L2”。本次没有把这种
即时功能填充解释为精确的响应时刻可见性模型；后者是另外的建模边界。

关闭 coherence 的旧消融得到 8,231 次 L2 miss，相对正确参考 38,254 是 **−78.4833%**，
实际显著恶化。它只证明改变一致性会改变驻留，不能证明 inclusion 是误差根因。
LLC inclusion 决定 LLC replacement 的反向失效；在几乎没有 LLC 逐出的 nab 上，
单独切换它不变化，也不能推出缺少 owner-supply refill。

## 数据范围与验证

- 来源：`tmp/first-core-common-end-20260911/source/`，10 负载 × C4/C8/C16/C32，
  40 配置、600 条逐核 trace。负载为 stockfish、omnetpp、zstd、lbm、sph_exa、
  tealeaf、nab、graph500、namd、neutron。
- 本轮是既有回归集合的 **user-plus-kernel 诊断重评**，未做校准、没有新增 held-out
  泛化结论。未重跑 gem5/FastSim，也不产出新的吞吐对比。
- 原始 FastSim 输出：`tmp/baseline-40-20260916/formal-*.json`。原脚本使用维护
  `configs/gem5-fs-native-kernel.cfg`。重评记录每个原始输出及 oracle/manifest/trace/
  事件字典/验证工具的 SHA256；不会把当前二进制或配置冒充历史输出的冻结 provenance。
- 40/40 通过 native hierarchy、drain、输入人口及 FastSim LLC outcome 守恒；
  0 个拒绝。另对 40 案调用 `validate_fs_oracle_identity.validate_result_identity()`，
  40/40 有效，使用实际 `effective-target.json`、`config.ini` 和 `uarch_profile.json`。
- 40 案均声明 `first-core-target-common-end-v1` 且功能预热开启。
  每核 warmup/measurement 精确 record、instruction、user record 边界及共同终点
  保存在各案 `tao_trace/trace.json:functional_boundaries` 与 manifest；预热不进入
  测量计数，barrier 后保留状态。重评没有改变这些边界。
- 事件字典为 `configs/pmu-event-dictionary-v1.json`：branch/L1D/private-L2 为
  `proxy`，LLC 为 `diagnostic`。这不是硬件 strict PMU headline。原始 combined
  FastSim 输出不提供可直接重评的独立 user cache PMU，本轮不伪造 user 表。

| 事件 | finite cases | MAPE | P50 | P90 | P99 | WAPE | signed bias |
|---|---:|---:|---:|---:|---:|---:|---:|
| branch miss | 40/40 | 3.2198% | 2.1042% | 9.3708% | 13.1128% | 1.7054% | −0.6745% |
| L1D tag miss | 40/40 | 2.4059% | 2.1234% | 4.8914% | 8.1627% | 2.0290% | +0.9871% |
| private-L2 tag miss | 40/40 | 3.4008% | 1.5709% | 9.1444% | 17.7623% | 2.1627% | +1.4126% |
| LLC tag miss | 40/40 | 4.5141% | 3.0728% | 11.7173% | 19.9927% | 1.9662% | +1.9662% |

百分位使用 Type 7；MAPE 按配置等权，WAPE 按原生参考事件总数计算。比如 branch
的 1.7054% 是 WAPE，不能写成 MAPE。private-L2 最大 APE 现为 NAMD C4 的
22.7493%（FastSim 31,959，gem5 26,036）；nab C32 不再是原报告的异常尾部。

| nab 配置 | gem5 L2 miss | FastSim L2 miss | 相对误差 | 绝对计数误差 |
|---|---:|---:|---:|---:|
| C4 | 2,257 | 2,139 | −5.2282% | 118 |
| C8 | 13,106 | 14,301 | +9.1180% | 1,195 |
| C16 | 37,231 | 38,628 | +3.7522% | 1,397 |
| C32 | 38,254 | 36,314 | −5.0714% | 1,940 |

CPI 使用 scope-matched active cycles / macro instructions。旧脚本直接使用
`measured_cycles`，其中包含 idle；修复后使用已有 oracle aggregate 的 active CPI，
FastSim 优先使用整数周期/指令数计算，避免显示值的舍入误差。

| nab C32 | gem5 CPI | FastSim CPI | 相对误差 | CPI 绝对误差（cycles/macro instruction） |
|---|---:|---:|---:|---:|
| user-plus-kernel | 0.638496 | 0.602853 | −5.5824% | 0.035644 |

| CPI 集合 | 配置数 | MAPE | CPI MAE（cycles/macro instruction） |
|---|---:|---:|---:|
| 总宏指令人口一致子集 | 33 | 5.0614% | 0.054931 |
| 全集，包含 7 项既有宏指令人口差异，仅诊断 | 40 | 5.6999% | 0.055726 |

这不是 CPI 改善：FastSim 的所有预测都沿用原始输出。33 项人口一致也不意味着本轮
重新认证了逐事件配对或独立泛化。

## 实施与复现

维护验证器新增 `--source-root`、`--stats-dir`，重评与新运行共用同一套原生选源及
守恒检查。缺失 native、scope 错误、文件缺失或案例不足时失败；不会回退至旧代理。
重评缓存的 fingerprint 包含原始 FastSim JSON。报告补充 CPI absolute error、CPI
MAE 和人口一致标记。临时入口改为调用维护验证器，旧脚本另存
`run_baseline.path-class-invalid.py`，旧汇总保留但已明确失效。

```bash
python3 tools/run_native_kernel_fastsim_validation.py \
  --source-root tmp/first-core-common-end-20260911/source \
  --stats-dir tmp/baseline-40-20260916 \
  --output-dir tmp/baseline-40-20260916/native-rescored \
  --expected-cases 40 --jobs 1
```

等价临时入口：`python3 tmp/baseline-40-20260916/run_baseline.py --rescore`。
不带 `--rescore` 则串行新运行并写到独立的 `native-replay/`。
机器可读 JSON、CSV 和 Markdown 均在 `native-rescored/summary.*`；每案
`cases/<case>/validation.json` 保留来源指纹、门禁、PMU 绝对/相对误差。

验证：先看到新增回归失败，再实现；11 项 PMU 验证器测试、16 项 native sideband
测试通过。`cmake --build build -- -j16` 与 `./build/fastsim_tests` 通过。
原有五个 C++ 文件中的默认关闭 remote-supply 探针保持原样，未提交。

结论：撤回基于 13,208 和 85.9% 提出的 inclusion/owner-supply 根因判断。
后续模型改动必须从原生请求/事件证据出发，不能为拟合旧代理而制造 L2 hit。
