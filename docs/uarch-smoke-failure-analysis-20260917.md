# C4/C8 全矩阵 smoke 失败排查（2026-09-17）

## 1. 现场与全量核对

任务：`tmp/uarch-generalization-c4-c8-20260917/`。
`collector-state.json` 记录退出时间 `2026-09-16T18:17:43.007791+00:00`，
即北京时间 2026-09-17 02:17:43。检查点准备 136/136；smoke 34/136 完成，
102/136 失败；正式采集未启动。采集器的 `status` 返回 `process: null`。

对全部失败 case 的 `finalize.log`、`oracle/cpl_class.jsonl` 和
`trace-scratch/functional-boundary-core*.json` 核对，而非仅抽样：

| 失败类型 | case 数 | 证据 |
|---|---:|---|
| `CPL/common end mismatch` | 101 | 涉及 140 个核，全部 `measurement_records=0` 且 `first_tick=last_tick=0` |
| `Ruby functional read failed` | 1 | `uarch-baseline-c08-811.tealeaf_s`；原始尝试加两次重试均在 `0xdf0c080` 失败 |

这里没有 FastSim 回放或性能评分；不构成 CPI/PMU 精度或吞吐结论。

## 2. 空测量核的关闭元数据缺陷

已核对冻结二进制对应的源文件：
`tmp/se-fs-paired-c4-20260820/gem5-se-isolated/src/cpu/o3/probe/tao_trace.cc`，
SHA-256 为 `713c63a847d63f1e2fcdd3ff8be0eb3834564148a257e3469d885baeea8a219f`，
与 `tmp/first-core-common-end-20260911/source-hashes.json` 一致。

调用链：

1. `beginFunctionalMeasurement()` 打开所有参与核的功能测量窗口。
2. `initializeCpl()` 在该核首次被计入的用户事件/来自用户态的异常发生时才初始化
   CPL 时间。`accountCplCommit()` 在未初始化时跳过 kernel commit；
   `emitMicroRecord()` 也跳过这部分未建立 CPL 归属的 kernel records。
3. 最快核达到 10K user UOP 后，`noteFunctionalRecordEmitted()` 为所有参与核设置
   `cpl_measurement_end_tick_` 并调用 `finalizeCplMeasurement()`。
4. `finalizeCplMeasurement()` 仅在 `cpl_initialized_` 为真时执行 `settleCpl()`，
   因此从未初始化的核仍写出 `first_tick=last_tick=0`。
5. `tools/common_end_capture.py:validate_common_end()` 要求每核 CPL 的 `last_tick`
   等于共同终点，正确地阻止该产物被标记完成。

例如 baseline ASTCENC C4 的 core 2：预热 287,684 records，测量 0 records，
boundary 已声明 `measurement_closed=true`，共同终点为 `21777763734036`，
但 CPL 行仍为零。不能把这种情况说成“必须让每个核都达到目标”：
共同终点合同允许慢核保留实际数量，包括短窗口内的零数量。
也不能仅凭零记录断言整个窗口都在 idle；采集器还会过滤未建立 CPL 归属的 kernel tail。

修复应在生产端显式表达“参与共同关闭，但没有被计入的本地测量事件”，同时保持
零 records、零 cycles、原始 FST 和真实分母。可采用独立的关闭 tick/初始化状态字段；
若保留旧 `last_tick` 合同，应为经过空人口与零计数检查的核表达零长度终止区间，
不能只把 `last_tick` 从 0 改成共同终点而保留 `first_tick=0`，否则破坏 span 守恒。
不得按目标补齐 records、给空核制造活跃周期，或无条件放宽共同终点检查。

需要回归覆盖：一个 trigger 核和一个零测量核、非空核提前停止、空人口但非零周期、
缺失参与核、未达到目标的 trigger。正式 10M 通常降低空核概率，但不能替代修复。

## 3. TeaLeaf C8：测量页表快照遇到 Ruby 内存读取瞬态

