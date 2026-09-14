# Cache 请求/返回与权限生命周期修复

日期：2026-09-09。接续 [下级内存事件审计](causal-memory-edge-audit-20260909.md)。

本轮已在实验 `core.model=causal_read` 路径修复三处已定位的问题：查询 L1 前固定的
网络等待、cache 请求/返回阶段混用，以及普通读 fill 不获得 E 权限。代码和目标参数
按事件归属接入，没有 workload/PC 分支、CPI 补偿或 reference 时刻输入。

构建与 `fastsim_tests` 通过；只新增一次原固定前缀模拟和一次同一 TeaLeaf 真实 ROI
模拟。DRAM 配置与上一轮诊断完全相同，未重新采集 gem5。维护 native-FS 默认未切换。

## 事件与权限变化

### 请求与返回分别推进

`CacheConfig` 新增仅用于 `causal_read` 的 `miss_request_latency` 和
`fill_response_latency`。原 `hit_latency` 仍表示本级 hit response 服务。
未指定 miss 参数时保留旧通用配置的 fallback；显式 0 是有效的事件边。
其他 core model 使用这些新参数会拒绝运行，防止配置被静默忽略。

下级 `finish_fill` 释放本级 generation/MSHR，再调度自己的返回服务及网络消息；
上级在消息实际到达时才填充并释放上级 generation。返回在途时的新 load 仍可合并到
上级 generation。`cache_response` 记录本级发送响应的时刻，LLC 的网络边在它之后。

[`causal-gem5-o3-prototype.cfg`](../configs/causal-gem5-o3-prototype.cfg) 的目标参数为：

| 层级 | hit response | miss request | fill response |
|---|---:|---:|---:|
| L1D | 1 | 3 = mandatory 1 + request 2 | 0 |
| private L2 | 2 | 2 | 2 |
| LLC | 2 | 2 | 1 |

依据本次采集配置与上一轮源码审计，SimpleNetwork 单程 4；LLC 发请求后到 memory
admission 的 directory 路径为 5；MemCtrl 响应后到 LLC fill 的路径为 6。
peer controller response 单独配置为 2。DRAM 继续使用上一轮配置的实际 333-tick
时钟域转换值；没有顺带改变其调度、参数或几何。

### 将行排序与协议消息分开

保留原有保守的读/写 line lease 排序，直到片段 callback 才释放。它防止年轻写入使
旧返回数据失效后，旧 fill 又恢复该副本。`coherence_acquire/grant` 现在只授予本地
排序 token，grant 同拍发生，不再附加一轮网络 request/grant。

真正的 GETS/GETX 从私有 cache miss 或权限升级的 generation 出发，在 LLC/片上目录
入口决定是否需要 peer snoop。无 peer 的普通 miss 直接继续 cache/DRAM 路径；peer
数据或失效确认返回后，该共享事务才能响应。没有把旧路径总延迟简单减掉 26 拍。

`write_intent` 与 dirty data 分开传播。L1 store 缺少写权限时，先保留数据并分配
权限 generation，不在 grant 前把 tag hit 标成 dirty。私有数据已经存在的升级请求
由共享端返回权限，不因为 LLC 没有数据副本就新增 DRAM 读取。升级仍占用私有 MSHR，
并保持 SQ/TSO 与 store response 生命周期。

### fill 返回权限，处理 E→M 与真正的降级

响应携带独占授权；没有其他核的副本或在途读时，普通读 fill 获得 E，随后本地 store
直接完成 E→M。共享读的 fill 不获得 E；其后写入需要失效 peer 并等响应。

独占响应在途期间，新读可能出现，因此在私有 fill 到达时重新核验是否还能获得 E。
复核中进一步发现：这个核验不能顺手清掉已有 private-L2 的脏所有权。例如本地 L2
返回 L1 时来了 peer read，如果提前擦除所有权，LLC 可能绕过 snoop 使用旧数据。
现在只有实际 snoop/invalidation/eviction 才撤销已有所有权；新 shared fill 仅表示
本次没有获得新的 E 授权。该交错场景有专门测试。

## 计数含义

- `coherence_transactions` 仍是全程 line lease 数，包括本地 hit，不等于网络事务数。
- `directory_requests` 是实际到达 **LLC/片上目录** 的共享请求数，不是 gem5 独立
  memory-directory controller 的接收计数。
- `permission_upgrades` 是 resident L1 数据因缺少写权限而创建的升级数；
  `exclusive_store_hits` 是本地已有独占权限的 store hit 数。
- 历史名 `miss_generations_l1_l2_llc` 记录数据或权限 generation；私有 resident
  upgrade 也占 MSHR，并额外发出 `permission_request`。不能把它等同 cache tag misses。
  每个 generation 仍有唯一 `fill`，按这对事件独立计算占用积分。

本轮 TeaLeaf 全程：277,649 个 lease、7,233 个共享请求、45,788 个本地独占 store
hit、0 个 resident 权限升级。跨核共享升级及脏数据转移在机制测试中验证，不能将这个
没有此类事务的 TeaLeaf 样本当作完整一致性精度证明。

## 最少必要验证

新增机制检查覆盖：

1. 使用独立固定 DRAM service 的冷读：admission 前后消息边与源码一致；只把 L2
   fill response 从 2 改为 9 时，DRAM admission 和 L2 fill 不动，上级返回与消费者
   发射恰好晚 7 拍；返回在途的新 load 合并到仍占用的 L1 MSHR。
