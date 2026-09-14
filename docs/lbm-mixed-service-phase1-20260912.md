# LBM 修复第一批：持久混合控制器与 store 服务证书

日期：2026-09-12。状态：组件实现及测试通过；**完整 LBM 修复尚未完成，默认配置未变**。

本轮按照 [LBM common-end 诊断](lbm-common-end-error-audit-20260912.md) 开始实现，
采用测试先行、独立组件审查和最终任务差分审查。没有提交或推送；保留工作区原有修改。
任务产物：`tmp/lbm-mechanism-repair-20260912.OEvBcu/`。

## 1. 已实现的改动

### 1.1 独立持久化 RD/WB 控制器

新增 `include/fastsim/mixed_dram.hpp`、`src/mixed_dram.cpp`，并加入常规 CMake 库和
`fastsim_tests`。服务 ID 包括 core、动态 sequence、fragment 和 RD/WB kind；
队列、实际准入、读写方向与 turn 计数、bank/rank 日历和在途响应跨调用保留，可复制回滚。

关键合同及实际定向测试：

| 机制 | 已验证的行为 |
|---|---|
| 读空闲写进展 | 写数严格大于 low 才启动；冻结阈值下 65 个 WB 排出 18 个，留下 47 个 |
| RD/WR 时序 | 12 种同/异 BG、rank 方向组合；旧 rank 的约束不被中间命令覆盖 |
| 写恢复 | WR 后 PRE 包含 CWL、burst、tWR；不再使用 read-like PRE 公式 |
| 页面策略 | adaptive 扫描已准入的当前方向队列；相反方向请求不参与该次决策 |
| 选择范围 | selection 在排他 frontier 之前；command/response 可在其后，晚到 row hit 不能倒改选择 |
| 容量释放 | RD 在 DRAM ready 释放读缓冲，外部 response 另含 frontend/backend；ID 保留到外部响应 |
| 跨批状态 | 同一流整批输入与分 frontier 输入的 ID、选择、命令和响应完全相等 |
| 异常安全 | 非法时间或累计延迟范围在修改状态前拒绝，随后合法重试不丢失请求/结果 |

这不是 gem5 整体控制器的精确复刻：调度明确命名为 `kRowHitFirstApproximation`，
尚无 hidden-bank/seamless FRFCFS、refresh、同线合并/转发、命令总线仲裁或 rank 电源状态。
所有时间仍是显式整数 cycle，不读取 gem5 标签、不改生产 burst=10，也不根据 CPI 调参。
WB 的完成字段表示 DRAM 数据服务结束，不是普通 store callback 或 gem5 的入队确认。

**该控制器目前没有生产 Simulator 调用者，因此本节不能解释下方任何 CPI 变化。**

### 1.2 修复现有 post-commit 路径的真实证书缺陷

旧代码可把一个 store epoch 标记为稳定，但不保留其已接受的请求顺序；随后独立
FR-FCFS 又按旧 AGU proposal 改写服务。测试在修改生产代码前复现：第二条普通 store
的共享服务原点是 cycle 8，实际 send 已是 cycle 117。

现在 FR-FCFS 在 post-commit 固定点内部求解，接受的 batch 与核心反馈一同发布；
成功后不得再执行独立修复覆盖该证书。测试核对 shared origin=final send、
shared response=store drain/SQ release，要求 FR-FCFS 实际成功，并验证 generic 与
materialized 内核一致。尚未联合求解的 response/causal/ROB-suffix retime 组合显式拒绝。

FR-FCFS 诊断计数是内部调用累计，包括被外层拒绝的试算；
`store_post_commit_request_stable_epochs` 才是外层已接受 epoch 数。不能把两者直接相除
当作成功覆盖率。物理 cache/DRAM/CHA/核心计数仍按事务回滚。

此修复不等于全部请求资源已在 actual send 预约；也没有解决普通 store 的跨 Q 未决服务。

## 2. 验证记录

最终生产/测试改动之后执行：

```sh
cmake --build build -- -j16
./build/fastsim_tests
```

均退出 0，整套测试输出 `all FastSim tests passed`。独立控制器优化构建及 ASan/UBSan
通过；LSan 因当前 ptrace 环境不支持而未验证。完整 LTO 链接仍有已披露的 serial LTRANS
提示，不能称为全构建无警告。四个故意破坏阈值/WR 恢复/选择范围/adaptive 方向的变体
均被测试拒绝。溢出问题由独立审查发现，修复后的范围证明和测试再次通过独立复审。

