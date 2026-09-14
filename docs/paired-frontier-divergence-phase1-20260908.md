# 成对 frontier 首差异与 store 生命周期定位（2026-09-08）

本轮执行
[成对事件账本与建模优化方案](paired-event-model-plan-20260908.md) 中“先定位边界状态
修复后的首个 shared-order/response 分歧”这一门禁。目标不是为一次局部提前追加补偿，
而是把 `memory path → shared DRAM order → 跨核 response → SQ/TSO` 的传播链闭合。

## 1. 实施内容

`core.response_frontier_audit_stride_uops` 开启时，FastSim 现在为每个活跃 SQ 槽位额外
保存一个 release owner。审计样本中的 `incoming_sq_release_sequence/cycle` 因而在
`core.response_paired_frontier=false` 时也有效；位移字段为 0。该状态大小固定为
`cores × sq_entries`，只参与诊断输出，不参与调度。不开启 frontier audit 时不分配这份
状态，生产默认路径不变。

新增 `tools/analyze_paired_frontier_divergence.py`，可同时读取多组成对 audit stats：

- 先校验相同 core/sequence 的 PC、load/store、依赖距离和内存事件身份；
- 用每个样本自己的 `interval_gap_cycles` 归一化本核时刻，寻找首个语义差异；
- 另保留 candidate−baseline 的绝对时刻差，寻找首次提前、首次落后和提前→落后；
- 输出 memory path、DRAM blocker、当前 capacity cause 和直接 SQ owner；
- 明确把组件标签限定为“该样本看到的 gate”，不把窗口开始前已不同的活跃状态误报为
  原始原因。

定向 C++ 测试覆盖跨 checkpoint 的 SQ owner 传递，并验证开启 audit 后 cycles、memory
population 和 LLC population 不变。Python 测试覆盖纯 interval-gap 平移、首次差异和
SQ 提前→落后分类。

## 2. 首差异闭合

在 TeaLeaf LLC32 C4 的 baseline 与完整边界状态回放之间采用 65536→4096→256→1
的逐级采样。三个未直接修复的核在各自开头 5,001 个 UOP 内完全一致；修复核 core 2
在入口开始至 sequence 223951 也逐字段一致。

| 传播位置 | baseline | 边界状态候选 | 结论 |
|---|---:|---:|---|
| core2:223952, kernel load | DRAM path，latency 460，retire 548520 | L1 path，latency 2，retire 548064 | **首个语义差异**；局部提前 456 cycles |
| core2:232538, load | DRAM arrival/command 560703/560871，retire 560997 | 557873/557878，retire 559514 | 原阻塞请求已整体提前，不再占用后面的共享日历 |
| core1:276057, load | issue/arrival 561889/560721，command 560967；blocker=c2:232538 | issue/arrival 相同，command 560871；blocker=c1:275881 | **首个跨核差异**；DRAM 仲裁改变，反而提前 96 cycles |

core1:275861 和 275881 自身在两侧完全一致。core1:276057 的 request issue、canonical
arrival 和功能身份也相同，只有 DRAM command/blocker 及之后的 response/retire 改变。
所以首个跨核传播不是退化，也不是 core1 的 FU、依赖或准入变化；它是 core2 请求提前
离开后产生的 DRAM 日历重排。

## 3. 第一次由快转慢

继续按绝对 retire 时刻找符号翻转，最早观察点是 core3 sequence 292582（PC
`0x4098a0`）：

- fetch/rename 仍比 baseline 早 3,710 cycles；
- 前一条样本 retire 仍早 10 cycles；
- 当前 store 的 dispatch/issue/retire 同时晚 36 cycles，cause 均为 `sq_capacity`；
- 新 SQ owner 账本指出两侧等待的都是 store 291860，但其 release 为
  587094→587130，差值恰为 36；
- 下一次 SQ 槽位阻塞来自 store 291861，落后扩大到 124 cycles。

两个 owner store 给出了可加和的反转账：

| owner | 上一 store drain 差值 | 本请求 DRAM latency 差值 | SQ release 差值 |
|---|---:|---:|---:|
| core3:291860 | −52 | +88（301→389） | **+36** |
| core3:291861 | +36 | +88（311→399） | **+124** |

291860 自身的共享请求仍更早完成，但候选把它放进了不同的 DRAM 相位：canonical
arrival/command 从 585312/585451 变为 581602/581829，排队服务时长增加 88 cycles。
当前默认 store 生命周期随后在 commit/TSO send 边复用这个完整服务时长，并把 response
作为下一条 store 的串行前沿。因此一个更早的共享请求可以产生更晚的 SQ release，之后
再由 SQ capacity 传播到普通 UOP。这里才是完整回归中的第一条已证“由快转慢”链。

