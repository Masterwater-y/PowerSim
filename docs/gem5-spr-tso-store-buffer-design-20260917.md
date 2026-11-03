# gem5 x86 TSO 与 Intel Sapphire Rapids store-buffer 对齐设计

日期：2026-09-17。

状态：**第一版已应用到生产 `gem5-fs`/`TCSim` 工作树，通过完整编译、serial
回退、单核 store-stream、双核 Store→Store、fresh KVM buffer-114 checkpoint
以及真实 FS serial/buffer identity 定向验证；生产默认仍为 serial，尚未声称与
SPR 周期级等价。**
本文中的 `SPR-like` 只表示采用与现代 Intel x86 相符的“有序 store buffer +
并行权限获取”机制。公开微架构资料通常将 Golden Cove 的 store buffer 报为
**114 entries**，Sapphire Rapids 使用 Golden Cove server core，因此本文把 114
作为 SPR profile 的建议初值。Intel 官方优化手册确认 Golden Cove 显著扩大了
store buffer，但没有把 114 项及完整内部仲裁、合并规则和逐周期行为定义成架构
合同；该数值仍须由目标机测量校准，不能视为所有 SPR stepping/SKU 的官方保证。

## 1. 结论

1. 当前 gem5 O3 的 `needsTSO=true` **不是把所有内存操作都串行化**。load 仍可执行
   store-to-load forwarding，也可在满足依赖条件时越过地址不同的旧 store。真正
   过度保守的是普通 committed store 的 Ruby 发送路径：每核最多只有一个普通
   store 请求在途，必须等它收到完整响应后，下一条 store 才能发送。
2. Intel Sapphire Rapids 同样实现 x86-TSO 的架构可见语义，但真实处理器使用
   多项 store buffer，将退休与缓存/一致性服务解耦，并可重叠多个 store 的内部
   准备工作。两者的 **ISA 内存序目标相同，微架构并发实现不同**。
3. 可以给 gem5 增加用户配置选项，但不能把 `storeInFlight` 从布尔量直接改成
   `N` 个真实 write 请求。Ruby 在 `writeCallback()` 中写入 cache line；跨 line
   请求可能乱序返回，年轻 store 可能先对其他核可见，从而违反普通 WB 内存上的
   Store→Store 顺序。
4. 已实现一个默认关闭、向后兼容的 `buffer` 模式：多个年轻 store 可
   并行获取写权限，真正的数据发布仍按程序顺序进行。该机制可称 `SPR-like`，
   但只有经过目标机校准和 litmus 验证后，才能称为 SPR profile。

## 2. 当前 gem5 的实际行为

本地生产 gem5 为 `/data00/yinhaolang/gem5-fs`，版本基线
`v25.1.0.1@c8222cc`，工作树含现有 TaoTrace/Ruby 修改。

### 2.1 配置入口

- `src/cpu/o3/BaseO3CPU.py` 定义 `needsTSO`，默认 `false`。
- `src/arch/x86/X86CPU.py` 的 `X86O3CPU` 将 `needsTSO` 固定为 `true`。
- 当前目标同时使用 `SQEntries=32`。SQ 可以保存多条 store，但这不等于有多条
  普通 store 已发送到 Ruby。

### 2.2 单普通 store 在途

`src/cpu/o3/lsq_unit.cc::writebackStores()` 的发送条件包含：

```text
(!needsTSO) || (!storeInFlight)
```

成功发送后，`storePostSend()` 在 `needsTSO` 模式下设置
`storeInFlight=true`；只有 `completeStore()` 收到该 store 的完整响应后才清零。
因此普通 store miss/upgrade 的整个服务时延都会阻止下一条普通 store 发往 Ruby。

这与现有 LBM native ledger 一致：测量窗口中的普通 Ruby store 服务没有重叠，
单 store 链占用率约 95%。证据见
[`lbm-common-end-error-audit-20260912.md`](lbm-common-end-error-audit-20260912.md)。

### 2.3 gem5 已保留的 TSO 行为