失败现场在：
`smoke/source/uarch-baseline-c08-811.tealeaf_s/`，
另外两次尝试保存在 `smoke/failed/uarch-baseline-c08-811.tealeaf_s-*`。

三次均已完成 atomic → O3 切换和初始页表快照，日志包含：
`initial guest PTE snapshot root=0xae38000 ... known=25088`。
串口随后出现 `tealeaf-roi-solver operation=workbegin-serial`；
只有 `initial-pte-state.json`，没有 `measurement-pte-state.json`。
因此不能套用旧批次“初始页表快照偶发失败、重试即可”的判断。

调用链为：
`Terminal::writeData()` → `traceMeasurementBegin()` → `captureMeasurementPteState()` →
`readGuestPteSnapshot()` → `System::physProxy` → Ruby functional read。
`RubySystem::simpleFunctionalRead()` 会搜索有效 cache copy、controller buffers、
network 和符合权限的备用 copy；查不到时，`RubyPort::recvFunctional()` fatal。

在独立目录做了两次单 case 串行复现，均再次在同一地址失败。第一次启用
`RubySystem`；第二次只在失败前 1,000,000 tick 的区间启用
`RubySystem,RubySlicc,ProtocolTrace`。命令和日志分别保存在：

- `tmp/uarch-smoke-diagnosis-20260917/tealeaf-c8-ruby-debug/`
- `tmp/uarch-smoke-diagnosis-20260917/tealeaf-c8-protocol-debug/`

第二次日志给出目标行 `0xdf0c080` 的完整局部链路：

| tick | 协议事件 |
|---:|---|
| 22412493700705 | core 0 发起 LD |
| 22412493701370 | L0Cache（物理 L1）`I → IS` |
| 22412493702036 | L1Cache（物理私有 L2）`I → IS` |
| 22412493704034 | L2Cache bank 2（物理 LLC）`NP → ISS` |
| 22412493706032 | Directory 2 接到 Fetch，`I → IM`，向内存请求数据 |
| 22412493728343 | 测量页表快照读取同一行；四个 controller 均 Busy，无 RO/RW/Maybe_Stale 副本，buffer/network 搜索失败 |

`MESI_Three_Level.slicc` 复用 `MESI_Two_Level-dir.sm`。目录 `I` 表示内存有效、
所有 cache copy 无效；`I → IM` 只排入 memory fetch，`IM → M` 等待
`Memory_Data` 返回。该故障点正处于这个只读取数窗口，内存仍是有效数据来源，
但 `IM` 声明为 `AccessPermission:Busy`，而 `simpleFunctionalRead()` 的 Busy
路径不调用目录的 `functionalMemoryRead()`。这是 functional-read 的可见性缺口，
不是新硬件参数不被支持，也不是本轮宿主并发过高才能触发。

**已实现修复（见 §7 单 case 验证）：** 在
`src/mem/ruby/system/RubySystem.cc` 的 `simpleFunctionalRead()` Busy 分支，
按现有顺序先搜 controller buffers、network、`Maybe_Stale` 副本；全部未命中且
存在处于 Busy 的 Directory 控制器时，才调用该目录的
`functionalRead(line_address, pkt)`。目录无 TBE（clean `I → IM` 不分配 TBE）时
它落到 `functionalMemoryRead()` 读权威内存。安全性论证：
- 只有在无任何 RO/RW 缓存副本、所有 controller buffer（含承载在途脏写回
  `MEMORY_WB` 的 `requestToMemory`，见 `Directory_Controller::functionalReadBuffers`）、
  network、`Maybe_Stale` 均未命中后才触发，此时唯一权威副本即内存。
- `functionalMemoryRead()` 先查 mem 请求队列再 `sendFunctional`，因此 `M → MI`
  等脏写回在途数据会被 buffer 命中，不会被内存旁路读成陈旧值。
- 目录若持有 TBE（DMA 路径），`functionalRead()` 走 `testAndRead(tbe.DataBlk)`
  而非内存，仍然正确。故该回退对所有目录 Busy 状态都安全，不是只对 `IM` 放宽。

