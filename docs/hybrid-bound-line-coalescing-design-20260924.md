# 混合式 line coalescing：程序序时间戳消环 + 精确 weave 保精度

日期：2026-09-24。状态：**历史提案；首轮具体设计与实施见
[固定读服务关系](hybrid-fixed-read-services-round1-20260924.md)。**
下文保留最初的架构分析；“精确 weave 保精度”、重复 miss 为主要误差来源等判断是
待验证假设，不是保证。当前方案固定一次服务关系，再传播竞争后的响应，尚未实现
通用的零竞争 bound 接口。PMU 5% 是观察目标，不设置精度准入或自动回退门槛。
前置：[buffer TSO store timing](buffer-tso-store-timing-20260923.md)、
[MSHR 首轮验证](buffer-tso-mshr-validation-20260923.md)、
[修复方案选项](buffer-tso-prepare-throughput-fix-options-20260924.md)、
[Sniper/ZSim 对比](sniper-zsim-fs-trace-driven-comparison.md)。

本文记录两件事：(1) FastSim 现状与 zsim 的架构取舍分析；(2) 一个只借"无环前向"思想、
保住 FastSim 精度资产的混合式方案。本文不构成对 zsim 代码的复制，亦不引入按负载拟合的参数。

---

## 1. 问题回顾：buffer 路径的环

- FastSim 成批读入一段 trace，在**按模拟时间排序的 weave**里一次性做准入、逐行合并、资源竞争。
- 判断 follower B 是否合并到父 A，需要"A 的响应时刻 / A、B 的到达先后"，而这两者正是同一趟 weave 自己要算的输出——**决策依赖自身输出，自指成环**。
- **修正 serial 归因：** `core.tso_store=serial` 在配置中定义为单个在途 store，
  不是“所有 load miss 都停住整个核心”。不能据模式名推断 follower 必在 fill 后，
  也不能推断每个地址天然只产生一次 miss。serial 历史精度较好是观察结果，机制仍需配对证据。
- buffer-114 允许多个 store 并行准备写权限、FIFO 发布，改变请求重叠与资源压力；
  同行读服务是否在途的判定可能与竞争后的时间互相依赖，但不能把所有差异都归为 load MLP。

历史上 5 轮尝试均未拿到净收益（详见 §5 与各引用文档）：config-only 复用旧多趟路径不生效；
单趟直接接线把 CPI 从 −18.7% 推到 +125.5%（逐条裸加等待）；受限服务引用收益≈0；
强制不回退与局部回退均无精度收益且损失吞吐。

---

## 2. zsim 是怎么解开这一环的

基于 zsim 源码实查（`ooo_core.cpp`、`filter_cache.h`、`contention_sim.*`、`ddr_mem.cpp`、`memory_hierarchy.h`）。

### 2.1 每行一个数据可用时间戳
- 核内直接映射的 L0 FilterCache，每表项存 `rdAddr/wrAddr/availCycle`（本行数据在哪个周期可用）。
- load 同行命中 filter 项：`return MAX(curCycle, availCycle)`（filter_cache.h:108），从不问"父 miss 在不在飞"。

### 2.2 合并是"读"出来的，不是"判"出来的
- 核心按 trace **程序序**逐条算 uop，时间只增不减。
- 第 1 个 load 行不在 → 发 miss，下层返回 respCycle（如 200），写入 `availCycle=200`。
- 程序序后面的同行 follower 再查该行，filter 项仍在 → 直接读到 200，不再发请求。没有 `if (should_coalesce)` 分支。
- 所有 follower 与父在同一刻完成，stall 天然只算一次（MAX 折叠），不会逐条相加。

### 2.3 为什么无环
程序序由 trace 固定；时间戳是单调标量；follower 只依赖**程序序上已算完的前驱状态**，不依赖本趟自己后面的输出。决策不再自指，一趟即可。

### 2.4 bound 接口
每个内存部件都实现同一同步方法（memory_hierarchy.h:123）：

```cpp
class MemObject {
    virtual uint64_t access(MemReq& req) = 0;  // 入：到达周期；出：可用周期
};
```

- 部件内部只维护"资源下一次何时空闲"，前向标量计算：
  `resp = MAX(req.cycle, nextEligibleCycle) + latency; nextEligibleCycle = resp;`
- 给出无竞争服务时间下界，故称 **bound**。
- 一次 `access` 同时产出两样：(1) 返回 respCycle（供程序序行时间戳与合并）；
  (2) 向 EventRecorder push 一条 TimingRecord（ddr_mem.cpp:275），稍后由 **weave** 阶段
  ContentionSim 事件队列按真实时间执行，只解 bank/channel/bus 资源竞争。
- 即 **bound（程序序、无环、给延迟+埋事件）+ weave（事件队列、只解竞争）**，全局时间排序只用于域间 crossing 和资源竞争，不用于决定逐行合并。