- store 在提交后才由 `commitStores()` 标记为 `canWB`；
- SQ 按年龄维护，load 会搜索旧 store 并进行同地址 forwarding；
- x86 snoop 冲突路径在 `needsTSO` 下保守地重放后续 load；
- release、LL/SC、locked RMW、barrier 和 strictly ordered access 有独立约束；
- Store→Load 到不同地址的放宽不是由关闭 `needsTSO` 才能获得。

所以 `needsTSO=false` 不是 SPR 模式，而是同时撤销部分 x86-TSO 约束的诊断开关。

## 3. 与 Sapphire Rapids 的差异

Intel 公开定义的是 x86 可观察内存序，而不是 SPR store-buffer 的完整 RTL：

- 普通 WB store 不应与其他 store 重排为对外可见；
- load 可以在满足 forwarding/冲突检查时越过更老、地址不同且尚未全局可见的 store；
- fence、locked operation、UC/WC/NT 等路径有额外规则；
- store buffer 允许核心继续执行，而不必等待每条 store 完成整个缓存层级服务。

Intel 对 Golden Cove（SPR 所用核心）的公开优化材料说明其扩大了 ROB、load
buffer、store buffer 等结构以增加乱序窗口和内存并行度。公开微架构汇总及研究
论文普遍使用 **114-entry store buffer** 描述 Golden Cove/Alder Lake；这也是
SPR-like profile 当前最合理的初值。但 Intel 官方文档没有把 114 作为 SPR 的
架构可见保证，也没有公开足以复刻所有 SPR 内部状态机的合同。当前宿主机是 Intel
Xeon Platinum 8457C，后续可以在同机上做延迟、吞吐和 buffer-full 校准；114、
RFO 并发数和 drain width 都必须保留为可配置参数，而不是写死在通用 O3 模型中。

因此需要区分：

| 层次 | 当前 gem5 | SPR-like 目标 |
|---|---|---|
| ISA 内存序 | `needsTSO=true`，目标为 x86-TSO | 继续保持 x86-TSO |
| retired store 缓冲 | SQ 保存多条 store | 有界 FIFO store buffer |
| 普通 store Ruby 服务 | 最多一条完整请求在途 | 多条权限/RFO 准备可在途 |
| 数据对外可见 | 前一条响应后才发下一条，天然有序 | 数据仍按程序序发布 |
| 同地址年轻 load | SQ forwarding | 继续从最年轻匹配旧 store forwarding |
| fence/locked/UC | 现有特殊路径 | 必须 drain 或走保守串行路径 |

## 4. 不可接受的最小补丁

以下改法看似直接，但不能标为 TSO 或 SPR-like：

```text
storeInFlight: bool -> outstandingStores: unsigned
发送条件: outstandingStores < N
```

原因是 Ruby `Sequencer::hitCallback()` 在 write callback 时执行
`data.setData(pkt)`。如果老 store 的 GETX/upgrade 很慢，而年轻 store 命中本核 M
line，年轻 store 可先更新 cache line，并被其他核观察到。仅让 completion callback
按序返回也不够，因为数据和 coherence state 可能已经在 callback 前后改变。

这种实现可以作为显式命名的 relaxed/diagnostic 模式，用于估算“单在途规则”的
性能上界，但不得用于 x86 正确性、SPR 对齐或正式 oracle。

## 5. 推荐用户接口

保持 `needsTSO` 只表达 ISA 内存序，新增独立的微架构模式。用户接口固定为：

```text
--tso-store=serial
--tso-store=buffer
--tso-store-buffer-size=114
```

语义如下：

- `--tso-store=serial`：当前行为，也是默认值；必须走原有
  `needsTSO/storeInFlight` 代码路径。
- `--tso-store=buffer`：SPR-like 机制；只改变微架构并发，不关闭
  `needsTSO`。
- `--tso-store-buffer-size=N`：buffer 模式可分配的 store/SQ entry 数。SPR
  profile 默认 `N=114`，用户可覆盖为其他正整数。

