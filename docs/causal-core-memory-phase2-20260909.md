# 因果事件模型：混合指令与真实前缀接入

日期：2026-09-09。接续 [A 阶段](causal-core-memory-phase1-20260909.md)，完成 B 阶段中的
FP/SIMD、普通 store、分支及功能依赖扩展。维护默认未切换，完整 native-FS CPI 尚未验收。

后续多核、一致性、连续预热与内核/序列化接入见
[第三阶段记录](causal-core-memory-phase3-20260909.md)；本文保留第二阶段当时的范围和结果。

## 实现与机制边界

- `include/fastsim/op_traits.hpp`：把旧 IntervalCore 的纯操作分类表抽成共享定义，复用
  整数、FP simple/complex、SIMD、predicate 的 FU、延迟和流水化配置。没有新增拟合参数。
- `src/causal_read.cpp`：普通 store 的 address/data execute、writeback、ROB retire、
  cache send、各 fragment response 和 SQ release 分开。SQ 项独立保存，ROB 退休后仍在；
  `needs_tso` 开启时一个 store 的所有 fragment 返回前，下一个 store 不发送。
- load 在地址生成后检查旧 store。完整字节覆盖且尚未全部发送时可以转发；部分覆盖等待
  store 发送后重新查层次。尚未执行的旧 store 地址保守等待，不以 trace 中提前可见的
  地址作为运行时消歧答案。这个保守近似尚未替换成预测/违例恢复模型。
- dirty L1 eviction、L2 对 L1 的包含失效、LLC eviction 沿下层传递完整脏行；末端调用
  现有 `DramModel::enqueue_write`。完整行写回不制造额外 read-for-fill，store 的 SQ
  完成也不等待将来的 DRAM dirty eviction。L1 fill 被 L2 失效取消时，已提交写数据
  进入下层写回，不能随被丢弃的 L1 安装一起丢失。
- 分支在实际 fetch 使用既有 predictor；预测失败后，正确路径的后继 fetch 等待该
  分支实际 writeback 加已配置恢复延迟。load 延迟通过 RAW 链自然影响分支恢复。
  不重建 wrong-path 指令，也没有用 gem5 分支时间作为输入。
- 真实接入暴露了 predictor 的接口假设：`schedule_commit` 原先只匹配最新 checkpoint。
  现在按 sequence 匹配未登记的 checkpoint，支持多条分支同时在飞，仍在 retire 的下一
  cycle 才让训练结果对 fetch 可见。旧 interval 调用保留 newest checkpoint 的常数时间路径。

`core.model=causal_read` 保留历史配置名，JSON 状态更新为
`experimental-single-core-mixed`。核心统计区分最后退休时刻与尾部 store/写回传输完成时刻；
`drained_cycle` 不表示 DRAM 内部 buffered writes 已全部执行完，后者有独立 pending 计数。
SQ 占用积分从 dispatch 计到 response 后的有序回收，不能与旧 O3 每 UOP displacement 相加。

## 超过四个源操作数

原始 TeaLeaf core1 功能输入在 ordinal 6 就出现 `n_src > 4`，而 FST hot record 只有
四个 producer distance 槽。整个 10,318,177-record 输入共有 866,274 条此类记录，不能
直接放宽检查后漏掉依赖。

新路径在源声明完整且有效的静态寄存器操作数表时，维护功能顺序的宏指令寄存器写者。
对于截断记录，补上读寄存器对应生产宏指令的所有仍活动 UOP；消费者通过同一套实际
完成通知唤醒。不能只取生产宏指令最后一条 UOP，因为它可能先于其他生产成员完成。
已退休成员视为已完成，状态按寄存器数和活动流水线容量有界。

这是一项保守的宏指令级依赖补全，不宣称恢复了原始全部微操作寄存器边。完整的四槽
记录仍使用其动态 producer distance；没有完整 operand map 的截断记录继续报错。
没有 PC 白名单、workload 判断、参考 timing/path 或误差系数。

## 必要验证

最终构建 `cmake --build build -- -j16` 和 `./build/fastsim_tests` 均通过。新增检查集中于：

1. FP complex FU 共享及非流水化占用、独立 SIMD 发射、load→FMA、load→分支恢复。
2. 多条在飞分支按各自身份登记提交；截断依赖等待生产宏指令全部活动成员。
3. store 提交早于响应、SQ 满时的正确释放、TSO、完整/部分转发、未知旧 store 地址、
   跨行 store 端口及尾部响应。
4. 脏行跨 L1/L2/LLC 传递到控制器写队列。

已有带宽、load 生命周期和基础回归随测试程序运行。未重跑全矩阵、上一轮八组 CLI、
随机种子扩展或 sanitizer。实际前缀发现 predictor checkpoint 问题后，修复并重跑受影响检查。

### 真实 TeaLeaf 连续前缀

从原始 core1 功能 trace 的 ordinal 0 开始，固定取前 10,000 条宏指令（17,503 UOP），
通过现有 BinaryTraceWriter 保留功能记录、ASID、operand map 和所用页映射。逐字节比较
证明全部 64-byte UOP 记录与原始连续前缀一致；没有按指令类型删除或改写记录。

本次使用单核冷启动、理想取指/翻译的显式实验配置。它不是四核完整 ROI 的 CPI 对照，
也不是未参与机制设计的 held-out workload。

| 检查项 | 结果 |
|---|---:|
| 普通 load / store | 833 / 833 |
| 分支 / 预测失败 | 1,668 / 32 |
| 截断操作数补全 | 1,666 UOP |
| 实际 load / store fragment callbacks | 833 / 833 |
| 最后退休 / 尾部 store 传输完成 | 14,805 / 14,902 cycles |
| ROB dispatch-blocked elapsed cycles | 9,649 |
| ROB / IQ / LQ 占用积分 | 2,716,581 / 318,450 / 137,772 |
| SQ 占用积分 | 220,937 |
| 最大活动 UOP | 224（ROB + frontend 容量） |

从 CSV 独立重建全部队列占用积分，核对原始四槽 RAW、分支恢复、TSO、逐 fragment
返回及阶段带宽，均通过。这些中间状态还没有与相同配置和初始条件的 gem5 配对。

同一前缀另在旧 `interval_bound` 路径运行，改动前后二进制的整个 core/thread JSON
逐项相等，均为 27,648 cycles；这验证共享操作表与 predictor 接口调整没有改变旧路径。
该周期不能与新路径相减后解释为 CPI 精度收益。

产物：[`tmp/causal-mixed-implementation-20260909/`](../tmp/causal-mixed-implementation-20260909/)。
`tealeaf-prefix/provenance.json` 记录输入身份；`events.csv` 是实际事件；
`tealeaf-prefix/validation.json` 和 `audit_prefix.py` 记录检查和重建方法。
最终二进制 SHA256：`fe08606b199a74dfcb715c4ad424d6f5e6314a15e773b5b776f55cecc636cb16`。

## 尚未完成的真实负载机制

仍缺多核共享状态/一致性、真实 I-side/翻译、native kernel/系统调用/序列化/原子操作、
ASID 切换、连续 warmup→measurement 边界和 DVFS。cache 写回传输当前没有端口/缓冲
反压；DRAM 复用原 FCFS 与 buffered-write 近似，没有新增 FR-FCFS 或 refresh。

因此本轮证明真实混合指令可以进入新的事件链，以及已实现边的内部一致性；它没有证明
完整 TeaLeaf 的 CPI 误差减少。下一接入重点是多核共享事件所有权和连续边界，同时补齐
取指、翻译与串行事件；保持服务参数固定，再进行相同完整 ROI 的 gem5 中间状态和 CPI 验收。
