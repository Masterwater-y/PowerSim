# 原两阶段模型的 load 返回与写回修复

日期：2026-09-09。状态：机制修复、构建、整套测试及必要 TeaLeaf C4 验证完成。
接续 [完整依赖接入](two-stage-dependencies-20260909.md) 和
[两阶段修复审查第二项](two-stage-repair-review-20260909.md#4-第二项在原-feedback-中统一请求时基与完成值)。

## 问题与实现

producer 的程序序访存边界可能将独立 load 的 memory event 向后推，但 UOP 保留原始
issue/completion。原反馈从较早的 UOP completion 加延迟差，不能保证它晚于对应
memory response。此次五 UOP 反例中，load 的模型返回为 119，完成和消费者 issue
却均为 117；修复后完成和消费者 issue 均为 119，末条完成为 120。

修复位于原 `compute_core_timing_feedback_impl` 遍历内：

1. 对普通 load 的所有 data fragments，求当前反馈所建模的最大绝对 response。
   使用已经转换到 core 时钟、包含当前 issue 位移的事件响应；保留事件与反馈索引身份。
2. 在执行／依赖／既有资源下界之外，增加该 data-ready 下界，再竞争 writeback 带宽。
   RAW 消费者、ROB 和有序退休沿最终 writeback 继续传播。
3. store、atomic、IFetch、page walk 保留各自响应规则；普通 load 下界没有套用到它们。
   StoreSet 仍携带 store 地址生成边，不因本次 load 返回修复改为等待 cache callback。

写回容量使用批次内 `ResponseWritebackCalendar`：开放寻址表记录 cycle/count，
按 accepted UOP 加 ROB 数量预留空间，generation 标记复用存储。工作量随 UOP 和实际
满槽碰撞数变化，不按延迟跨度展开或逐 UOP 分配对象。未来预约保留更早空档。
每个 checkpoint 从现有 sparse ROB 的完成记录恢复尚可能冲突的写回，占用不因切批丢失。

通用和 materialized 快速内核共用此规则；activity certificate 和 block transfer
同时检查写回容量／data-ready。失败的 block 候选不发布写回预约。原可选完整 FU/port
修复仍保持可选，本次没有开启它，也没有增加每批的完整反馈遍历。

`ChunkUopBound`、`UopIndex`、`ChunkMemoryEvent` 定义未增大，FST 格式和数据未改变。
跨 checkpoint 写回恢复的实现与验收针对原 sparse-scoreboard 两阶段路径；旧 non-sparse
近似路径仍保留其原资源策略。本次不将其升级成另一套精确 O3 求解器。

新增诊断计数为 `response_load_data_ready_repairs/cycles`。现有
`sparse_resource_writeback_*` 现在也包含上述写回容量处理，以往为零不表示原模型真实无
写回冲突。这些是模拟器内部诊断，不能直接当成 gem5 PMU 或 CPI 增量。
普通 load 的完成现在必须等待完整模型响应；`memory_exposure` 的旧延迟折减不能使其
提前完成。维护配置本来就是 `memory_exposure=1.0`，本次没有修改配置系数。

## 必要验证

- `cmake --build build -- -j16`、`./build/fastsim_tests` 通过。
- 新增 `tests/test_response_completion.cpp`：程序序访存起点错位、三片 load 的单次
  写回、同拍返回竞争窄 WB、未来预约保留早期空档、7-UOP chunk / 8-cycle epoch 的
  跨边界恢复，以及通用／快速内核等价。审计逐条检查 RAW、response≤WB 和 WB 容量。
- 原依赖、StoreSet、activity、block-transfer、窗口等测试一起通过。
- 工作负载仅使用已有 TeaLeaf：两次长输入初测，随后最终长输入 before/after 和完整
  依赖输入 generic/fast 共四次复核。模拟串行，未与本任务构建／测试重叠；未重采集，
  未运行其他负载或新矩阵。最终源码在测试后仅澄清了容量表注释。

## TeaLeaf 长输入：精度与吞吐

同一 C4 长输入、冻结维护配置，测量段 40,000,002 用户 UOP，宏指令分母 25,297,808。
gem5 CPI 沿用现有冻结对照 `current-optimal-v28_6-c4-c32-20260827/accuracy.json`
中该 case 的 **1.081237829**。下列误差使用这套既有口径，不混用其他 sideband 周期数。

| 指标 | 修改前 | 修改后 |
|---|---:|---:|
| 合计 core cycles | 25,683,641 | 25,871,081 |
| cycles / user UOP | 0.642091 | 0.646777 |
| cycles / 用户与内核宏指令 | 1.015252 | 1.022661 |
| 相对 gem5 的 CPI 有符号误差 | −6.1028% | −5.4176% |
| 测量段用户 UOP 吞吐 | 6.7701 M/s | 6.4896 M/s |
| 全程 wall time | 6.4509 s | 6.7140 s |

CPI 绝对误差减少约 **0.685 个百分点**，吞吐下降 **4.14%**。初测也观察到约 4.6%
吞吐下降，因此不能宣称无开销。它仍保留原框架吞吐量级，但不等于全部负载通过准入。

| PMU 相对误差 | 修改前 | 修改后 |
|---|---:|---:|
| branch misses | +3.7232% | +3.7232% |
| L1D tag misses | −5.3428% | −5.3407% |
| private L2 tag misses | +0.0206% | +0.0231% |
| LLC tag misses | +0.0328% | +0.0328% |

PMU 基本不变，CPI 收益主要来自时序约束。测量段 933,872 UOP 的完成下界被修正，
下界增量合计 5,314,207 cycles；写回碰撞等待合计 2,628,337 cycles。这些等待相互重叠，
不能相加作为 CPI 收益。目标时序改变使反馈调用数 6,319→6,370；每批遍历轮次没有增加。

## 完整依赖短输入

沿用 L1D=64 KiB、DRAM=3 GiB 的已采集 C4 输入：全程 2,939,373 UOP，测量段
40,000 用户 UOP 加 1,903 内核 UOP。

修改前 38,131 cycles，修改后 generic/fast 均为 **38,157 cycles**；cycles/user-UOP
从 **0.953275→0.953925**，这个窗口没有改善。两内核的全部 scope PMU、core/totals
和 CHA 输出一致，PMU 也与修改前相同。修正 200 UOP、下界增量 2,050 cycles，WB
碰撞等待 1,302 cycles；两者反馈调用均为 16 次，快速内核处理测量段全部 41,903 UOP。

## 已解决与剩余边界

本次保证普通 load 不早于**当前模型响应**写回，并在原 sparse 路径传播写回容量约束。
没有使用 case/PC 系数、gem5 issue/response 标签或 CPI 残差补偿。

当前响应仍来自既有 `MemoryReplay` 服务结果及其 issue 位移投影。这不证明移动到达后
原 hit/merge/DRAM 路径仍有效，也不消除程序序 clamp 自身可能多加的等待。下一项需要
识别哪些请求因移动而跨过 fill/permission/resource 边界，并在原批次机制中处理相应
descriptor 失效／局部重算。不能把这次下界修复描述成 cache 生命周期已经闭合。

证据：[tmp/two-stage-response-20260909](../tmp/two-stage-response-20260909/)。
包含修改前快照、冻结配置、反例 before/after、初测与最终输出、冻结参考、
`summary.json`、`after-sha256.json`、构建和测试日志。
