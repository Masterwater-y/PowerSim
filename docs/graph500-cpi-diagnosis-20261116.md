# Graph500 C4/C8 CPI 误差诊断 — 2026-11-16

更新：下文是最初诊断，关于“重试是最强残差候选”的排序已被后续负面对照降级，不能作为已确认根因。完整 FU 日历和稀疏接收下界两轮尝试均未改善精度。最新的同起点服务时延、parent 返回和 L1 路径审计见 [后续服务时序诊断](graph500-service-cpi-diagnosis-20261116.md)。

后续讨论已汇总到 [功能流 load 接收、重试与资源反馈：诊断、设计和实施方案](spr-functional-load-admission-retry-design-20261116.md)，包括 gem5 重试与硬件重放的区别、有限队列/资源日历/事件唤醒模型、分阶段实施及精度和吞吐验收边界。该方案随后实施但失败并回退；见 [回退与稀疏候选验证](spr-sparse-load-pressure-validation-20261116.md)。

## 口径与身份

- 基线：当前 SPR-M4，功能 TLB 与 UART/APIC 服务均开启；ELF SHA-256 `d6f5bae930dfad9782a938080f1de5e647dc44a6fc967c89dbc6f44e4eb9c5f9`。未修改默认配置或可执行文件。
- CPI 是 workload-task active cycles / retired macro instructions；两侧使用相同退休终点策略 `retired-stream-completion-v1` 和逐核 task macro 人口。不是 elapsed CPI、UOP CPI 或逐核 CPI 的算术平均。
- 快速模式复用已保存的独立 BaseCPU task-clock 审计。本次三个机制对照及一个统计控制分别用维护入口重新评分，均为 2/2 CPI scored、failed/pending=0；不是严格 PMU 或硬件认证。
- 下述缓存计数来自当前来源保存的 Ruby/SLICC native summaries / task-pmu；没有使用 kernel_events 的 taotrace-path-class-v3 字段充当缓存真值。这些 raw 观察未重跑严格完整性审计，因此不作为新的正式 PMU 精度结果。
- 原始 stats.txt 的诊断窗口比正式退休终点略大：C4 core0 numCycles=7,516,475，正式 active=7,516,390；诊断统计只用于定位机制，不能与正式 CPI 周期逐项相加。

## 当前误差

| 核数 | gem5 CPI | FastSim CPI | 相对误差 | CPI 绝对误差 | 少预测 active cycles | 每内层迭代少预测周期 |
|---|---:|---:|---:|---:|---:|---:|
| 4 | 0.966422 | 0.839507 | -13.132% | 0.126915 | 3,948,362 | 0.889 |
| 8 | 0.969791 | 0.851812 | -12.165% | 0.117979 | 7,338,091 | 0.827 |

两例等权 CPI MAPE=12.649%，CPI MAE=0.122447 cycles/macro；不是 WAPE。相对误差=(FastSim−gem5)/gem5，绝对误差=abs(FastSim−gem5)。

| case/core | gem5 active cycles | FastSim active cycles | 周期缺口 | 同口径 macro 数 |
|---|---:|---:|---:|---:|
| c04-854.graph500_s/0 | 7,516,390 | 6,496,964 | 1,019,426 | 7,769,554 |
| c04-854.graph500_s/1 | 7,516,410 | 6,517,570 | 998,840 | 7,775,437 |
| c04-854.graph500_s/2 | 7,516,411 | 6,542,343 | 974,068 | 7,783,948 |
| c04-854.graph500_s/3 | 7,516,398 | 6,560,370 | 956,028 | 7,781,279 |
| c08-854.graph500_s/0 | 7,539,938 | 6,579,647 | 960,291 | 7,766,960 |
| c08-854.graph500_s/1 | 7,539,934 | 6,590,243 | 949,691 | 7,782,269 |
| c08-854.graph500_s/2 | 7,539,954 | 6,616,965 | 922,989 | 7,783,250 |
| c08-854.graph500_s/3 | 7,539,827 | 6,599,418 | 940,409 | 7,780,839 |
| c08-854.graph500_s/4 | 7,539,912 | 6,627,035 | 912,877 | 7,776,693 |
| c08-854.graph500_s/5 | 7,539,902 | 6,620,863 | 919,039 | 7,759,759 |
| c08-854.graph500_s/6 | 7,539,948 | 6,655,854 | 884,094 | 7,775,978 |
| c08-854.graph500_s/7 | 7,539,946 | 6,691,245 | 848,701 | 7,772,549 |

各核都低估，不是单个 housekeeping 核异常。C8 相对误差略小并不代表根因消失。

## 实际热点是 APSP 标量循环