现有 gem5 O3 的 `SQEntries` 同时承担 speculative store queue 和 committed
store-buffer 容量。第一版不应再维护一个与 SQ 重叠、语义含糊的第二容量：

- buffer 模式下，CLI 将 `--tso-store-buffer-size=N` 映射为有效
  `SQEntries=N`；
- serial 模式下，不读取该值来修改 CPU，继续使用原 profile 的 `SQEntries`
  （当前基线为 32）；
- 如果命令行保留 `--tso-store-buffer-size=114`，只把
  `--tso-store=buffer` 改成 `--tso-store=serial`，容量参数变为 inactive，
  不报错也不修改 `SQEntries`，从而实现一键回退；
- 输出中同时记录 requested 和 effective 值，serial 模式应显示
  `requested=114, active=false, effectiveSQEntries=32`，避免静默歧义。

其余资源仍独立配置：

- `cacheStorePorts`：每周期可尝试的 store-side 端口数；
- Ruby Sequencer/TBE 容量：缓存层级可接受的并发；
- buffer 模式的实际 permission/RFO 并发受 store/SQ 容量、store port、
  optional prefetch queue 和 TBE/网络资源共同限制。第一版的内部权限请求不占
  Sequencer retired-demand request table；若以后需要单独限制 prepare 并发，
  再增加高级参数。

不要提供一个名为 `needsTSO=spr` 的多义开关，也不要让选择
`buffer` 自动修改 cache、ROB、频率或 Ruby 容量。它只启用新机制并采用显式的
store-buffer size。

SimObject 内部建议使用枚举，而不是字符串散落在 C++ 中：

```python
class TSOStore(Enum):
    vals = ["serial", "buffer"]

tsoStore = Param.TSOStore(
    "serial",
    "Committed-store implementation used when needsTSO is true",
)
tsoStoreBufferSize = Param.Unsigned(
    114,
    "Store-buffer entries used only by the buffer TSO implementation",
)
```

配置脚本负责把 active buffer size 映射到 `SQEntries`。C++ 初始化时额外断言：

```text
tsoStore == serial => serial path; buffer-only state absent
tsoStore == buffer => needsTSO && SQEntries == tsoStoreBufferSize
```

## 6. 语义正确的实现结构

### 6.1 两阶段 store 生命周期

普通 WB、非原子、非 strictly ordered store 使用：

```text
address/data ready
  -> ordered commit into store buffer
  -> permission/RFO prepare (multiple outstanding)
  -> permission ready
  -> FIFO data publish
  -> store response
  -> SQ release
```

关键不变量：

1. prepare 可以重叠，publish 必须按 store 程序序；
2. prepare 不携带新 store data，也不能算作 store 已全局可见；
3. 若权限在轮到 FIFO head 前因 snoop/替换丢失，head 重新获取权限；
4. 只有真实 publish 完成后才能 `completeStore()` 并释放 SQ；
5. load forwarding 继续读取 SQ 中最新的匹配旧 store，不依赖 cache 中是否已有新值。

### 6.2 O3 LSQ 修改

建议修改：

- `src/cpu/o3/BaseO3CPU.py`
  - 增加 `TSOStore`、`tsoStore` 和 `tsoStoreBufferSize` 参数；
- `src/cpu/o3/lsq_unit.hh`
  - 将单一 `storeWBIt` 生命周期拆为 prepare frontier 与 FIFO publish frontier；
  - 为每个 SQ entry 保存 permission 状态和 fragment 完成状态；
- `src/cpu/o3/lsq_unit.cc`
  - `writebackStores()` 先为 eligible ordinary store 发权限准备；
  - 只允许 publish frontier 的 store 携带数据进入可见写路径；
  - retry、split response、squash、drain/takeover 都维护两个 frontier；
  - atomic、LL/SC、release、cache maintenance、local/UC、HTM 先保留串行路径。

现有 store 在发送时已被 `setCompleted()`，SQ 在 response 后才释放。新模式不要改变
ROB retirement 与 SQ lifetime 的这个基本分离，只改变缓存服务的准备阶段。