最终 CLI SHA256：`d98d6b773ece3dd93d5ebc74c68bda7b3503ed6a71447a1d14d73329bcc84163`。
旧默认 CLI：`f704f0bf52f95ade02a6d9eb5f988de93b290f2ca6f109b9425d2265db822e73`。
完整配置 include 链、命令、输出和校验保存在任务目录；源码配置目录与本轮入口快照
逐项相同，ordinary-load=3、response-to-ready=1、Q=1024 均保留。

完整 C32 验证使用 `tmp/first-core-common-end-20260911/source/formal-32c-782.lbm_r/`，
32 条 trace，`user-plus-kernel`，功能 warmup 不计分，实际宏指令分母 240,123,783，
用户 UOP 308,016,035；共同终点 tick 19051778228256。输入/参考身份门禁和 common-end
边界均检查；不截取冷 10k 窗口，不重新采 gem5。该 LBM case 已用于诊断，是回归集合，
不是独立 held-out。

四次完整 C32 模拟均完成。默认配置和关闭 private preview 的控制组，scope（排除宿主
吞吐）及 threads 与冻结默认精确相等；post-commit 同配置下，新旧二进制也精确相等。

| C32 对照 | gem5 CPI | FastSim CPI | 相对误差 | CPI 绝对误差 | 32 核 CPI MAE（不加权） |
|---|---:|---:|---:|---:|---:|
| 默认，新二进制 | 3.906000 | 4.200373 | +7.5364% | 0.294373 | 0.321020 |
| 仅关闭 private preview | 3.906000 | 4.200373 | +7.5364% | 0.294373 | 0.321020 |
| post-commit，修复前 | 3.906000 | 4.301644 | +10.1291% | 0.395644 | 0.426127 |
| post-commit，修复后 | 3.906000 | 4.301644 | +10.1291% | 0.395644 | 0.426127 |

CPI 绝对误差和 MAE 均为 cycles/macroinstruction。四组均为 32 核正误差；按宏指令数
加权的逐核 CPI MAE 分别为 0.294373 / 0.294373 / 0.395644 / 0.395644，不存在核间正负
抵消。本表是同一 case 的配置/版本干预，不是四 case 聚合或新的全矩阵 P99。

**本次证书修复在完整 LBM C32 没有 CPI 收益，也没有新增这项 CPI 回退。** 更差的
4.301644 是既有 post-commit 开关的结果，修复前后完全一致；不能把开关差当作补丁差。
相对默认多出的周期是 24,317,577。

关键覆盖门禁失败：31,308 个 post-commit 候选 epoch 仅 1 个稳定，31,307 个全部因
实际到达跨 horizon 而回退；记录了 90,023,175 次共享事件重放。新混合控制器尚未接入，
其对已知关键请求的生产直接覆盖为零。不能用内层 FR-FCFS 计数增加声称覆盖成功。
因此没有扩展 C4/C8/C16 或全 40 项，也没有报告新 P99、完整阶段账本或 quiet-host 吞吐。

证据入口：任务目录 `phase1-verification.json`，包含全部逐核 CPI/绝对误差及指纹。
旧二进制对照的包装脚本曾因配置循环变量遮蔽二进制 digest，导致模拟完成后的 hash
断言失败；原失败元数据已保留并标无效。二进制本身的实际 SHA 不变，独立校验器对已
完成输出重新检查 scope、所有逐核人口、输入/配置和指纹并通过，没有重新生成模拟结果。

## 3. 尚未完成的接入门槛

现有 `SharedSystem::access` 同步返回完整 latency，并立即给 fill/MSHR 安装数值完成时间。
但持久控制器在合法 frontier 内可能尚未选中某个读请求。不能为了拿到 latency 提前
排空该批，也不能把旧响应、零或最大整数当作真实新响应。

继续实施必须补齐三组状态：

1. 分离共享请求提交与完成发布；保存未决 fill、follower、dirty-WB 和资源 owner。
2. 核心保存可恢复的局部状态和服务等待者，区分发现/准入游标与退休游标；SQ/ROB 等
   非 RAW 资源边也要恢复，允许不依赖未决 load 的年轻请求继续参与竞争。
3. 生产 lookahead、共享选择 frontier 和核心退休进度分别维护，证明所有来源没有遗漏
   更早请求；零新 UOP 的 epoch 也必须推进在途服务，跨 Q ID 不随 chunk 回收而消失。

Q-entry 全批回放只能作为“所有被消费请求均已安全选中”的有限开发路径，不能解决任意
未决读/晚发送 store。详细接口及源码依据见任务目录 `frontier-integration.md`。
当前不扩到 40-case，不推广新默认，不声称 LBM/P99 或 quiet-host 吞吐门禁已通过。