本批命令为 `-m 19384 -p 16000 -q apsp_NYroad_19384.bin -g 1`。C4/C8 同一 marked ELF SHA-256 为 `4e3e00100c8797e29f39e9d4f89ef9780bd2a68e03df3a23fd4d47df9a0feb8c`。不要把本窗口按一般 Graph500 BFS/原子更新来解释。

core0 的用户 UOP 全部落在 `run_apsp._omp_fn.0`；C4 用户 UOP=9,978,448，C8=9,975,125。主循环通常为 7 个宏指令、9 个功能 UOP、3 个 load。C4 core0 循环约 1,108,662 轮，条件 store 仅执行 4 次；两例 core0 都没有原子 UOP。

```asm
405c60  mov (%rdi,%rax), %edx     # 流式读取 A[j]
405c63  add (%rsi), %edx          # 读取本轮不变标量；load + add
405c65  cmp (%rcx,%rax), %edx     # 流式读取 B[j]；load + compare
405c68  jge 405c6d
405c6a  mov %edx,(%rcx,%rax)      # 本窗口极少执行
405c6d  add $4,%rax
405c71  cmp %rbx,%rax
405c74  jne 405c60
```

这意味着大量同一 cache line 的相邻 load 与返回前的请求合并。单看 miss 数不能判断这些 load 的等待位置、队列占用或有效 MLP。C4/C8 的整体周期缺口相当于每内层迭代约 0.89/0.83 周期；这是摊销结果，不是发现了固定一拍的缺失边。

## 排除与降级的解释

1. **UART/OS 后台不是主因。** 两例 UART 请求=0；APIC 请求分别 12/24。没有测量期缺页或 syscall。已记录 IRQ 总开销分别 79,572 / 132,725 周期，仅为残差约 2.02%/1.81%，且模型本来就在执行相应内核流；不能把这些周期全部当作可回补缺口。
2. **功能 TLB 修复已开启，但对本窗口影响很小。** IO OFF 下 TLB old→functional：C4 CPI 0.837365→0.837905，C8 0.848734→0.848402；不能据此关闭机制正确的 TLB，旧误差补偿也不等于正确。
3. **遗漏大量缓存 miss 的证据不足。** 当前保存的原生计数与模型值非常接近，见下表。未排除 miss 延迟、返回合并和并行度的错误。
4. **分支不是主要解释。** 退休分支人口两侧一致；原生 miss C4=873、C8=1493，模型=1027、1713。模型也没有少预测大量 miss。恢复开关对照仍有约 11%–13% 残差。
5. **不存在“原子锁竞争”的本窗口证据。** 用户热点没有原子操作，条件 store 也极少。

| case | L1D tag miss 原生 / 模型 | private L2 tag miss 原生 / 模型 | LLC tag miss 原生 / 模型 |
|---|---:|---:|---:|
| c04-854.graph500_s | 556,493 / 556,607 | 277,711 / 277,734 | 277,636 / 277,645 |
| c08-854.graph500_s | 1,112,489 / 1,112,709 | 555,240 / 555,279 | 555,086 / 555,097 |

以上为来源明确的保存计数诊断，不是本次新认证的 PMU 准确率。

## 最强候选：拒绝、重试、发射资源与反压的耦合

| core0 原生诊断 | C4 | C8 |
|---|---:|---:|
| 退休 load | 3,329,256 | 3,328,114 |
| MemRead 发射次数 | 10,046,943 | 10,246,818 |
| cache 拒绝计数 | 561,427 | 569,741 |
| bank 冲突计数 | 3,167 | 3,035 |
| IQ full 事件 | 6,341,592 | 6,359,705 |
| 前端被后端阻塞周期 | 6,563,585 | 6,587,448 |
| 首次 load 发射至完成，平均周期 | 129.691 | 130.100 |

gem5 的 MemRead 发射次数约为退休 load 的 3.02/3.08 倍。两例均存在显著 load 重发；窗口/错路执行会影响这个倍率的精确分解，但仅 80–85 周期的窗口尾差和少量退休 branch miss 不能自然解释数百万次额外发射。现有统计没有按首次、cache-block 重试、bank/port 重试分别守恒拆分，所以不能把全部额外发射都算成某一类拒绝。

原生源码链路：
- `gem5-spr/src/mem/ruby/system/Sequencer.cc`：达到 16 个 outstanding 后可返回 BufferFull；同线 alias 成员也递增 outstanding，alias 不等于免费接收。
- `gem5-spr/src/cpu/o3/lsq_unit.cc:1805`：检查 cache blocked、load port 与 bank，失败后进入 retry/block 路径。
- `gem5-spr/src/cpu/o3/inst_queue.cc:902`、`:1324`：重试请求回到 ready memory 队列，再经过 FU 调度。等待并没有消失，且重试会消费发射资源。