### 6.3 serial 路径隔离与一键回退

这是实现验收条件，不只是 CLI 约定：

1. `serial` 必须是 SimObject 和所有 runner 的默认值；
2. `LSQUnit::writebackStores()` 保留原 serial 函数体和原条件
   `(!needsTSO) || (!storeInFlight)`、`storePostSend()` 和
   `completeStore()` 的逻辑不改；只在函数尾部由
   `tsoStore == buffer` 调用独立的 permission-prepare helper；
3. buffer 的 prepare frontier、permission token、回调表和统计使用独立状态，
   只在 buffer 初始化时分配；serial 模式不得更新或查询它们；
4. Ruby 的 permission-only request 必须由独立 packet/request 标志触发。普通
   serial `WriteReq` 的 `Sequencer::makeRequest()`、`writeCallback()` 和 PMU
   路径保持原样；
5. 不允许用公共 callback 中大量 `if (buffer)` 分支逐步改写 serial 生命周期。
   buffer request 应走独立回调，最后复用经过审查的公共资源释放原语；
6. serial 回退测试必须比较旧二进制与新二进制
   `--tso-store=serial`：退出码、CPI、Ruby 请求/响应、原有统计值、PMU、FST
   hash 和 checkpoint/drain 状态均一致。对确定性的模拟结果要求
   bit-identical；`config.ini` 可多出 inactive 新参数，统计文件也可多出明确为
   inactive/zero 的 buffer-only 字段；
7. 命令行只将 `buffer` 改为 `serial` 后，buffer size 和 buffer-only Ruby
   请求完全失效，无需重新编译或修改其他选项；
8. 新二进制的“无 `--tso-store` 选项”、“`--tso-store=serial`”和
   “`--tso-store=serial --tso-store-buffer-size=<任意正整数>`”三种运行的模拟
   结果必须相同。

### 6.4 Ruby 修改

第一版复用 `SoftPFExReq` 作为 O3→Ruby 的内部 transport，但不把它的 early
response 当作权限完成通知：

- 当前 `Sequencer::makeRequest()` 没有把 `isPrefetchEx()` 映射成独立的
  store-permission 类型；
- `issueRequest()` 当前固定使用 `PrefetchBit_No`；
- RubyPort 对 software prefetch 有 early response，不能用它表示权限真正到达。

已新增内部 `STORE_PREPARE` Request flag、`RubyRequestType:Store_Prepare` 和
MESI_Three_Level 的独立 `Store_Prepare`/`SP_IE` 事件状态：

- 在 MESI_Three_Level 中按 `PF_Store`/GETX 语义获取可写权限；
- 不执行 `data.setData(pkt)`；
- O3 在 Ruby 接受内部请求后标记 prepare 已发；真实 FIFO store 若在权限到达前
  到来，会在 `SP_IE` 上接管并继续原 store 生命周期；若权限已到达则命中 E/M；
- 不作为第二条 retired demand store 重复计入原生 PMU；
- 若与同 line 请求合并，仍保持每个 CPU store prepare 的身份和完成状态；
- 独立统计已接收但尚未终止的 prepare；drop、真实 store 接管、权限响应和坏地址
  四类终点各闭合一次，RubyPort drain 会等待该计数归零。

第一版不把“权限已到达”单独回调到 SQ，因此不是完整的 SPR store-buffer RTL。
其正确性依赖真实带数据 store 仍保持原 `storeInFlight` FIFO 发布；prepare 只是
提前启动 GETX/UPGRADE，不能写入 store data，也不调用 `completeStore()`。

建议先只支持当前正式使用的 `MESI_Three_Level`。其他 Ruby protocol 在未实现对应
permission-only 事务时应启动失败，而不是静默退回可能不同的语义。

### 6.5 barrier 与特殊访问

第一版应采用保守且可证明的规则：

- `MFENCE`/`SFENCE`、locked RMW、原子操作进入前 drain 所有旧 publish；
- 不让普通 store prepare 跨越尚未完成的 store barrier；
- UC/WC、non-temporal、MMIO、cache maintenance、LL/SC、HTM、local access 继续
  使用现有串行路径；
