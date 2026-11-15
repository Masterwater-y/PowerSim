# 两阶段窗口内多核 store 合并

默认关闭的 `core.spr_store_multi_group` 依赖既有 `core.spr_store_group_merge`。关闭时仍是每个窗口全局最多一个组；开启时每核最多一组、四核窗口最多四组。组成员继续受已有同核、同 line、功能身份、预算和可见性检查约束，新增上限是宿主算法界限，不扩充目标 SQ、store buffer、Sequencer 或 TBE 容量。

候选在原两阶段批处理中一次前向形成。每个成功组仅保留一个层级 owner 请求，follower 仍保留退休人口和独立的 SQ/共享信用。所有组共享窗口事务与一次反馈：任何一组的权限或全核未来水位认证失败时，恢复整个窗口的缓存、目录、信用、反馈和计数，并最多一次走原始请求路径；这不是逐周期推进或反复迭代求解。此近似仍建立在 legacy buffer 上，**未补齐独立 store prepare hint 和 head demand**，也没有解除既有 group 路径对正式配置中 private preview、下层取指和 materialized fast kernel 的限制。

主工作树以 `cmake --build build -- -j16` 构建并通过 `fastsim_tests`；新增 `fastsim_spr_multi_store_group_tests` 和另 15 项相关专项通过。真实 Simulator 定向覆盖多核双组、单组开关兼容、跨核同线冲突、分核预算、任一组水位失败时整窗恢复，以及 FCFS/单遍 FR-FCFS；测试通过不等于完整负载准确或吞吐达标。

完整四案 C4 使用同一冻结二进制 `f7aa3d3b…` 和相同功能输入，每案两侧均启用原单组，仅切换多组开关；原预热、线程计划和 MMIO 保留。两侧共同关闭 private preview、取指下层与 materialized fast kernel，因此不能把它称为正式 canonical 配置的精度。并行墙钟不用于吞吐评价。输入只使用退休功能记录；`.sreq` 等当前采集与独立 Atomic/QEMU producer 的端到端等价仍未证明。

四案两侧均正常完成，v2 功能身份、task ID 与逐核退休人口一致，配置唯一差异为多组开关。CPI 未发生可观测变化：

| 负载 | gem5 CPI | OFF/ON CPI | CPI 绝对误差 | 相对误差 | 成功组 OFF/ON | 成功成员 OFF/ON | 模型层级请求减少 |
|---|---:|---:|---:|---:|---:|---:|---:|
| FLAC | 0.281872 | 0.320902 / 0.320902 | 0.039030 | 13.85% | 68 / 68 | 191 / 192 | 1 |
| Graph500 | 0.652711 | 0.754082 / 0.754082 | 0.101371 | 15.53% | 0 / 0 | 0 / 0 | 0 |
| Marian | 1.428304 | 2.896766 / 2.896766 | 1.468462 | 102.81% | 105 / 105 | 215 / 215 | 0 |
| XZ | 0.603524 | 0.645145 / 0.645145 | 0.041621 | 6.90% | 28 / 28 | 57 / 57 | 0 |
| 四案等权 | — | — | **0.412621** | **34.77%** | — | — | — |

绝对误差单位 cycles/macro，等权项为 CPI MAE 和 MAPE，非 WAPE；起点不严格一致。FLAC OFF/ON 候选为 70/72、2/3/4 成员组为 26/29/13 和 25/30/13，未来水位回退为 2/3；Sequencer 和 L1D 模型请求各减少 1，架构退休人口不变。该受限路径在本批没有可观测 CPI 收益，开关保持默认关闭。新输出未独立 native 重评分，原生参考复用同源已验证报告。

模型退休 L1 load miss OFF/ON：FLAC 7,698/7,698，Graph500 556,356/556,356，Marian 81,710/81,710，XZ 60,476/60,476。对应 native known 为 3,898/276,155/43,387/23,616；Marian/XZ native L1 unknown 为 4/27，不能评分；FLAC/Graph500 虽无 L1 unknown，事件资格仍为 incomplete-evidence。各案 native L2/L3 均有 unknown，不能完整评分。
