# 两阶段请求起点修复与必要验证

日期：2026-09-09。接续 [Graph500 回归分析](graph500-response-origin-regression-20260909.md)。
状态：生产实现、构建、完整测试和三个负载的定点验证完成。Graph500 和 Stockfish
改善，TeaLeaf 控制小幅恶化；不声明整个模型或 P99 已通过精度验收。

## 实现

修改 `src/simulator.cpp` 中原有 `compute_core_timing_feedback_impl`，不新增开关、
参数补偿或完整遍历。通用和 materialized 内核共用同一实现。

1. 普通数据事件合并绝对就绪条件：`request=max(event_origin, uop_origin+delay)`。
   event 的位移为相对自身 origin 的剩余量，排序等待与 RAW/dispatch 等等待不再相加。
2. sequencer/MSHR/数据重启等真实资源阻塞若进一步移动请求，将该绝对下界投回 UOP
   起点，再更新 UOP issue/FU 下界。不能把归一化后的 event 位移直接当成 UOP 位移，
   否则会漏掉真实资源阻塞。纯功能排序 envelope 不额外变成 UOP 的 FU issue 位移。
3. 每个 data fragment 在已有 `memory_issue_extra_q16` 缓冲区保存自己的反馈；后续
   timing replay 使用该 fragment 的请求起点，不再统一覆盖成 UOP 的最大位移。
   既有 issue-resource 修正和 checkpoint closed-gap 转换仍在同一出口追加。
4. store 的 post-commit/TSO send 保留为绝对时刻，再逐事件导出相对位移，避免覆盖
   地址生成的 UOP issue；DRAM 生命周期审计也使用相同事件位移和 store send。
5. IFetch 保留自己的起点；PTE 请求保留 translation/walker 链。load 最新片段返回、
   单次 WB、WB 容量、跨 Q 的 ROB/WB 恢复继续保留。store/atomic 原有执行和完成
   规则保留，本轮统一其数据请求位移，没有扩展设备或中断模型。

`memory_producer_issue_q16` 的选择逻辑未改变，添加了起点合同注释：服务 latency 与
导出的 memory displacement 都以该事件起点为基准。没有只改成 raw issue、却继续
沿用旧起点计算的服务延迟。功能排序、原 cache 服务路径选择及其近似仍然存在。

FST、热 UOP/event 描述符和公共配置/API 无变化；复用现有 per-event 缓冲，不新增
逐 UOP 分配或全局事件队列，不采用 causal_read。新增工作是现有循环中的整数运算
和已有缓冲区读写，不增加反馈轮次。

## 机制验证

- `cmake --build build -- -j16` 成功；包含新增测试的 `./build/fastsim_tests` 输出
  `all FastSim tests passed`。
- 扩展 `tests/test_response_completion.cpp`：用独立慢 ALU 和较早的 cache miss
  分别产生排序与 RAW 就绪下界，覆盖两种等待覆盖关系，以及单片和三片 load。
  检查请求按绝对下界合并、服务延迟起点一致、RAW 和 WB 约束，以及 generic/fast 等价。
- 定向单槽 sequencer、单槽 L1 MSHR 测试，确保归一化后真实资源阻塞仍投回 UOP
  起点；加入 store/atomic 请求起点检查。
- 新回归测试链接此前已证明零开关等价于旧模型的诊断库时，明确失败于
  `request charged both overlapping issue-ready and ordering delays`。修复后通过。
- 原跨 Q/WB、未来写回空档、分片、DTLB hierarchy、TSO/pending-fill、StoreSet、
  服务失效回退和快速路径测试一起通过。本轮没有扩大生产模式或切换实验参数。

## 三个负载

复用上一轮冻结 FST、manifest、functional warmup 和 Q=1024。各自输出配置、
用户/内核人口和 CPI 分母一致；FST size/mtime 与冻结身份相同。before 为用户要求
修复前的生产模型（已含依赖、load/WB、服务校验），不是更早的三项修复前版本。

