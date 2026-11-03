# SQLite 四核 NO_ACCESS 重采集与验证（2026-10-20）

后续完整周期账、分支顺序反例与独立对照见 [SQLite CPI 误差来源分析](sqlite-cpi-retirement-diagnosis-20261020.md)。本页保留重采集时的生产默认结果。

使用已修复 gem5 重新建立身份绑定的 KVM checkpoint，恢复短窗口检查后采集正式窗口，再以当前生产 FastSim 的冻结副本运行维护 native validator。没有修改负载、ROI、延迟常数或模型开关。

## 1. 来源与流程

- 标准 `708.sqlite_r`，官方参数 `--memdb --size 2000 --testset main --verify`；4 个单线程 rate 副本、4 核、8 内存通道、3 GiB、buffer TSO 114。
- 延用已验证的 workload SHA 和计算 ROI 标记。既有四副本输出重新逐字节核对官方 reference；这是历史输出的身份复核，没有重新运行宿主完整 benchmark。
- 新 checkpoint 绑定当前 gem5、原 frozen runner、workload、launcher、内核和磁盘身份；没有改写旧 checkpoint。
- 恢复 smoke 的第一核测量目标为 100K user UOP；正式目标为 10M，使用 first-core-target-common-end-v1，保留全部核到共同终点。
- 两次恢复均执行 KVM → 100 万指令 warmup → stat；guest UART 核验四个单线程任务。

| 阶段 | 宿主耗时 s | warmup s | FST bytes | 正常退出 / 无守卫中止 |
|---|---:|---:|---:|---|
| checkpoint-creation | 86.548 | N/A | 288 | 是 |
| recovery-smoke | 134.510 | 55.174 | 490,084,576 | 是 |
| source | 421.289 | 54.104 | 3,011,709,472 | 是 |

## 2. 新功能语义的覆盖

四条 FST 均声明完整 v1 请求角色并带有 finalized `.mreq`。缺行表示该版本的普通/其余请求；零条 NO_ACCESS 也是合法的完整分类，不能把它误报为没有采集。逐核核对源记录数、稀疏行顺序、测量/预热边界与模型消费数；完整流 hash 由 FastSim 读取器校验，维护 validator 指纹绑定四个伴随文件。

| 核 | 测量记录 | 全源 NO_ACCESS | 预热 NO_ACCESS | 测量 NO_ACCESS | native local_access |
|---|---:|---:|---:|---:|---:|
| 0 | 9938216 | 0 | 0 | 0 | 0 |
| 1 | 10011235 | 0 | 0 | 0 | 0 |
| 2 | 9982613 | 0 | 0 | 0 | 1 |
| 3 | 9921347 | 0 | 0 | 0 | 0 |

**本轮测量窗口没有 NO_ACCESS 微操作，但请求角色信息已完整采集并由模型消费。** 因此 Zstd 中针对 CDA 的本地 SQ/扫描路径没有在这个 SQLite 测量窗口触发，不能期待它自动消除 SQLite 的 CPI 差额。

native local_access 是原生终止分类，可能包含其他本地访问；不要求与纯功能 NO_ACCESS 行数机械相等。模型仍保留 CPU、译址、SQ 和宏/UOP 数量，仅按角色排除不应产生的数据请求。

## 3. Active CPI 与 elapsed

统计范围为 recorded-owner workload-task；新源双方宏指令分母一致，为 **23,590,370**。下面历史源与新源来自不同自然执行窗口，不是同一动态 trace 上的模型修复前后对照，不能把差值全部归因于 NO_ACCESS。

| 来源 | gem5 active CPI | FastSim active CPI | 相对误差 | CPI 绝对误差 / 单案 MAE |
|---|---:|---:|---:|---:|
| 旧源，缺请求角色 | 1.000286 | 0.772986 | -22.72% | 0.227301 |
| 本轮新源，完整请求角色 | 1.000925 | 0.773267 | -22.74% | 0.227658 |

