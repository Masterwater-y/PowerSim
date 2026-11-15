# Marian store 生命周期观察（2026-11-06）

## 范围和结论

复用四案的 Marian C4 FST、同一个 FastSim 冻结二进制和预取开启配置，直接观察 FastSim 现有 legacy buffer TSO 的最终接受 store 记录。审计要求 `sim.cpi_attribution=true`、关闭 materialized fast kernel；并且与本轮 `spr_load_bank_feedback` 互斥，因此观测和控制均关闭 load bank。其余选项、输入、线程计划、FR-FCFS 和预取保持一致。两次并发运行，仅用于机制观察，不用于吞吐评价。

观测与控制的功能身份、各核周期、完整 time-accounting、退休 PMU（含退休 cache）、预取计数及已比较的缓存请求计数一致；模型 CPI 均为 2.742719238，24,463,924 条 workload-task 退休宏指令。原四案 load-bank 开启的预取 ON CPI 为 2.733326837，故本观测不应直接视作原配置的逐事件执行。gem5 活动 CPI 参考 1.428304347；当前观测 CPI 绝对差为 1.314414891 cycles/macro，参考窗口起点仍非严格一致。

可确认：在这个模型配置中，许多 store 的发送时间远晚于其退休后变得可发送的时间；前序未释放时四行窗口也经常允许后续 store 提前发送。尚不能推断这些等待中有多少暴露到退休关键路径，更不能将其总和直接归因给 CPI。

## 测量期 FastSim 逐条记录

`diagnostics.store_lifecycle_path` 的既有 v1 格式输出最终接受的 regular、非 local store UOP，包含四核合计 4,660,747 条，其中 workload-task 4,387,961 条；记录按自己的 phase 标记筛选，0 条违反 dispatch ≤ issue ≤ retire ≤ eligible ≤ send ≤ SQ release。各核人数分别为 1,115,877、1,263,387、1,153,917、1,127,566，与各核 `response_store_uops` 相等；`send→release` 时长和与各核 `response_store_send_to_response_cycles` 相等。这是 UOP 而非源程序 store 宏指令、cache line 请求或 gem5 Sequencer ST 人口。

| 观测阶段（全部 4,660,747 条） | 平均模型周期/条 | 解释 |
|---|---:|---|
| dispatch → retire | 54.67 | 逐条时间间隔，可相互重叠 |
| retire → eligible | 2.00 | 本配置的模型准入延迟 |
| eligible → send | 881.75 | 含 store 窗口/顺序及其他模型约束的队列等待 |
| send → SQ release | 14.83 | 模型服务和释放间隔，不能直接等同原生协议 callback |
| retire → SQ release | 898.58 | 单条驻留时间，不是独占流水线停顿 |

`eligible→send` 各核中位数为 186/0/186/1 cycles，P90 为 2062/1860/2164/1998，P99 为 8198/6026/7618/6694；四核顺序为 0/1/2/3。其合计约 41.10 亿条目周期，store 等待大量重叠，**不能与 CPI 周期直接相加或相除**。

3,826,422 条（82.10%）在 eligible 时，模型保存的此前 store 最晚释放水位仍在未来；其中 2,801,884 条（73.22%）在该水位之前已发送，1,024,538 条在该水位或之后发送。这里的水位是模型 `store_drain_ready`，并非已证明的 gem5 前驱身份或独立硬件事务。

仅对测量期标记为 task 的 store，把每核 `send→release` 完整区间求并集后跨核求和：区间时长和 67,697,062 core-cycles，并集 62,567,096 core-cycles，两者差 5,129,966 core-cycles。它衡量所选区间的重叠计数，并集没有裁剪到共同 ROI 边界；不能当作关键路径停顿或 native 时间。

## 与 gem5 的边界

同次采集的 `stats.txt` 有四核 store buffer offload 共 2,622,657、merged 共 400,373；这些为原生统计 dump 中的全域计数，测量窗口、核/任务人口及一次合并与 FastSim UOP 的映射未证明一致，只表明原生存在实质的合并，不可直接与上述记录相减。

同一个原生 dump 中，四核 Sequencer 的 `storePrepare.accepted` 分别为 1,064,576、886,092、626,706、640,638，`dropped` 分别为 305、256、309、210；这些是原生 prepare 提示请求计数，并非与模型 store UOP 一一对应的配对数据。它证明此 dump 中 prepare 活动存在，不能证明某个特定模型 store 的提前量。

当前 legacy 路径以一次 hierarchy 访问近似 prepare 并结合模型 `memory_response_cycle` 计算后续 SQ release；审计中 `prepare`/`permission` 的 0 是该路径未导出独立边，不是 prepare 没发生或延迟为零。协议级独立 callback 亦不可用，不能把 `send→SQ release` 伪称为 gem5 `Sequencer admission→callback`。原生逐 store 的 prepare、队头请求和 callback 尚未同成员配对，所以根因、CPI 可消除量仍未闭合。

下一步聚焦：确认同一条 store 或 cache-line 的功能身份与生命周期窗口，分离 prepare 提前获得权限、队头实际发送、callback 和 SQ 释放；在不引入 OoO 时序作为输入的前提下，用原生仅作离线参考。无需按 workload 调参数或把累计等待直接加到 CPI。

产物：`tmp/marian-store-observe-20261106/{control.json,result.json,stores.bin,analysis.json,analyze.py}`；观测与控制同用 `tmp/spr-resident-four-repaired-20261106/fastsim`。本次未重采 gem5，也未完成原生逐成员生命周期关联。