| Case | 修复前 CPI | 修复后 CPI | 修复前误差 | 修复后误差 | 绝对误差减少 |
|---|---:|---:|---:|---:|---:|
| Graph500 C8，正式 | 2.271196 | 1.745121 | +20.9521% | **−7.0640%** | +13.8881 pp |
| Stockfish C16，正式 | 0.788150 | 0.759041 | +17.3125% | **+12.9798%** | +4.3327 pp |
| TeaLeaf L1D64 C4，DSE | 0.507553 | 0.504608 | −17.3206% | **−17.8004%** | −0.4798 pp |

正式 case 沿用宏指令 CPI，DSE 沿用 cycles/user-UOP；分别与自己的冻结 gem5 参考
比较，没有混合原始 CPI。TeaLeaf 的已有负误差被扩大，不能声称三个负载都改善。
这是通用计算修复的有限验证，不是 held-out 或完整 formal40/DSE54 验收。

Graph500 总周期 110,795,730→85,132,186，减少25,663,544。与前轮只改入口的临时
诊断 −9.0910% 不同：正式实现还保留了真实资源阻塞投回 UOP 的下界、逐片反馈及
store send 的起点一致性。没有按 CPI 选择性删除这些规则。

| 目标计数变化 | Graph500 C8 | Stockfish C16 | TeaLeaf L1D64 C4 |
|---|---:|---:|---:|
| L1D misses | +60 | −10 | 0 |
| private L2 misses | +149 | 0 | 0 |
| LLC misses／DRAM reads | +1 | 0 | 0 |
| branch misses | 0 | 0 | 0 |

TeaLeaf 全部 scope PMU 完全一致。Graph500 另有 permission upgrades +101、remote
supplies +143；Stockfish 有 merged misses +61 等交错差异，完整字段保存在 summary。
这些是前后差分，不重新包装为 gem5 PMU 精度验收。

## Graph500 必要吞吐对照和原指令见证

固定 NUMA 0，构建及测试结束后串行运行 before/after/after/before，每个版本两次。
全部重复的目标 cycles/PMU 相同；这四次兼作精度对照，未额外重复精度运行。

| measurement 用户 UOP 吞吐 | 第一次 | 第二次 | 均值 |
|---|---:|---:|---:|
| 修复前 | 7.8309 M/s | 7.7523 M/s | 7.7916 M/s |
| 修复后 | 8.1717 M/s | 8.0628 M/s | 8.1172 M/s |

本轮均值提升 **4.18%**；每个版本仅两次，不能外推为所有负载的稳定性能收益。
Graph500 timing feedback calls 从14,475降至11,372；load/WB 仍实际触发：
load data-ready repairs=2,246,381，WB collision cycles=3,596,033。

最后对 Graph500 用户窗口做一次 generic 审计，其 per-core cycles、scope PMU、CHA
与快速内核一致。1000 UOP 内可见的 RAW、load response≤WB 和 WB width=8 检查通过。
原反例 core 0/sequence 8,940,543/PC `0x4093bb` 在重算后的上游状态中变为：

- UOP 基础 issue=6,611,235，反馈实际 issue=6,611,276；
- event 排序起点=6,611,367，请求=max(6,611,367,6,611,276)=6,611,367；
- L1 latency=2，response=completion=6,611,369。

此次排序下界覆盖了41-cycle反馈等待，没有再次相加。上游状态已随全程重算改变，
不能要求绝对时刻与前轮局部推导相等。

## 产物与剩余边界

[tmp/two-stage-request-origin-20260909](../tmp/two-stage-request-origin-20260909/) 保存
源码／二进制 before 快照和哈希、构建及测试日志、旧模型失败证据、输入、7次工作负载
命令和输出、`summary.json/csv`、`summarize.py` 及最终源码／二进制哈希。
7次为 Graph500 ABBA 4次、两个控制各1次、Graph500 generic 审计1次。控制运行可
并行，但没有使用它们的 wall time 作吞吐对照。未重采 FST、未运行 gem5 或全矩阵。

仍需后续解决功能排序代理的硬件含义、移动到达后的服务路径有效性，以及其他已有
误差。Graph500 仍低估7.06%，Stockfish 仍高估12.98%，TeaLeaf 仍低估17.80%；
不把本轮通过的机制测试等同于模型整体精度合格，也不以残差补偿抵消控制负载回归。