绝对误差与单案 MAE 单位为 cycles/宏指令；不把两个来源当成两个负载做总体 MAPE。

| 来源 | gem5 elapsed ms | FastSim elapsed ms | 相对误差 |
|---|---:|---:|---:|
| 旧源 | 1.968546 | 1.521277 | -22.72% |
| 本轮新源 | 1.966026 | 1.518904 | -22.74% |

Elapsed 为目标系统经过时间，独立于 CPI 分子和宿主采集/审计耗时。

## 4. 退休 PMU：原始计数及缺口

**Incomplete-evidence errors**：参考为已观察子集，模型为估计值，硬件 perf 等价未认证。unknown 不当作 0，也不一律当作 miss。

| 退休事件 | gem5 已观察 | FastSim | 暂定 APE | gem5 unknown | 模型 unknown |
|---|---:|---:|---:|---:|---:|
| retired_l1_load_hits | 6,532,111 | 4,555,532 | 30.26% | 580,971 | 2,562,309 |
| retired_l1_load_misses | 71,887 | 74,426 | 3.53% | 580,973 | 2,562,324 |
| retired_l2_load_hits | 716 | 769 | 7.40% | 588,276 | 2,562,324 |
| retired_l2_load_misses | 71,165 | 73,658 | 3.50% | 588,276 | 2,562,324 |
| retired_l3_load_hits | 155 | 161 | 3.87% | 588,294 | 2,562,356 |
| retired_l3_load_misses | 70,992 | 73,465 | 3.48% | 588,294 | 2,562,356 |
| retired_load_fb_hits | 7,297 | 0 | 100.00% | 580,973 | 2,562,324 |
| retired_loads | 7,192,190 | 7,192,190 | 0.00% | 9 | 0 |
| retired_stores | 3,100,343 | 3,100,343 | 0.00% | 9 | 0 |

仍缺部分 native 退休/服务成员分类、ISA/event qualification 证据，错误路径与预取对 cache 状态的间接影响也未完整建模。补齐 NO_ACCESS 功能身份不会自动补齐这些服务证据。

## 5. Ruby tag miss 与 branch 诊断

Cache 真值只来自维护 validator 审计的 conserved Ruby/SLICC native summaries；不使用 path-class cache 代理。下面与上节退休 load 事件独立，不能互相改名替代。

| 诊断 | gem5 | FastSim | APE |
|---|---:|---:|---:|
| branch_misses | 136,580 | 136,679 | 0.07% |
| l1d_tag_misses | 73,007 | 74,874 | 2.56% |
| llc_tag_misses | 71,570 | 73,465 | 2.65% |
| private_l2_tag_misses | 71,749 | 74,035 | 3.19% |

## 6. 验证收据与限制

- 来源、共同终点、线程归属、硬件身份、指令人口及 native 守恒检查通过；不等于精度阈值达标或硬件 PMU 认证。
- FastSim 的本地扫描仍为响应侧约束估计，严格 cache 准入扫描和 buffer/PTE 服务组合未完整实现。本轮没有修改它们。
- 未发布新的吞吐测量。旧 SQLite 来源保留用于回归，未删除其他接受输入。
- 全工作区 FST 25,094,248,000 bytes（25.09 GB），磁盘剩余 426.52 GB，低于 100 GB FST 上限。

证据：[完成检查](../tmp/sqlite-noaccess-recapture-20261020/completion.json)、[采集命令](../tmp/sqlite-noaccess-recapture-20261020/campaign-commands.json)、[冻结身份](../tmp/sqlite-noaccess-recapture-20261020/frozen-identity.json)、[新 checkpoint](../tmp/sqlite-noaccess-recapture-20261020/checkpoints/c04-708.sqlite_r/fastsim-checkpoint.json)、[正式源](../tmp/sqlite-noaccess-recapture-20261020/source/c04-708.sqlite_r/)、[维护验证](../tmp/sqlite-noaccess-recapture-20261020/validation/cases/c04-708.sqlite_r/validation.json)。