未加通用回归 fixture；此处以该 TeaLeaf checkpoint 的单 case 复现→通过作为验证，
后续应补 dirty owner、写回、DMA 与 PTE 内容一致性的自动化回归。

备选是重设计“停止发射、结清在途请求、快照、统一重开计数”的测量屏障，
但这会改变预热状态和测量边界，属于更大的采集语义变更，不建议仅为绕过错误而先做。

不能用禁用 Ruby、吞掉读错误、从可能陈旧的 backing store 盲读、伪造 PTE 值，
或删除 PTE 门禁作为正式数据修复。

## 4. 前置失败遮住的第三个门禁问题

进一步使用维护的 `audit_p1_native_response_sideband.audit_result()` 对全部 135 个
已有整理后 oracle 做只读审计，再检查原生来源、层级守恒和 drain 门禁：
134 个通过，1 个失败；TeaLeaf 的 gem5 fatal case 没有可用的整理后 oracle。
这不是绕过共同终点门禁来接受数据，134 也不是最终可用 case 数。

失败项为 `uarch-j4-c08-706.stockfish_r`。它原先先在 core 6 的 CPL/common-end
检查退出，后面的 native/P0 门禁尚未执行。core 5 实际存在：

- `unknown_kernel_cycles=532`，`measured_cycles=1830`，比例 `0.290710383`；
- `unknown_kernel_sources=[{"source":"transition:swapgs","count":1}]`；
- 用户测量人口 430 UOP、混合测量人口 3,785 records；
- CPL tick 求和守恒，且该 core 5 的 `last_tick` 本身等于共同终点。

`accountCplCommit()` 在当前分类为 USER、观察到 kernel commit 且没有 pending
syscall 时会进入 `UNKNOWN_KERNEL`。这里只能证明进入路径缺少归属；
不能仅凭 `swapgs` 就断定是 syscall、IRQ 或 scheduler，更不能直接重标计数。
应针对这一 case 的 core 5 记录首个 unknown 转换前的 KernelEntry、syscall
边界与退休顺序，检查跨测量屏障的 pending 状态和用户/内核事件顺序。
在原因查明并通过零 unknown 门禁之前，该 case 仍不可用于正式采集结果。

审计结果：`tmp/uarch-smoke-diagnosis-20260917/native-gate-recheck.json`。

## 5. 修复与恢复顺序

- 保留本轮原始现场，不手改 oracle、完成标记或 `run-contract.json`。
- 原采集器已对 Ruby 错误自动重试两次；直接 `start` 会再次遇到 CPL 问题，
  TeaLeaf 又经两次独立串行复现，不能依赖增加同样重试。
- 先修空核共同关闭元数据与目录 `IM` 的 functional-read 可见性，定向回归
  baseline ASTCENC C4、baseline TeaLeaf C8；同时定位 J4 Stockfish C8 的
  unknown kernel 归属，保留零 unknown 门禁。
- `preflight()` 固定检查旧 gem5 SHA-256；`freeze_runtime()` 又冻结二进制、工具、
  TCSim wrapper 等输入。修改源码或换二进制后，不能直接复用原合同声称无缝续跑。
- 应将经过验证的新二进制身份显式纳入采集入口，而不是删除 SHA 检查或用旧名字
  覆盖冻结二进制。本轮 122 个冻结输入的大小/mtime 核对未见变化；两份关键
  TaoTrace 源码的 SHA-256 与冻结源清单一致。
- 应先用新版本完成定向回归，再建立新输出目录和新的二进制/源码身份合同；
  如要复用已有检查点，必须走可审计的兼容性验证，不能关闭 binary/config mismatch
  检查。新一轮全 136-case smoke 通过后才能进入正式采集。

## 6. 本次验证与未执行事项

- 全量空核检查：101 case、140 个核，相关 CPL tick/cycle/commit/entry 字段和
  native committed-memory 人口均为零，无反例。结果见
  `tmp/uarch-smoke-diagnosis-20260917/empty-core-audit.json`。