2. 无共享者读后本地写不再请求目录；两个核共享读后写入必须升级和失效，且不新增
   DRAM 读；存在 older read 返回时，writer 继续遵守行排序，不能恢复失效旧数据。
3. 脏 owner 的 L2→L1 返回与 peer read 交错时，保留所有权直到实际 snoop，并发生
   必要的数据转移。原有 replacement/inclusion、RAW、TSO、queue/callback 测试通过。

首轮新增交错测试把两个核同拍的独立请求误设为“读必然先到”；实际公平调度可以先处理
store。修正 fixture，用一条执行 ALU 明确先后，并断言 store 在 read callback 前已
退休。未放松排序检查。该失败日志保留；最终整套测试通过。期间的增量构建用于修正
fixture 和脏所有权边界，没有重复矩阵或重采集。

原前缀 **14,560 UOP、27 条扩展 RAW 边**保持不变。复用既有独立审计器核验 RAW、
TSO、序列化、宽度、回调、ROB/IQ/LQ/SQ 与各级 MSHR/DRAM 占用积分，全部通过。
新增配对核验结果：

- **136 对 private-L2/L1 fill 全部相隔 2 cycles**，原来全部同拍。
- **1,802 次 acquire/grant 全部同拍**；争用仍可使 queue→acquire 等待。
- 上轮定位的 **17 次 resident store 均为本地 E→M**，hit→response 全部 1 cycle，
  没有原先固定的 26 拍 prelookup 等待，也没有转为权限请求。

前缀首个普通冷 load（core 0、ordinal 6）现在为：execute/L1 miss 5，private L2
miss 8，LLC/目录请求 14，DRAM admission 21，media ready 118，LLC fill 185，
private L2 fill 190，L1 fill/data 192。资源释放与消息到达各自发生，不是回填总完成时间。

## 同一真实 ROI 对照

共 **2,939,373 UOP**，包括 2,897,470 预热 UOP、40,000 用户 ROI UOP 和 1,903 内核
ROI UOP；83 条扩展依赖全部消费。原输入及附件 SHA、功能边界、人口/回调/fill 守恒
通过。前后 DRAM 配置完全相同；其余有效配置差异逐项验证为上述源码推导的 cache/
network 事件参数。修复前二进制也与上一轮诊断指纹匹配。

| 指标 | 修复前，上一轮 DRAM 参数对照 | 本轮 |
|---|---:|---:|
| 全程 L1/L2/LLC generations | 7,233 / 7,233 / 7,232 | 相同 |
| 全程 L1 MSHR 占用积分 | 1,569,158 | 1,255,706 |
| 全程 private L2 MSHR 占用积分 | 1,561,925 | 1,219,541 |
| 全程 LLC MSHR 占用积分 | 1,301,497 | 1,139,961 |
| 全程 DRAM 请求占用积分 | 599,993 | 604,793 |
| 全程 ROB dispatch 阻塞周期 | 789,310 | 557,225 |
| 全程 ROB 阻塞 episodes | 3,244 | 3,284 |
| ROI core 0 cycles | 1,528 | 1,528 |
| ROI core 1 cycles | 17,896 | 13,780 |
| ROI core 2 cycles | 4,962 | 4,397 |
| ROI core 3 cycles | 18,892 | 14,692 |
| ROI cycles / user UOP，诊断值 | 1.08195 | **0.859925** |

阻塞次数略增、阻塞时间减少并不矛盾；不能单看 episode 数判断修复方向。DRAM 参数
不变，但真实到达交错随 cache 时序改变，其占用积分略增也是重算事件的结果。
所有全程计数包括预热，不能直接对照 gem5 ROI 内的 stall 计数。

采集 reference 的加权诊断值为 **0.89255**，当前数值更接近它；但本轮仍不认定正式
CPI 精度改善。现有每核最后 warmup retirement 切点未证明等价于 gem5 测量时钟窗，
I-side/翻译仍理想化，FRFCFS/refresh/完整 write 时序、tick 相位、共享控制器/TBE
容量及 peer 消息路径还未完整实现，L1 Sequencer/mandatory queue 也未分别显式建模。
当前 peer 数据通过 LLC，行排序保守且部分资源
无界；它不是完整 Ruby MESI_Three_Level 的等价实现。

下一步应继续核验测量时钟窗与 DRAM/共享控制器调度的事件边界，尤其单请求 FCFS
callback 如何接入实际排队与选择事件；不要根据剩余 CPI 残差反调已核验参数。

## 文件与产物

源文件：`src/causal_read.cpp`、`include/fastsim/causal_read.hpp`；配置与统计接入位于
`include/fastsim/config.hpp`、`src/config.cpp`、`include/fastsim/types.hpp`、`src/main.cpp`；
机制检查位于 `tests/test_causal_read.cpp`。

[`tmp/causal-cache-permission-20260909/`](../tmp/causal-cache-permission-20260909/) 保存：
`before/`、前后指纹、构建/测试日志、`prefix/`、`roi-10k/`，以及 `audit.py` 和
`validation.json`。独立前缀审计器复用上一轮 `audit_prefix.py`，没有修改其断言。
没有运行其他 workload、额外 ROI 参数矩阵或新增任何设备/中断模型。
