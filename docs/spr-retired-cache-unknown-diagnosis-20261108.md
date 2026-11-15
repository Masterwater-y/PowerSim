# 小 FS 仍有 unknown 的原因定位

现有分类规则已经实现，但规则所需的服务结果没有在所有实际完成路径上正确传递。
本次通过原始 FS 样本、实际生成的 SLICC 代码和生产 C++ 组件的隔离复现，
确认两个会丢失主事件结果的缺陷，并复现一个 Request/TBE 表回收缺陷。
这是原因定位；尚未修改模拟器源码或完成修复后 FS 验收。

## 1. 普通 L1 hit：完成回调早于结果写入

`MESI_Three_Level-L0cache.sm:1313–1318` 的驻留 Load 路径依次执行：

```text
h_load_hit → Sequencer::hitCallback → noteHierarchyCompletion
uu_profileDataHit → perfCacheNativeLookup → sprLookup
```

`noteHierarchyCompletion` 在 `taotrace_response.hh:637–648` 立即读取结果、
转换事件、标记 consumed，并在未被 TBE retain 时移除当前条目。
后面的 `sprLookup` 在找不到条目时直接返回（同文件 698–706）。
因此真实驻留 hit 到达完成点时，三个层级仍是 Undecided，三个事件一起 unknown。
旧账本在后续 action 中仍可按 context/sequence 写入 hit，形成“旧诊断有 hit，
新主事件没有结果”的现象。

生成的 `build/X86/mem/ruby/protocol/MESI_Three_Level/L0Cache_Transitions.cc:259–267`
也保留了这个调用顺序，问题不只是源文件文字上的推测。

例如 mode 0 core 0 的样本 sequence=1298076、request=400993：
`resolved=true`，admissions=responses=1，attempt/completion ID 均为 400993，
旧 hierarchy 的 hits=1，但 `spr_failed_level_results=[0,0,0]`、
`spr_missing_levels=7`。这里 `U-INCOMPLETE-LIFECYCLE` 实际指新结果不完整，
不能解释为真实内存请求未完成。

mode 0 的四核计数逐核对应：

| core | 主事件 unknown（L1/L2/L3 各自） | U-INCOMPLETE-LIFECYCLE | 旧直接 L1 hit | U-UNSUPPORTED-ISA |
|---|---:|---:|---:|---:|
| 0 | 3665 | 3660 | 3660 | 5 |
| 1 | 3721 | 3717 | 3717 | 4 |
| 2 | 3599 | 3596 | 3596 | 3 |
| 3 | 3626 | 3624 | 3624 | 2 |

每个主事件合计 14,611 unknown，其中 14,597 的数量与旧直接 L1 hit 逐核一致。
三个事件的 unknown 不能相加当作三倍独立 load 人口。

## 2. Request 扩展路径漏拷贝新结果

`tao_trace.cc:3535–3536` 将 `facts.sprMiss = native.sprMiss` 放在仅适用于
`TaoTraceNativeAccessRegistry::Snapshot` 的 `if constexpr` 分支内。
实际退休还有直接从 `TaoTraceRubyResponseExtension` 取结果的路径
（同文件 3776–3784）。扩展中即使已经有完整转发结果，投影到退休事实后
仍得到默认空 Aggregate，三个事件均 unknown。

隔离复现使用生产 `perfRetirementNativeFacts` 模板，得到：

```text
Request extension 的转发结果 unknown=0
通过 extension adapter 后 unknown=7
通过 Snapshot adapter 后 unknown=0
```

mode 2 原始样本包含 `store_forward=1`、`resolved=true`、没有 Ruby 请求，
却 `spr_unknown_mask=7` 的情况，与该缺陷相符。
mode 2 除旧直接 L1 hit 外，还存在转发/合并等额外未知，不能全部归为普通 hit。
具体哪些额外样本来自扩展投影、哪些缺少合并点观察，仍须分别核验。

## 3. 被替换的 attempt 没有结束，表可随执行长度增长

同一 Packet 重发时，`perf_cache_native.hh:112–120` 会删除旧事务映射，
再生成新 attempt ID；旧 SPR 条目未同步结束。
新条目最终完成时，只消费新 ID。`sprRelease` 也只在 consumed=true 时移除条目，
没有完成回调的旧 ID 因而可长期留在 `sprCurrent_`。
弱 Request 指针失效本身不会使 unordered_map 的条目消失。

实际 Sequencer 在有序 store 回调被延后、或请求需要重发时确实会重新调用
`issueRequest`（`Sequencer.cc:592–623`）。

生产组件隔离复现中，每次只保留一个 Packet，反复替换旧 attempt，
完成新 attempt 并清除旧退休账本，仍在完成 65,535 个请求后触发 65,536 上限。
对照输入连续完整关闭 70,000 个请求则不溢出。这证明上限本身不是原因：
没有结束的历史条目可以占满本应只保存活对象的表。

mode 1 的实际 FS 确认在该表上溢出，但现有失败回执未输出表内残留分类。
上述泄漏机制已复现；它在本次 mode 1 中的具体占比、是否还有其他遗漏释放路径，
仍须增加容量/结束原因诊断后确认，不能宣称已解释全部 65,536 个条目。

## 4. 证据边界及后续修复

| 判断 | 状态 | 证据 |
|---|---|---|
| 驻留 hit 结果写入晚于完成转换 | CONFIRMED | FS 样本、生成状态机代码、生产组件顺序对照 |
| 扩展投影丢失 SPR 结果 | CONFIRMED | 生产模板隔离复现；mode 2 转发样本相符 |
| superseded attempt 可累积占满表 | CONFIRMED | 生产组件循环复现与完整关闭对照 |
| 上述泄漏是 mode 1 全部残留的唯一来源 | INCONCLUSIVE | 实际溢出回执没有条目分类 |
| 已解决所有合并/资格未知及零 unknown 验收 | INCONCLUSIVE | 尚未修复和重采集 |

隔离复现命令回执：`1a9caee34afcbfe414fb1a92cfdad67d`，四项检查通过。
复现只改变测试输入顺序，未修改生产逻辑；“先 lookup 后完成”的对照
不是修复后 FS 结果，也不证明观察器时序中性。

修复应在实际服务点、完成之前写入 observer-only 结果；两种退休投影都需保留
已有的 SPR Aggregate。替换、取消和服务结束应显式关闭对应活对象身份，
同时保留仍由 TBE/消费者持有的服务结果。不能靠提高容量或退休时补造 hit 解决。
mode 0 的 14 个资格未知和 mode 2 的合并点缺观察也需要独立检查。

原始证据位于 `tmp/spr-retired-cache-v2-20261107-build/` 下
`smoke-mode0-codex-retry/`、`smoke-mode2-codex/` 的
`tao_trace/oracle/perf-cache-core*.json`、
`tao_trace/oracle/perf-retirement-core*.json`，以及
`smoke-mode1-codex/capture.log`。沿用这些原始源，没有用代理计数替换主事件。
尚未进行四负载、中性或同源 FastSim 评分；硬件等价保持 unverified。