- `test_common_end_capture.py` 4 项和 `test_uarch_common_end_collection.py` 18 项
  现有测试均通过；现有 fixture 没有零测量核，不能视为已覆盖此次缺陷。
- 两次定向 gem5 诊断均正常结束于预期 fatal，未留后台采集子进程。
- 本次已实现并单 case 验证目录 `IM` functional-read 修复（§7）；空核共同关闭
  元数据缺陷与 J4 Stockfish C8 unknown kernel 归属仍未修复。没有启动新的
  全矩阵 smoke 或正式采集，没有改动原始数据、完成状态或采集入口合同。

## 7. 目录 IM functional-read 修复的重建与单 case 验证

源码改动位于冻结二进制对应的隔离源码树
`tmp/se-fs-paired-c4-20260820/gem5-se-isolated/src/mem/ruby/system/RubySystem.cc`
（`X86_MESI_Three_Level` 经 `MESI_Three_Level.slicc` 包含 `MESI_Two_Level-dir.sm`，
即修改点确实在编译路径上）。

重建（不覆盖冻结二进制）：
- 环境 `conda activate yinhaolang`（Python 3.11.15 + scons 4.10.1），
  `TAOGEN_SHARED=/data00/yinhaolang/taogen/shared`（提供 `lru_banked.hh` /
  `uarch_profile.hh`，与 preflight 身份路径一致）。
- 命令 `scons build/X86_MESI_Three_Level/gem5.opt -j128`。
- 冻结参考 `tmp/first-core-common-end-20260911/gem5.opt` SHA-256 仍为
  `473ef0cb4792208f9a3ff60633142e4291da6816a693dcecc9aa50837b9c1471`（未改动，
  与新产物不同 inode）。修复后二进制存于
  `tmp/uarch-ruby-funcread-fix-20260917/gem5.opt`，SHA-256
  `33701d5a3d4d5b68240a99368caa8cd12e5429e7674f97e5c8a8443bfa8f1500`。
- 该产物链接需 `GLIBCXX_3.4.30`（conda `python3-config --ldflags` 引入
  `-L.../envs/yinhaolang/lib`），运行时以
  `LD_LIBRARY_PATH=/root/miniconda3/envs/yinhaolang/lib` 满足；此为链接工具链差异，
  与 Ruby 修复正确性无关。正式采集入口需以新二进制身份显式建合同（见 §5）。

单 case 验证：以失败 case `uarch-baseline-c08-811.tealeaf_s` 的原始
`command.json` 为准，仅替换二进制与输出目录，保留同一检查点
（`.../6d99d2d317b0131c447e/checkpoint`，`source=checkpoint-cr3`，root `0xae38000`）、
`--wait-for-roi-workbegin` 与 `--tao-functional-warmup`，并补齐采集器环境变量
（`FASTSIM_EFFECTIVE_TARGET_GENERATOR/PYTHON`、`FASTSIM_EVENT_DICTIONARY`、
`TAOGEN_SHARED`）。输出目录
`tmp/uarch-ruby-funcread-fix-20260917/validate-tealeaf-c08-811/`。

结果（`run.log`）：
- 在原 fatal 同一 tick `22412493728343` 完成测量页表快照：
  `measurement guest PTE snapshot root=0xae38000 known=25088 present=8671
  nonpresent=16417 boundary_inflight_faults=0`；`Ruby functional read failed`
  计数 0；`WORKBEGIN tick=22412493728343` 通过。
- ROI 跑到 `target=10000` user UOP，`common_end_tick=22412497368033`，
  `committed native-response drain complete`，`exit ... cause=... max instruction
  count`，`stats.txt` 27,155 行。
- 关键产物 `trace-scratch/measurement-pte-state.json` 写出（旧失败现场缺失该文件），
  `present_pages=8671`、`nonpresent_pages=16417`、`boundary_inflight_faults=[]`。

即修复在原确定性失败点消除了 fatal 且不改变共同终点语义。仍未做：全 136-case
smoke、`fastsim_tests` 门禁构建、空核/Stockfish 两项其余修复，以及跨变体的
自动化一致性回归。