- split store 在所有 fragment 权限准备完成后作为一个 FIFO 项发布；若第一版不
  实现，必须 fail closed 或回退整条 store，不能只发布部分 fragment；
- context switch、CPU switch、checkpoint、drain 均要求没有悬空 permission token。

## 7. 配置与身份合同

新增模式会改变 gem5 timing、跨核交错、cache/coherence 事件和 FST，属于新的
微架构 variant。必须：

1. 将 `tsoStore`、requested/active buffer size 及实际 SQ/Sequencer 容量写入
   最终 `config.ini` 和 `effective-target.json`；
2. 扩展 `tools/generate_fs_effective_target.py` 与
   `tools/validate_fs_oracle_identity.py`，让 buffer 缺字段或模式、容量、
   `needsTSO`、有效 SQ 不一致 fail closed；旧 binary 缺新字段时只兼容为
   `serial/114/inactive`；
3. 每个 variant 重新运行 gem5 并生成自己的 FST/oracle，不能复用 serial 基线
   trace；
4. 原生 Ruby PMU 区分 retired demand store、内部 permission request 和最终
   publish，保持 request/outcome 守恒；
5. 保留原 binary、配置和 source hash，不能只凭目录名标记为 SPR。

## 8. 验证门槛

### 8.1 架构正确性

至少加入多核 litmus：

- Store Buffering：无 fence 的 `0/0` 结果允许；
- Message Passing / Store→Store：观察到年轻 store 时不得仍观察到旧 store 的旧值；
- `MFENCE` 后的 Store Buffering：`0/0` 禁止；
- 同地址 store-to-load forwarding；
- 两条同 line store、两条跨 line store、权限丢失后重取；
- locked RMW、split store、retry、snoop、eviction 和 drain。

两种 TSO 模式都必须通过相同的允许/禁止结果集合；`buffer` 只应改变
频率和时序，不应新增架构禁止结果。再运行 Ruby tester 和适用的 gem5 regression。

### 8.2 机制与性能

增加统计：

- store-buffer occupancy/full cycles；
- prepare issued/completed/reacquired；
- publish count、publish width；
- permission-ready-but-not-head 等待；
- head-not-ready 等待；
- in-flight prepare 高水位；
- barrier/atomic/特殊路径回退次数。

容量 sweep 至少包含 `32/64/114/128`，其中 114 是 SPR-like 主点，32 是当前
gem5 profile 对照，64/128 用于识别容量趋势和 114 是否已进入饱和区。不能只比较
serial 与 114 后把全部差异归因于“并行 TSO”，因为该比较同时改变了机制和容量；
还要比较 `serial-32`、`buffer-32` 和 `buffer-114`。

微基准应同时覆盖：

- 全部 L1 M hit：新模式不应无故增加延迟；
- 多个独立 store miss/upgrade：prepare 在途数应大于 1，服务可重叠；
- 老 miss + 年轻 M hit：年轻数据仍不得先发布；
- store-heavy streaming：验证 buffer-full 和 drain throughput；
- sharing/false sharing：验证频繁失权时重新获取而不产生旧权限误用。

### 8.3 SPR 校准与 FastSim 下游

宿主 `/proc/cpuinfo` 已确认是 Intel Xeon Platinum 8457C，可用固定频率、单核绑定、
隔离 SMT sibling 的 store microbench 校准趋势。若通用 `perf list` 不提供所需
store-buffer 事件，应使用该型号对应的 Intel perfmon event JSON/raw encoding，
并记录事件名、编码、作用域和 multiplex 状态。

对 FastSim 的正式采用必须使用新 gem5 variant 的 fresh trace，并按
[`accuracy-reporting-contract.md`](accuracy-reporting-contract.md) 第 0、8 节：