---

## 3. FastSim 现状 vs zsim 的优劣

| | FastSim 现状 | zsim |
|---|---|---|
| 定时 | 成批 time-sorted weave 一次性定时 | bound 程序序前向 + weave 只解竞争 |
| 逐行合并 | 与定时耦合，同趟判 → 自指成环 | 程序序行时间戳读出，无环 |
| 优势 | 严格事件序、复杂协议交错更直接；实测尾部精度高 | 无环、可扩展上千核、吞吐高 |
| 劣势 | 环导致欠合并或多趟；phase-2 weave 占约 91%、难扩展 | bound 是局部下界，内存尾/复杂协议系统性低估 |

- FastSim 在已对齐矩阵上 CPI MAE 0.058；对照文档显示其 MAE 比 zsim 低约 6.9×，尾部优势明显。
- zsim 中位吞吐 8–9M uops/s，赢在核数与吞吐；FastSim 赢在精度。两者是不同的精度/吞吐点，不存在全面碾压。

---

## 4. 版权 / 学术规范 / 新颖性

改成"bound 式"思想本身不构成抄袭，但三件事必须分开：

1. **版权（GPL v2）**：版权保护代码表达，不保护思想。必须 clean-room：不复制 zsim 源码/头文件/强相似实现，
   只依据论文与公开接口描述独立实现，避免 GPL 传染。
2. **学术抄袭**：bound/资源占用时刻前向定时是远早于 zsim 的通用排队/interval 技术，非其首创；
   义务是在论文显式引用 zsim（Sanchez & Kozyrakis, ISCA 2013）及 interval/排队模型前作。
3. **新颖性**：不能做成"复刻 zsim"。主张落在 FastSim 差异点——bound 前向与高精度守恒门的结合、
   精度接近 FastSim 现状而吞吐接近 zsim 的混合点、在 SPR/buffer-TSO 上做到 zsim bound 做不到的尾部精度。

---

## 5. 混合式方案（提案）

**核心：只借"无环前向"这一通用思想消环，保留 FastSim 已验证的精确 weave 作为竞争求解与基准。**

### 5.1 阶段划分
1. **程序序 pass（便宜，对应现有约 9% 的阶段 1）**
   - 行状态仅保留时间戳：`line_state { data_avail_cycle, owner_request_id, present }`，按每核程序序扫 load/store。
   - 行不在 → 向内存系统发行一次 miss，拿到 respCycle，写 `data_avail_cycle`。
   - 同行 follower 到达时刻 < data_avail_cycle → 不发行，完成时刻取
     `MAX(dispatch, data_avail_cycle)`，与父共享同一完成事件；follower stall 与父 miss 取 max 折叠，**一次阻塞**。
     - 同时修掉 8.6 的"逐条裸加"（CPI 过冲到 1.08）与 PMU 的"N 次 miss"。
2. **phase-2 weave（精确）**
   - 只在已去重的 miss 流上运行，负责跨行/跨核的 bank、channel、目录、coherence 竞争；不再做逐行合并判断。
   - 内存部件需提供 bound 式接口（传入到达周期、同步返回可用周期），由 weave 结果回填修正。
3. **状态一致性**
   - fill/invalidate 更新时间戳；buffer 权限回滚与 page-walk 回滚时连同行状态一起快照/恢复
     （复用 8.6 已验证的回滚机制）。

### 5.2 与 zsim 的明确边界
- 不复制 zsim 实现；行状态、weave、守恒门均沿用 FastSim 自有代码独立实现。
- weave 仍是 FastSim 的精确竞争模型与高精度来源，不退化为 zsim 的纯事件 bound。
- 论文引用 zsim 及 interval/排队前作，主张"高精度守恒门 + 高吞吐"的联合点。

### 5.3 验证计划（先反例，后全矩阵）
1. 先在 **NAMD c04（CPI −18.7% 反例）、lbm c04 与 lbm c32（PMU ~300% 反例）** 做最小验证。
2. 观察项：follower 最终响应、CPI 相对及绝对误差、PMU 分事件误差、吞吐与
   late-reference／回退覆盖。服务合并不等于全 ROI 每个地址只能有一次 miss；
   必须区分 generation、替换、失效和真实重复事务。
3. 首轮报告后再决定全 38-case 矩阵的安排；5% 不是扩展实验的准入门槛。
   来源、人口及守恒检查用于保证比较有效，不是精度阈值。

---

## 6. 未决问题

- 将现有 FR-FCFS/共享系统包装成 bound 接口的具体改动面与 bound-vs-精确 weave 的误差量级，尚未量化。
- 行时间戳在跨 epoch、RAW 顺序反转、incoming 服务仍在飞三种形状下的有效性需在反例中重点验证。
- 本文不代表机制已被证明；首轮以默认关闭开关实施，保留生产默认与旧路径。