## 8. 空测量核修复、合并二进制与端到端验证（2026-09-17 续）

在 §7 的 Ruby 修复之上，本节完成 §2 的空测量核缺陷修复，将两处修复合并进同一
二进制并纳入采集入口合同，对两类失败各做一次端到端定向回归。

### 8.1 生产端修复与被推翻的初始判断

改动位于同一隔离源码树
`tmp/se-fs-paired-c4-20260820/gem5-se-isolated/src/cpu/o3/probe/tao_trace.cc`
的 `finalizeCplMeasurement()`：`cpl_initialized_` 为假时，若该核有有效
`cpl_core_id_` 且进程级静态 `cpl_measurement_end_tick_` 非零（证明共同关闭已
建立），则把 `cpl_first_tick_ = cpl_last_tick_ = cplMeasurementEndTick()`，写出
`last_tick == common_end`、`span=0`、`measured=0` 的零长度终止区间——不补齐
records、不制造活跃周期，所有 class 的 cycle/commit/entry 保持为零。

初始两版判断均被现场证据推翻，记录以备后续参考：

- 第一版按“该核在 trigger 的参与者循环里被赋值 per-core 终点”的假设，守卫
  `cpl_measurement_end_tick_per_core_.find(core)`。重建重跑后空核仍 `last=0`。
- 第二版改用静态终点，但沿用了 `cplMeasurementEndTick()` 里的 `require_roi_`
  前置条件。重跑后空核依旧 `last=0`，且与第一版逐字节相同的输出说明分支根本
  没有执行。
- 于是在 `finalizeCplMeasurement()` 入口加临时 `warn` 打点，重建重跑得到决定性
  证据：finalize 时 **所有核 `require_roi_=0`**，而 `static_end` 与空核的
  `percore` 均已正确等于 `21457813974732`。即根因是我从 `cplMeasurementEndTick()`
  误抄了恒为假的 `require_roi_` 门；去掉该门、仅保留 `cpl_core_id_ >= 0 &&
  cpl_measurement_end_tick_ != 0` 后修复生效。打点代码已移除。

`cplMeasurementEndTick()` 自身即便 `require_roi_` 为假也会落到非零静态
`cpl_measurement_end_tick_`，因此返回的仍是正确共同终点（有 per-core 值时用
per-core，否则用相同的静态值），不会退化到 `curTick()`（仅在无共同关闭时才用）。

### 8.2 回归测试

`tests/test_common_end_capture.py` 新增两项，直接复现失败门禁：
`test_empty_measurement_core_closes_on_common_end`（三核：一 trigger、一慢核、
一零测量核，`n_user=0` 且 `last_tick==common_end` 时通过）与
`test_empty_core_with_zero_last_tick_is_rejected`（把空核 `last_tick` 改回 0 时
必须触发 `CPL/common end mismatch`）。`test_uarch_common_end_collection.py` 中
`test_damaged_checkpoint_is_preserved_and_reprepared` 的假 `args` 补齐 `binary`
字段以匹配 `environment(binary)` 的真实签名。两模块共 24 项测试通过。

### 8.3 合并二进制与采集入口合同

两处修复在同一源码树 `scons build/X86_MESI_Three_Level/gem5.opt -j128` 重建，
产物安装到 `tmp/uarch-common-end-fix-20260917/gem5.opt`，SHA-256
`7814e0e305581c1e87ea04e5b99f762f849103c2b97e0b771a342a33d98ef6e5`。冻结二进制
`tmp/first-core-common-end-20260911/gem5.opt`（`473ef0cb…`）未改动。

采集入口 `tools/collect_uarch_common_end.py` 的合同更新（非删除 SHA 检查、非覆盖
冻结件）：
- `DEFAULT_BINARY` 指向合并修复二进制；`preflight()` 的固定 SHA 更新为
  `7814e0e3…`，仍然 fail-closed。