- 先过 `tools/validate_fs_oracle_identity.py`；
- cache PMU 只用 native Ruby/SLICC 来源；
- 使用 `first-core-target-common-end-v1`；
- 报告逐案 gem5/FastSim CPI、相对误差、CPI absolute error；
- 聚合同时报告 MAPE 与 CPI MAE；
- 若作为新默认或版本级提升，跑完整 10-workload C4/C8/C16/C32 矩阵，并保留
  LBM C1/C13 retirement-window controls。

## 9. 已完成实现与验证

### 9.1 交付位置

为避免覆盖 `/data00/yinhaolang/gem5-fs` 和 `/data00/yinhaolang/TCSim` 中已有的
TaoTrace/Ruby 工作，第一版代码先在隔离树中完成并验证，再作为聚焦补丁叠加到
两个生产工作树。可重放补丁为：

- `patches/gem5-o3-tso-store-buffer.patch`：gem5 O3、Request、RubyPort、
  Sequencer 与 MESI_Three_Level 协议修改；
- `patches/tcsim-tso-store-buffer-plumbing.patch`：FS checkpoint/restore、
  TaoTrace wrapper 和 `gem5_fs_roi.py` 参数传播；
- FastSim 仓库内的 runner、collector、effective-target 和 identity validator
  修改直接保存在对应 `tools/` 文件；
- `tests/test_gem5_tso_store_options.py`、
  `tests/test_tso_store_identity.py` 和
  `tests/test_uarch_common_end_collection.py` 覆盖 CLI、回退与身份门禁。

两份外部 patch 已实际应用到 `/data00/yinhaolang/gem5-fs` 与
`/data00/yinhaolang/TCSim`。2026-09-18 又在临时副本上分别完成
`--reverse --fuzz=0`、`--forward --fuzz=0` 和逐文件 `cmp`，往返后的 13 个
gem5 文件与 3 个 TCSim 文件均与生产树逐字一致。该检查同时证明补丁可精确回退，
不依赖 offset 或 fuzz。

### 9.2 构建结果

隔离树的 `X86_MESI_Three_Level/gem5.opt` 已在最终 sender-state 修订后完整重建：

```text
sha256:
b8e5f0703d3b1a3d30333b0030e1635c647f96800cb564d148a3bd303e4ab2d4
size: 2724062784 bytes
compiled: 2026-09-18 11:58:01
```

使用 Python 3.11、SCons 4.9 和
`TAOGEN_SHARED=/data00/yinhaolang/taogen/shared` 构建。系统 Python 2、
Python 3.7 和旧 SCons 3.0 均不适用于该 gem5/pybind11 组合。

生产树 `/data00/yinhaolang/gem5-fs/build/X86_MESI_Three_Level/gem5.opt`
也已完成全量加最终增量构建：

```text
sha256:
c2c6be9dbdc0da94ec217dda0cdf042ebbf92a8a8176f7e2de5798357592837b
size: 2724046376 bytes
compiled: 2026-09-18 12:54:44
```

### 9.3 serial 一键回退

同一 workload、工作目录和二进制参数下：

| 模式 | buffer size 参数 | 有效 SQ | simTicks | prepare |
|---|---:|---:|---:|---:|
| 旧生产 binary | 无 | 32 | 1,282,572,810 | 无此统计 |
| 新 binary 默认 `serial` | 114（inactive） | 32 | 1,282,572,810 | 0 |
| 新 binary `serial` | 7（inactive） | 32 | 1,282,572,810 | 0 |

旧 binary、新默认 serial 和显式 `serial --tso-store-buffer-size=7` 使用相同
workload、工作目录和 executable path。旧 binary 与新 explicit-serial 的
`config.ini` 除新增 inactive 参数外一致；`stats.txt` 去除 host wall-time/rate/
memory 以及新增的两个零值 prepare 统计后逐行一致。因此把
`--tso-store=buffer` 单独改回 `--tso-store=serial` 会恢复原 SQ 和原数据发布
路径，buffer size 不生效。