## 4. 两个消融与耦合证据

仅开启仓库已有的 `core.store_post_commit_request`，把 store cache/coherence 请求移到
commit 边，边界状态的全核差值从 +55,896 变为 −94,085 cycles。这证明默认的早期
请求/后期 SQ release 混合确实是回归通道。但该候选把无边界状态的 CPI 从
0.8144051186（相对 gem5 +9.6487%）提高到 0.8360406164（+12.5616%），绝对精度明显
变差。这个结果只说明请求时点还与其他模型误差耦合，不能单凭聚合 CPI 判定
post-commit 语义本身。该开关没有建立唯一 request owner，也没有同时替换 admission、
service、callback 和 release，因此它只是机制消融，不是可推广的完整候选。

随后试过将 post-commit store 的 admission 与 response drain 分离、允许多条 store
保持顺序但不等待上一 callback。该实现把 baseline CPI 降至 0.5169922733
（−30.3940%）。该实现被撤回的直接依据是它违反当前 x86 TSO/gem5 的
single-store-in-flight 约束、制造了不存在的 store 并行度；大幅 CPI 低估是语义错误的
后果和定位信号，不是独立的否决规则。失败实现未留下配置项或生产路径。一次性结果保留在
`tmp/measurement-boundary-state-20260908/shared-divergence/`，只作为停止证据。

**决定：** 保留 SQ owner 审计和离线成对分析器；边界状态、post-commit store 和其他
实验开关继续默认关闭。下一建模步必须原子处理以下职责，不能只移动一个时间戳：

1. store address/data ready 与 commit eligibility；
2. TSO 允许发送、Sequencer 接纳和 single-store-in-flight owner；
3. 唯一 shared request 的 DRAM service/callback；
4. callback 后的 SQ release，以及 fence/serialization 的完整 drain。

每条请求的服务只能消费一次。候选需先复现 291860→292582 的 owner 链，并逐事件对齐
gem5 的 admission、service、callback 与 release；之后用 LLC32/L1D64/Stockfish/
ASTCENC 检查组件交互，最后才评价总体 CPI。不能因为总 CPI 改善而接受违反事件合同的
实现，也不能因为总 CPI 暂时恶化而撤销一条已独立证实的组件修复。

## 5. 复现

合并报告位于
`tmp/measurement-boundary-state-20260908/shared-divergence/paired-divergence-report.json`。
核心命令如下；各窗口 cfg 和 stats 与报告同目录。

```bash
python3 tools/analyze_paired_frontier_divergence.py \
  --pair core2-entry \
    tmp/measurement-boundary-state-20260908/shared-divergence/core2-entry-baseline.json \
    tmp/measurement-boundary-state-20260908/shared-divergence/core2-entry-state.json \
  --pair core1-first-cross-core \
    tmp/measurement-boundary-state-20260908/shared-divergence/core1-first-baseline.json \
    tmp/measurement-boundary-state-20260908/shared-divergence/core1-first-state.json \
  --pair core2-former-blocker \
    tmp/measurement-boundary-state-20260908/shared-divergence/core2-blocker-baseline.json \
    tmp/measurement-boundary-state-20260908/shared-divergence/core2-blocker-state.json \
  --pair core3-first-lag \
    tmp/measurement-boundary-state-20260908/shared-divergence/core3-first-lag-baseline-sq-owner.json \
    tmp/measurement-boundary-state-20260908/shared-divergence/core3-first-lag-state-sq-owner.json \
  --pair core3-sq-owner \
    tmp/measurement-boundary-state-20260908/shared-divergence/core3-sq-owner-baseline.json \
    tmp/measurement-boundary-state-20260908/shared-divergence/core3-sq-owner-state.json \
  --output tmp/measurement-boundary-state-20260908/shared-divergence/paired-divergence-report.json

cmake --build build -- -j16
./build/fastsim_tests
python3 tests/test_paired_frontier_divergence.py
python3 tests/test_measurement_boundary_memory_state.py
git diff --check
```

最终关闭态复跑与改动前的 `scope_metrics/totals/cores/threads/cha/instruction_cha`
逐项一致（比较时去除审计数组、host wall-time/throughput 和并行等待计数）；开启 audit
后的 totals/CHA 与旧二进制也一致。

完整 CPI 回归还没有被修复；本轮交付的是首个回归传播链、无扰动有界 owner 审计和
可复现的成对定位工具。默认模型、维护配置和生产 manifest 未启用任何新候选。