- 该二进制经 conda `python3-config` 链接需 `GLIBCXX_3.4.30`，系统
  `/opt/gcc-11.5.0` 的 libstdc++ 只到 `3.4.29`。为使采集自足、不依赖调用者用哪个
  解释器，把所需 libstdc++ 随二进制放到同级 `lib/`
  （`tmp/uarch-common-end-fix-20260917/lib/libstdc++.so.6[.0.34]`），`environment()`
  改为 `environment(binary)`，在该 `lib/` 存在时将其前置到 `LD_LIBRARY_PATH`；
  四处调用点（prepare/gem5/finalize/detached start）均传入 `args.binary`。这是
  链接工具链差异，与两处修复的正确性无关，不改动系统工具链。

### 8.4 端到端定向回归

用合并二进制以两个失败 case 的原始 `command.json`（仅替换二进制与输出目录，保留
原检查点与 ROI/warmup 标志）重跑，再对产出的 boundaries + oracle 执行与采集器
相同的 `validate_common_end`（即原先抛 `CPL/common end mismatch` 的那道门）：

- baseline ASTCENC C4（`731.astcenc_r`，空核缺陷）：gem5 退出 0；空核 1、2 现为
  `last=21457813974732`、`span=0`、`measured=0`，`cores_with_mismatch=0`；
  `validate_common_end` 通过，`common_end=21457813974732`、`trigger=3`、
  `per_core_user_uops={0:9289,1:0,2:0,3:10000}`。
- baseline TeaLeaf C8（`811.tealeaf_s`，Ruby 缺陷且同时含一个空核）：gem5 退出 0，
  `Ruby functional read failed` 计数 0，原 fatal 的 `WORKBEGIN tick=22412493728343`
  通过；空核 6 现锚定于 `last=22412497368033`；`validate_common_end` 通过，
  `common_end=22412497368033`、`trigger=3`、8 核 `user_uops=59498`。

回归产物存于 `tmp/uarch-common-end-fix-20260917/regress/{astcenc,tealeaf}/`。

### 8.5 门禁与未执行事项

- C++ 门禁：`cmake --build build -- -j16` 成功，`./build/fastsim_tests` 输出
  `all FastSim tests passed`。
- Python：`test_common_end_capture` 与 `test_uarch_common_end_collection` 共 24 项通过。
- 仍未做：J4 Stockfish C8 的 unknown-kernel 归属修复（根因见 §9）；全 136-case smoke 与
  正式采集；Ruby 修复的 dirty-owner/写回/DMA 自动化回归与跨变体一致性回归。
  未启动新采集，未改动原始现场数据、完成状态标记或既有 `run-contract.json`。

## 9. J4 Stockfish C8 core 5 `transition:swapgs` 的根因（合并二进制重跑确证）

§4 只用事后离线复审看到该 unknown，且原始 case 先死在 core 6 空核 bug 上，从未用
两处修复后的合并二进制重跑，存在"是否为崩溃残留伪影"的疑点。本节用运行时事实闭合。

### 9.1 合并二进制单 case 重跑

以原始 `command.json` 单跑，仅替换二进制（`tmp/uarch-common-end-fix-20260917/gem5.opt`，
SHA-256 `7814e0e3…`）与输出/trace 目录，保留同一检查点、ROI 与 warmup 标志，补齐采集器
环境变量。脚本与日志：`tmp/uarch-common-end-fix-20260917/regress/stockfish/{run.sh,run.log}`。
gem5 退出 0（`cause=a thread reached the max instruction count`），无 Ruby fatal、无
`collection error`；warmup 边界与原始逐字一致（core5 `records=1786871`，
`common_end_tick=22852223044524`，trigger core 1）。

### 9.2 两个门禁问题的判定

- **core 6 空核**：重跑后 `first=last=22852223044524`（共同终点）、`span=0`、`measured=0`，
  8 核 `last_tick` 全部相等，`cores_with_mismatch=0`——§2/§8.1 的空核修复生效，原
  `CPL/common end mismatch` 消失。
