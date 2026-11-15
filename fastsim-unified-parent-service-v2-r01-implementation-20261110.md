# FastSim v2 R0/R1：隔离实施记录

日期：2026-11-10。状态：第一片代码已实现，组件测试、完整 Release 构建、全量回归与真实两阶段测试通过；五案 15/15 份中性回放全部通过。30 次串行吞吐配对已启动。完整 v2 尚未验收。

实施目录：`tmp/parent-service-v2-r01-exclusive-20261110/source/`。冻结比较基线：`tmp/parent-service-v2-exclusive-20261110/attempt2/source/`。主源码未合并，未执行 commit/push。

候选二进制 SHA-256：`dad62e0d67bea89cd9c69a946a2bd5e1eac89b7e9f3edf54704c0f81f13dc905`。

prepare 保持开启，原并行两阶段和反馈框架不变。本片保持当前 prepare 响应与资源资格算法；estimated 服务分量、扩大接管覆盖和统一容量准入尚未激活。LLC/预取迁移及延迟 Tag 发布继续关闭。

## 本片改动

| 改动 | 范围与合同 |
| --- | --- |
| 候选快照 | 行索引、expiry/资源释放堆和空闲槽索引使用两层目录的页级 COW；只有写入触碰页才复制，已接受根保持只读 |
| 有界行索引 | 使用受服务容量约束的哈希槽；墓碑在固定阈值下压缩，保存重建次数 |
| 命中快速返回 | 普通驻留 hit、无需租约且无活跃行索引时直接返回，记录人口 |
| 拒绝原因 | 新服务源候选按 load/prepare/store、动作、互斥首次结果计数；准入倒置、carried、lower return 缺失/区间及资源原因分开 |
| 时间接口 | 增加携带 Q16、时钟域、epoch 和证据质量的接口骨架；当前路径不消费新 estimated 边 |
| 成本诊断 | 输出索引页/目录复制次数与字节、重建次数和 hit 快速返回人口；复制统计仅覆盖最终提交的反馈分支 |

`source_ledger` 的分母是新服务源候选，不是全部提示扫描人口。direct/dropped 后建立的 mandatory 服务归 store；既有 transient 附着不新建源。GETX/UPGRADE 必须按 prepare 行单独归约，不能再用混有普通读的 `source_fallbacks` 总数计算 prepare 失败率。

这一片采用按触碰页复制的事务快照，尚未实现原地 undo journal。它避免每次候选复制整张索引，同时保留现有值复制/放弃候选语义。压缩仍有容量内扫描成本，端到端吞吐需要单独验证。

## 已完成的验证

- 组件测试通过：行索引插入/删除/墓碑压缩、快照隔离、只读零数据页复制、单行少量页复制、资源释放与槽代次保护。
- 20,000 步释放堆差分测试通过：同时间排序及 push/pop 与既有标准堆一致。
- GETX 互斥拒绝原因和逐核归约测试通过，新源候选人口守恒。
- Q16 时间接口区分零与 unknown，拒绝跨 epoch 的直接比较。
- 完整隔离 Release 构建通过，parallel LTO 为 16。
- 同一组件快照工作负载的基线与候选校验和均为 5,120,000，最终均保留 512 个记录。组件测量与构建存在宿主争用，不能外推为仿真整体开销。

构建和测试日志位于实施目录 `results/`。`fastsim_tests` 与 `fastsim_parent_service_tests` 均通过；最终构建日志为 `build-final.log`，五案验证入口日志为 `neutrality-driver.log`。

真实反例已用新候选重新运行：保留 32 个退休 UOP；产生 1 个 prepare GETX，其首次拒绝明确为 `admission_mismatch`。共享 issue=5、retire=6、minimum canWB=8；split services 仍为 0。见 `results/runtime-prepare-witness.json`。

冻结源码指纹为 `results/r01-source-freeze.json`，可审查补丁为 `results/r01.patch`，共 6 个源码/测试文件发生变化。生成的 `tmp/` 测试输入不进入补丁。

## 当前回放证据

全部 15 份回执均满足逐核目标输出一致和新源账本守恒；独立硬件身份门禁 5/5 通过。下面列完整五案的 `v2-wait` 结果；这些预测与冻结 v2 完全一致，不能解释为本片改善了 CPI。

| 案例 | gem5 CPI | 新候选 CPI | AE，cycles/macro | 相对误差 |
| --- | ---: | ---: | ---: | ---: |
| FLAC | 0.284157 | 0.318292 | 0.034136 | +12.01% |
| Graph500 | 0.966058 | 0.836117 | 0.129940 | −13.45% |
| Marian | 2.359792 | 3.093714 | 0.733921 | +31.10% |
| XZ | 0.609912 | 0.645956 | 0.036043 | +5.91% |
| Zstd | 1.023967 | 1.181232 | 0.157266 | +15.36% |

| 模式，五案 | CPI MAE，cycles/macro | CPI MAPE |
| --- | ---: | ---: |
| off | 0.220310 | 16.705219% |
| v2-observe | 0.220310 | 16.705219% |
| v2-wait | 0.218261 | 15.566555% |

此前 0.299333 cycles/macro 的 MAE 对应 FLAC/Graph500/Marian 三案子集；完整五案的 0.218261 使用不同样本集合，不能把两者差异解释为模型改善。参考复用保存的 active-CPI 结果，未恢复 native 审计。

prepare 专属首次拒绝账本显示：FLAC 的 3,078 个 GETX、52 个 UPGRADE，以及 Graph500 的 2 个 GETX、14 个 UPGRADE均因准入不匹配被拒绝。Marian 的 1,899 个 GETX、230 个 UPGRADE因准入不匹配被拒绝，另 1 个 UPGRADE是目标未知。这里的分母是新 prepare 服务源候选；后续尚未检查的代次与资源资格仍不能据此认定有效。

## 下一阶段门禁

冻结五案的 off/v2-observe/v2-wait 已完成逐核目标时序、人口、缓存计数与调度事实核对，同时通过 source ledger 守恒；身份使用维护入口独立验证。此处是模型中性检查，不是 native cache 来源审计。

中性检查通过后，另做安静宿主、同输入同绑核的交错吞吐配对。五案并行只用于中性回放和争用诊断。R0/R1 的性能措施验证后，再依[优化方案](fastsim-unified-parent-service-v2-optimization-plan-20261110.md)接入 estimated 服务分量、扩展 prepare 接管并迁移统一准入。

`continue_validation.py` 已通过 Linux pidfd 获得现有中性进程退出通知，没有重新启动该批次。15/15 门禁通过后已启动 `run_pairs.py`：五案串行、同一组物理核、冻结 v2 与新候选各三次交错配对，共 30 次。保留每次宿主 CPU 计时、进程、资源和目标输出核对，同时报告 measurement user-UOP/s 配对比值及同输入端到端 wall-time 加速比；后者核对 warmup/measurement 人口，不伪称 end-to-end user-UOP/s。安静宿主与性能结论须待原始证据审查。它不会自动开放 prepare 时间路径。

[服务时间组件实施记录](fastsim-unified-parent-service-v2-time-interface-implementation-20261110.md)描述另一个隔离目录中的后续接口准备，该组件尚未接入模拟器。其单元测试不代替真实 prepare、跨窗口或容量统一验收。

按用户指示不恢复 native 来源审计。若报告 CPI，继续使用已保存并核对的 gem5 active 参考，每案同时列 AE（cycles/macro）与相对误差，聚合列 MAE 与 MAPE；内部 cache 计数不升级为 native 或硬件 PMU 真值。