真实 4-core Stockfish FS 也用显式
`--tso-store serial --tso-store-buffer-size 114` 完成 10K instruction、
`any-core` restore；结果再次为 `simTicks=1,476,188`、`SQEntries=32`、
`tsoStoreBufferActive=false`，四核 `storePrepareIssued=0`。这与此前默认
serial FS smoke 的 tick 完全一致。driver 在 serial 模式不向下层 gem5 命令追加
新选项，以兼容旧 binary；模式切换会选择对应身份的 checkpoint，而不会把
buffer checkpoint 重标成 serial。

### 9.4 buffer 机制 smoke

单核独立 store-stream 的 checksum 在所有模式均为
`8232763195715158016`：

| 模式 | 有效 SQ | simTicks | prepare issued | blocked |
|---|---:|---:|---:|---:|
| serial | 32 | 1,282,627,422 | 0 | 0 |
| buffer | 32 | 406,528,731 | 525,479 | 0 |
| buffer | 114 | 398,250,684 | 525,502 | 0 |

相对同条件 serial，buffer-32 与 buffer-114 的该微基准分别约为 `3.15x` 和
`3.22x`。这只说明 permission prepare 可有效隐藏串行 Ruby 获取权限延迟，不是
完整工作负载的性能结论。

修正测试线程配置后，双核跨 cache-line Store→Store litmus 实际执行 20,000 轮：

| 模式 | violations | simTicks | prepare issued |
|---|---:|---:|---:|
| serial | 0 | 1,266,659,073 | 0 |
| buffer-114 | 0 | 1,097,317,917 | 41,568 |

buffer 运行没有出现无效 SLICC transition 或 panic。该测试证明当前覆盖场景下
没有观察到年轻 store 先于旧 store 可见；它不能替代第 8.1 节列出的完整 litmus
集合。

### 9.5 FastSim 集成与真实 FS 验证

- TSO runner/effective-target/identity/standalone-driver 单元测试：15 项通过；
- common-end collector 回归：19 项通过；
- Python 3.11 源码编译检查通过；
- 使用一份真实旧 FS `config.ini` 重新生成 sidecar，得到
  `serial/114/inactive/SQ=32`，随后
  `tools/validate_fs_oracle_identity.py` 通过；
- buffer 身份只有在 request/profile/effective-target 的 mode、size、active、
  `needsTSO` 和 `SQEntries` 全部一致时通过。
- permission prepare 使用独立的 Sequencer outstanding 计数，不进入 retired
  demand request table；协议在 drop、真实 store 接管、权限响应或坏地址四类
  终点各闭合一次。buffer checkpoint drain smoke 中 RubyPort 在 333 ticks 后报告
  `Drain count: 0` 并 `signalDrainDone()`；同一 synthetic SE 配置的 serial
  对照也在 333 ticks 后得到相同结果。两者随后都停在该测试配置既有的 O3
  pipeline drain，因此这证明新增 prepare 不再阻塞 RubyPort drain，但不宣称
  该 synthetic 配置完成了 checkpoint round-trip。
- 生产 binary 使用已有 serial ROI checkpoint 完成了 4-core Stockfish、10K
  instruction、`any-core` 的 FS restore smoke；`simTicks=1,476,188`，
  `storePrepareIssued=0`，final-config identity gate 为 `valid=true`，
  `tsoStore=serial`、`tsoStoreBufferActive=false`、`SQEntries=32`。
- 尝试以 `buffer-114` 复用该 serial checkpoint 时，checkpoint profile gate
  按设计拒绝，证明不会把 serial checkpoint 误标成 buffer。
- 在宿主 KVM 环境中从 boot checkpoint fresh 创建了 Stockfish C4
  `buffer-114` ROI checkpoint，cache key 为 `164e36fefeb53572a26b`，路径为
  `tmp/tso-production-buffer-checkpoint-final/checkpoints/`
  `mesi-three-level-3GiB/4c/706.stockfish_r/164e36fefeb53572a26b/checkpoint`。
  `metadata.json` 和父目录 `request.json` 均记录
  `fresh-kvm-boot`、`tso_store=buffer`、active=true、size=114；`m5.cpt`、
  3 GiB 压缩 memory payload 和磁盘 COW 均完整。