- **core 5 unknown**：与原始产物**逐字节相同**——`unknown_kernel_cycles=532`、
  `unknown_kernel_commits=1242`、`unknown_kernel_entries=1`、`first_tick=22852222435134`、
  `unknown_kernel_sources=[{"transition:swapgs",1}]`、`event_counts.syscall=2`（sysnum 228
  `clock_nanosleep`）。**即该 unknown 是确定性的，不是空核崩溃的残留伪影。**

### 9.3 归属：跨测量屏障的 syscall 处理程序头部

core 5 的测量窗口 = FST 最后 3785 条 record（`warmup_records=1786871`）。逐条解码：

| record | PC | 含义 |
|---:|---|---|
| 1786870 | `0x7ffdc0be7bb0`（用户态） | `syscall` marker（`op_class=-1`），落在 warmup，屏障**前一条** |
| **1786871** | `0xffffffff82400080` | 屏障后第一条，`entry_SYSCALL_64` 首个 uop（`swapgs`），内核态 |
| 1788112 | — | 该内核段结束（下一条 1788113 `K->U` 回到 `0x7ffdc0be7bb2`） |

leading 内核段长度 `1788113-1786871=1242`，**精确等于 `unknown_kernel_commits=1242`**。窗口内
另两个 syscall（entry@1788328、1789850，同为 `entry_SYSCALL_64`）的 `syscall` marker
（1788327、1789849）都在窗口**内**，被正确归为 SYSCALL。唯独 leading 这段的用户侧
`syscall` 边界退休在屏障前一条（warmup），处理程序主体跨过屏障。

代码路径（`tao_trace.cc`，与冻结源逐字节同，diff 为空）：`noteCplSyscallEntry()` 只在
`cplMeasurementActive()` 为真时置 `cpl_syscall_pending_`（1638-1645）；core 5 的这条
`syscall` 边界在 warmup 退休，`functional_measurement_started_` 尚假，故 **pending 从未被
置位**。屏障后 core 5 首个被计入的 commit 是该 syscall 宏op 的**用户解码 uop**（
`first_tick-WORKBEGIN=1998 ticks=6 cycles`，`initializeCpl(initial_user=true)` → USER）；
随后 `swapgs`（内核）在 `accountCplCommit` 命中 `!decoded_user && cpl_cycle_class_==USER`
且 `cpl_syscall_pending_==false`（3030-3040），落入 `UNKNOWN_KERNEL` 并记 `transition:swapgs`。

即根因是 **syscall 缺少跨 warmup→measurement 屏障的 pending latch**。框架对 page-fault
已有同类先例：`onKernelEntry()` 在 `functional_warmup_ && !functional_measurement_started_
&& fromUser && page_fault` 时置 `premeasurement_page_fault_pending_`（1740-1745），measurement
开始后首个用户 commit 清除（6444-6449）。syscall 无对应字段，故边界恰好切在某核 syscall
中段时（本矩阵中仅此一例）就产生一个 unknown。

### 9.4 修复方向（保留零 unknown 门禁）

按 page-fault 先例补一个 `premeasurement_syscall_pending_` 类 latch：在 warmup 阶段
（`!functional_measurement_started_`）记录"最近一条尚未返回用户态的 syscall 边界及其
sysnum"，measurement 开始后，若该核首个内核 commit 出现在任何用户 commit 之前且仍在该
syscall 处理程序内，则把 leading 段归为 SYSCALL（复用已捕获的 sysnum），并在首个用户
commit 或 SYSRET/IRET 处清除。这样把 1242 commits / 532 cycles 从 UNKNOWN_KERNEL 移入
SYSCALL(228)，`unknown_kernel_entries` 归零，门禁自然通过——**不放宽门禁、不按名字重标、
不伪造计数**。仅凭 `swapgs` 名字不能断定类别，但 FST 退休顺序（同一 `entry_SYSCALL_64`
入口 + 屏障前用户侧 `syscall` marker + sysnum 228 与窗口内两次一致）构成 syscall 归属的
充分证据。备选（更保守）：显式丢弃首个用户 commit 之前、无 pending 归属的跨屏障内核尾，
使其不计入分母；但这会改变 core 5 的 CPI 分母，语义上不如 latch 精确，未采用。