FastSim 当前已建模 16 个 Sequencer 槽、分区 IQ、返回依赖与合并服务；不能说“没有队列模型”。源码 `src/simulator.cpp:16510` 在下一次可接受时刻安排 admission，在 `:16976` 以返回时间释放槽位，在 `:17331` 更新 IQ 释放。差别在于：
- 功能流中一个 load 对应的发射/访存事件，没有原生失败尝试的逐次 FU 资源占用。
- 第一阶段的共享 FU 日历根据下界执行；第二阶段移动依赖/访存时刻时，默认 `response_sparse_resource_repair=false`，没有启用通用 FU 重排修复。不能据此直接断言已发生超额发射，但需要审计被移动的 load/ALU/branch 是否在同一周期争用同一端口。
- 源码已有 IQ 与 Sequencer 等等待反馈，缺口更可能出在反馈的时刻和资源耦合，而非一个容量参数忘记打开。

C4 纯统计对照中，FastSim core0 load 的“修正后 issue→completion”均值约 21.56 周期，而 gem5 的“首次 issue→completion”约 129.69。**这两个起点不同，不能报告为 load 延迟低估 6 倍**：FastSim 已把不少等待移到 issue 之前（fetch→修正 issue 约 99.86 周期）。这恰好说明需要追踪等待放在哪里、期间占哪些资源，而非把 129.69 直接写成固定 load latency。

## 同输入机制对照

所有成功对照使用同一 ELF、相同来源和逐核 macro 人口；仅改变所列配置。参数延迟变化只作诊断，不作为拟合修复。

| case | 对照 | FastSim CPI | 相对误差 | CPI 绝对误差 |
|---|---|---:|---:|---:|
| c04-854.graph500_s | 当前默认 | 0.839507 | -13.132% | 0.126915 |
| c04-854.graph500_s | issue_to_execute: 0→1（诊断） | 0.828014 | -14.322% | 0.138408 |
| c04-854.graph500_s | response_branch_recovery: false→true | 0.838783 | -13.207% | 0.127639 |
| c04-854.graph500_s | spr_load_bank_feedback: false→true | 0.819777 | -15.174% | 0.146645 |
| c08-854.graph500_s | 当前默认 | 0.851812 | -12.165% | 0.117979 |
| c08-854.graph500_s | issue_to_execute: 0→1（诊断） | 0.842965 | -13.078% | 0.126826 |
| c08-854.graph500_s | response_branch_recovery: false→true | 0.859060 | -11.418% | 0.110731 |
| c08-854.graph500_s | spr_load_bank_feedback: false→true | 0.829030 | -14.515% | 0.140761 |

增加发射延迟、增加 bank/port 等待均可能让最终预测更快：它们改变到达顺序、批次与重叠关系，不能当作可线性相加的独立损失项。bank 对照更差，且原生 bank 冲突仅约 3000 次/core，不能声称打开 bank 即修复 Graph500。分支恢复在 C8 改善约 0.00725 CPI，但仍低估 11.418%；C4 略变差。

另外两个历史实验路径被配置校验器明确拒绝：private read services 与 v2-wait 冲突；legacy ROB/LSQ feedback 与 sparse scoreboard 冲突。没有绕过检查，也没有将拒绝运行计作验证。统计模式须关闭 materialized fast kernel；C4/C8 统计控制的完整 time_accounting 均与默认完全一致后，才使用其细分计数。

## 后续修复方向及未解决边界

优先做 bounded admission/retry resource feedback：用功能 load 身份、地址、当前模型的槽位/返回事件，估计拒绝期和重试批次；通过小型资源日历约束 load 发射及后续依赖和 IQ 占用。可以聚合连续阻塞窗口，保留单遍、有界状态，不要求逐周期流水线或回放 gem5 时序。内部重试只表示同一退休 load 的资源竞争，不生成错路访存，也不扩充退休人口。

验收前先增加两个守恒审计：①首次 load 与重试资源预约分开；②第二阶段移动后，按端口/周期核对预约与 IQ 释放的容量约束。再用 C4/C8 Graph500 与其他当前负载共同验证 CPI、PMU 和独立吞吐，避免只修好一个负载。

尚未完成：原生重试原因的精确分账、默认排程是否真的超额占端口的计数证明、同参数修复后的 CPI 因果对照。故当前给出的是证据最强的定位与修复优先级，不是“全部残差已解释”或已完成精度修复。

## 复查入口

- 逐核数值、原生计数、对照配置差异：[evidence.json](../tmp/graph500-cpi-diagnosis-20261116/evidence.json)
- 功能热点：[functional-hotspots.json](../tmp/graph500-cpi-diagnosis-20261116/functional-hotspots.json)
- 对照 quick 评分：[scoring](../tmp/graph500-cpi-diagnosis-20261116/scoring/)
- 同输入默认矩阵：[M4 report](../tmp/device-io-c4-c8-quick-20261116/report.md)