- 从该 fresh checkpoint 完成无 warmup 的 4-core Stockfish 10K
  `any-core` buffer restore，结果目录为
  `tmp/tso-production-buffer-checkpoint-final/results/sample/`
  `mesi-three-level-3GiB/4c/706.stockfish_r/164e36fefeb53572a26b/`
  `20260918T053912Z`。运行 `exit_code=0`，`simTicks=1,688,642`，
  `simInsts=10,475`；Ruby 收到 113 个 `Store_Prepare`，各核
  `storePrepareIssued=[37,38,38,0]`、blocked 全零。
- 该 buffer 结果的最终 `config.ini` SHA-256 为
  `e758805c611ca483745ba65e67badb3ea692477630f04784212c30b73af5371e`。
  `effective-target.json` 与 `tao_trace/uarch_profile.json` 均从该最终配置生成，
  两者确认 `buffer/114/active/SQ=114/needsTSO=true`；
  `tools/validate_fs_oracle_identity.py` 返回 `valid=true`、1/1 valid、0 mismatch、
  `identity_source=final-config-ini`。
- 真实 smoke 暴露并修复了 standalone `gem5_fs_roi.py` 的两个身份链缺口：
  Tao 模式现在会自动注入 canonical final-config generator/event dictionary
  （调用方显式覆盖仍优先），且 `--tao-no-records` 仍会把
  `uarch_profile.json` 保留在结果目录。清空所有相关环境变量后的复跑通过，
  不再需要手工设置 sidecar 环境。
- FastSim 默认构建命令成功，`./build/fastsim_tests` 输出
  `all FastSim tests passed`。

这些结果验证了生产机制、fresh KVM checkpoint、FS restore、配置传播和身份
门禁，但该 10K 运行使用 `--tao-no-records`，不是正式 FST/accuracy 样本；尚未
完成第 8 节的完整 litmus、Ruby tester、10M common-end FST/oracle 或 10-workload
精度矩阵，因此 buffer 仍是 opt-in 实验模式。

## 10. 后续实施顺序

| 阶段 | 交付 | 通过条件 |
|---|---|---|
| P0 | `--tso-store`、buffer size、默认 serial、config/identity 输出 | serial 与当前二进制 bit-identical；改单个模式选项可回退 |
| P1 | permission-only Ruby 事务与单 store 回调 | 不写数据、不重复 PMU、drain 守恒 |
| P2 | LSQ 多 prepare + FIFO publish | litmus 全过，跨行年轻 store 不提前可见 |
| P3 | retry/split/fence/atomic/checkpoint 完整化 | 无悬空请求，Ruby tester/regression 通过 |
| P4 | SPR 微基准校准与 fresh gem5/FastSim 矩阵 | 趋势、CPI/PMU、绝对误差及来源门禁通过 |

推荐先实现 P0/P1 的独立小补丁，不直接改生产默认。若只想快速量化性能上界，可另做
明确标记为 `relaxed-unordered-stores` 的实验模式；该结果不能用于证明 SPR TSO
正确性，也不能替代 `buffer`。

## 11. 参考

- Intel 64 and IA-32 Architectures Software Developer's Manual：
  <https://www.intel.com/content/www/us/en/developer/articles/technical/intel-sdm.html>
- Intel 64 and IA-32 Architectures Optimization Reference Manual：
  <https://www.intel.com/content/www/us/en/developer/articles/technical/intel-sdm.html>
- MICRO 2024, *Temporarily Unauthorized Stores: Write First, Ask for
  Permission Later*（以 114-entry Alder Lake store buffer 为公开研究基线）：
  <https://doi.org/10.1109/MICRO61859.2024.00065>
- gem5 O3CPU 文档：
  <https://www.gem5.org/documentation/general_docs/cpu_models/O3CPU>
- x86-TSO formal model：
  <https://www.cl.cam.ac.uk/~pes20/weakmemory/cacm.pdf>