### 9.5 实施（2026-09-17 续）

已在 `tmp/se-fs-paired-c4-20260820/gem5-se-isolated/src/cpu/o3/probe/tao_trace.cc`
落地上述 latch，逐字节镜像 page-fault 先例：

- **头文件**（`tao_trace.hh`）：紧随 `premeasurement_page_fault_*` 后新增
  `bool premeasurement_syscall_pending_ = false;` 与 `uint64_t
  premeasurement_syscall_sysnum_ = 0;`。
- **置位**（`onCommit`）：warmup 且 `!functional_measurement_started_` 时，一条在
  `!cplMeasurementActive()` 下退休的用户 syscall 边界，除照旧发 functional syscall
  marker 外，记 `premeasurement_syscall_pending_=true` 与该边界的 `RAX` 作为 sysnum。
- **清除**（`onCommit`）：屏障前任一**非 syscall 宏op**的用户 commit 清除 latch（syscall
  宏op 自身的尾随用户 uop 不清除），证明其处理程序已返回用户态。
- **消费**（`accountCplCommit` 3030-3070）：在首个测量内 `USER→kernel` 转移处，若
  `cpl_syscall_pending_` 为假但 `premeasurement_syscall_pending_` 为真，则把该 leading
  段判为 SYSCALL 并取 `premeasurement_syscall_sysnum_`；随后清除 latch。该路径逐字节
  复用既有 `cpl_syscall_pending_` 分支语义：**只 `++cpl_entries_[SYSCALL]`，不增
  `cpl_syscall_boundaries_`**，故 CPI 分母 `n_user = cpl_user_functional_uops_ +
  cpl_syscall_boundaries_` 与 `tick_sum_matches`/span 一律不变；1242 commits / 532
  cycles 仅从 UNKNOWN_KERNEL 的 cycles/commits/entries 平移进 SYSCALL 同名字段，两者均为
  守恒求和字段，`validate_kernel_events_oracle.py` 的周期守恒与 unknown 比例门禁自然维持。

首轮重跑暴露一个**既有潜在（此前从未被触发）**的守恒缺陷：`merge_kernel_events_oracle_v3.py`
与 `validate_kernel_events_oracle.py:427` 要求 `sum(syscall_profiles[].count) ==
event_counts.syscall`（即 `cpl_entries_[SYSCALL]`）。原 `cpl_syscall_pending_` 转移分支
（3030）只 `++cpl_entries_[SYSCALL]` 却从不增 `cpl_syscalls_[sysnum].count`——该 count
仅在 boundary 分支（3169）随 syscall 宏op 退休时递增。以往边界从不切在 syscall 中段，转移
分支的 SYSCALL 归属从未被走到，故此不一致长期潜伏；本 latch 使其首次可达，core 5 出现
`event_counts.syscall=3` 而 `syscall_profiles[228].count=2`，merge 报
`per_core[5] syscall profile counts do not conserve`。修复：转移分支开 SYSCALL 帧时同步
`++cpl_syscalls_[sysnum].count`，与单次 `++cpl_entries_[SYSCALL]` 对齐——**仍不动
`cpl_syscall_boundaries_`**（syscall 宏op 未在窗口内退休，不应进 CPI 分母）。此改同时修正了
原生 `cpl_syscall_pending_` 分支的同源潜在不守恒。

产物：`tmp/uarch-common-end-fix-20260917/regress/stockfish/`（合并二修二进制重跑，
gem5 退出 0，oracle 已生成，确证根因）。§9.5 的三改后需重建含 syscall latch 的二进制并
重跑 Stockfish + astcenc/tealeaf 定向回归，验证 `unknown_kernel_entries=0`、
sysnum 228 承接 1242 commits/532 cycles、门禁通过——见后续小节。
