# SPR 退休 miss 结果链修订：实施与验证记录

依据 `docs/superpowers/specs/2026-11-08-spr-retired-cache-result-path-repair-design.md`。
本记录是进行中的实现，不代表零 unknown 或最终验收通过。

生产链修订：驻留 L1 数据命中在 `h_load_hit`/`hh_store_hit` 完成回调前写入仅观察用状态；原 profile 动作保持次序。SPR Aggregate 从 Request extension 和 Snapshot 共用投影。Sequencer 重发时取得被替换的 Packet 事务 ID，在登记新 attempt 时关闭旧归属，保留被 TBE/follower 持有的服务，传递已观察 miss 位并标记重试估计。增加全局累积活对象、完成、替换、retain/release、存量和峰值诊断；squash 后尚在 Ruby 的请求仍需真实完成，不能把逻辑 squash 等同物理取消。

生产组件回归先复现并修复：扩展投影转发结果丢失；四条真实 SLICC Load/Load_Linked 命中动作顺序在完成前缺结果；70,000 次真实 Sequencer 发行片段重发触发容量异常。修订后同一回归通过，并覆盖重发时仍被 follower 使用的 TBE 以及重复完成和分片。FastSim 默认构建、`fastsim_tests`、`fastsim_perf_retirement_tests`、退休缓存契约回归 22 项和相关策略回归 6 项通过。gem5 gold `-j128` 重建通过；构建环境与失败重试日志位于 `tmp/spr-retired-result-path-20261108/`。

首轮修订后三模式使用原 checkpoint 恢复，C4/32GiB、500K warmup、50K target，三次均到 common-end、capture 返回 0、storage guard 未触发。独立目标身份有效，native response 结构守恒和 drain 通过；这些不等同事件分类完整。下表数值是 workload_task 各核 unknown，三个 miss 在同一核相同，不能相加作独立人口：

| mode | 核 0、1、2、3：退休 load 人口 unknown | 核 0、1、2、3：每个 miss 事件 unknown |
|---|---|---|
| 0 | 6、1、2、5 | 6、1、2、5 |
| 1 | 0、0、0、0 | 11、3、1、3 |
| 2 | 0、0、0、0 | 181、154、106、90 |

mode 0 剩余 unknown 标记为 unsupported，需核对实际指令/predicate；mode 1/2 的代表性未知样本为合并响应且缺少 admission 状态。mode 1 的全局生命周期计数为 created=1,246,715、completed=1,166,812、replaced=79,899、live=4、peak=31，TBE retain/release 均为 49,521；证明该次运行未重现容量异常，但不证明所有取消路径完整。

为继续定位，已加入有界 unknown 指令/predicate 样本和未识别合并控制器状态计数；不更改分类规则。诊断版仍在构建/采集过程中。不能跳过小 FS 零 unknown 和路径覆盖门禁；同起点中性、FastSim 同源回放、四负载均未执行，`hardware_equivalence=unverified`。

## 后续未知诊断与资格修订

诊断 mode 0 证据目录 `diagnostic2-mode0/` 的各核 unsupported load 分别为 7、4、4、4；有界样本无丢弃，分别有 7、4、4、4 个，均为同一退休 `prefetch`（PC 0x401f0f），`readPredicate=true`、`readMemAccPredicate=false`，该提示没有标记为实际数据读 UOP。这是实际样本，不依赖 `U-UNSUPPORTED-ISA` 标签推断。原表达式无条件把内存访问 predicate 置于整宏 unsupported 判定中，与既定“软件预取作为退休提示、异步请求可以不发生”的解释冲突。已对这一个资格条件修订：只在 `member.software_prefetch` 时允许 mem-access predicate 为 false，保留执行 predicate 和普通读取的原门禁。生产表达式回归已先失败后通过；FS 重验尚未完成。

mode 2 诊断目录 `diagnostic2-mode2/` 中，代表性 unknown 是 PC 0x401f9f 的普通 `mov`，实际 Sequencer coalesced，逐请求 admission 类别包含 `SP_IE_fill_credit` 和 `SP_IE_no_fill_credit`。全局累积未分类 admission 两类分别 26,281 和 35,084；这是从预热至结束的全局计数，不是退休人口，不能拿其作为事件总数。`SP_IE` 可由 I 上的 GETX 与 S 上的 UPGRADE 两种状态进入，store prepare 的 fill credit 或权限准备本身不能直接证明 follower 消费哪条数据路径，仍保留 unknown。上述模式 2 的诊断版进程运行时，源码新增了仅用于后续 mode 0 的采样范围调整；该次只用于读取其已构建二进制输出的原位诊断，不作为完整源码/二进制同一性或最终验收证据。

资格修订后的 gem5 正在重建，下一步需以新二进制重跑实际小 FS。SP_IE 消费者须在真实数据完成/合并边界建立可复现的服务关联，逐类验证才可关闭 unknown；不能仅凭 prepare 发起、信用位或共享回调猜测。继续保持前述中性、回放和四负载门禁未通过。

资格修订版 gem5 gold `-j128` 构建成功（`gem5-prefetch-build.log` 最后为 `scons: done building targets.`）。使用原 mode 0 checkpoint 的新采集 `prefetch-repair-mode0/` 到达 common-end，返回码 0，storage guard 未触发；独立 target identity 有效，native response 结构守恒、target drain 均通过，退休审计的 evidence gaps 为空。workload_task 四核 load 人口为 8567、8884、8928、8907，人口 unknown 均为 0；每核 L1/L2/L3 miss unknown 均为 0，正负与估计守恒由维护审计器检查。实际路径包含 data、软件预取、demand follower 和少量 prefetch follower；其他未出现的路径不以模式名称推断覆盖。

这是 mode 0 单模式结果，不能代表三个模式或真实硬件事件等价。mode 1/2 在前一次三模式采集中分别仍有 11/3/1/3 和 181/154/106/90 个每核主事件 unknown。`SP_IE` 要解决的不是增加上限或将 store prepare 直接当作 demand fill，而是将对应活动服务的实际数据完成、后续失效和每个 follower 的消费关联起来；目前这个生产链、取消边及其受控回归尚未完成。未再运行中性、同源回放或四负载。
