# FastSim FS 配置固化、I-side 守恒审查与剩余 CPI 误差

日期：2026-08-17

## 1. 本轮结论

本轮修复了一个部署层面的真实缺陷：此前 DTLB timing walker、TreePLRU 和
page-fault cache-state 等代码/模型已经存在，但正式可复现的 FS 配置并没有完整
保存在受维护的 `configs/` 下。直接使用共享 base config 会重新落回
`se_atomic`，并漏掉 page-fault cache-state 配置；之前的正式结果依赖 `tmp/`
中的派生 overlay。因此不能简单概括为“以前的代码修改都没生效”，准确说法是：

- 模型代码在使用正确实验 overlay 时确实生效；
- 默认/文档化入口没有完整携带已接受的 FS profile，存在配置漂移；
- 旧结果难以只凭受维护文件复现，这一点现已修复。

新增并设为 FS 工具默认值的两个 profile 是：

```text
configs/gem5-v28_1-fs-user.cfg
configs/gem5-v28_1-fs-user-plus-kernel.cfg
```

它们通过 `config.include` 继承共享微架构，再显式冻结：

- `dtlb.miss_model=timing_walk`；
- C4 校准、C8 冻结的 12-cycle effective walker service；
- user/user+kernel 各自接受的 page-fault cache-state 与 kernel event profile；
- FST 虚拟页 token 合同。

共享 `gem5-v28_1-time-epoch.cfg` 仍保留 `se_atomic`，但现在明确只用于
SE/control。所有受维护 FS 入口默认走 `timing_walk/12`。

## 2. 配置修复是否真的复现旧结果

正式复跑输出：

```text
tmp/committed-ledger-repair-20260817/formal-default-profiles/summary.json
```

两个 profile 的 SHA256 为：

```text
user:        e30af0950f6d820e53f7d0f9da54c3660ed52bfa4da630ce24d5b52518aac82f
user+kernel: 1e034577bc0375c6b8c2a71528161dcfc6c74b902578c66d4f546d07697b2602
```

将新默认 profile 的 20 case × 2 scope predicted CPI 与此前接受的、由实验
overlay 产生的 TreePLRU 正式结果逐项比较：case 顺序完全一致，最大 CPI 差为
**0**。这证明本轮不是重新拟合，而是把此前散落的有效配置固化到受维护入口。

相关实现包括：

- `src/config.cpp`：相对路径 `config.include`、overlay 覆盖和循环检测；
- `tests/test_main.cpp`：include/override 定向测试，临时文件写入项目 `tmp/`；
- `tools/run_fst_v7_formal_inference.py`：双 scope 默认 profile、路径与 hash
  provenance；
- `tools/run_kernel_event_accuracy_pipeline.py`、`tools/validate_fs_c8.py`、
  `tools/compare_fs_trace_driven.py`、`tools/audit_fs_committed_pipeline.py`：FS
  默认或固定使用 `timing_walk/12`。

`tools/validate_tcsim_c4_c8.py` 中仍有可选 DTLB override，但它是通用
TCSim/SE 验证入口，不是受维护 FS 默认路径。当前搜索不到任何受维护 FS
入口默认选择 `se_atomic` 的路径。

## 3. 当前正式 APE

以下数值来自新默认 profile 的完整 20-case、双 scope 复跑：

| workload | C4 user | C8 user | C4 user+kernel | C8 user+kernel |
|---|---:|---:|---:|---:|
| Stockfish | 23.09% | 20.26% | 25.04% | 21.88% |
| omnetpp | 10.46% | 11.02% | 11.05% | 11.76% |
| zstd | 3.60% | 4.79% | 3.42% | 1.50% |
| LBM | 7.02% | 2.78% | 5.50% | 1.93% |
| SPH | 4.67% | 6.85% | 10.95% | 11.26% |
| TeaLeaf | 6.01% | 12.58% | 5.43% | 12.44% |
| NAb | 8.76% | 12.45% | 7.40% | 11.04% |
| Graph500 | 7.46% | 3.12% | 10.74% | 0.74% |
| NAMD | 13.57% | 3.75% | 13.34% | 17.98% |
| Neutron | 0.44% | 2.49% | 0.47% | 2.70% |
| **mean** | **8.51%** | **8.01%** | **9.33%** | **9.32%** |

因此当前最大尾部仍是 Stockfish，不再是 Neutron。DTLB 修复已经解决了
Neutron 主误差，但不能解释 Stockfish、NAMD combined scope 或 TeaLeaf/NAb
C8 的残差。

## 4. committed trace 下的 I-side 能建模到什么程度

结论是：可以建模 committed-PC 产生的 I-cache/取指块下界，也可以建模
fetch/decode 带宽和已知分支恢复；不能从输入中唯一恢复 gem5 的完整 I-side
request stream。缺失信息包括：

- wrong-path PC 序列及其取指块、ITLB、L1I 请求；
- squash 后的 refetch 次数和时点；
- wrong-path decode/rename/ROB/IQ/LSQ 占用；
- page-table request 和 Ruby 中的 I-side transient 状态。

为了判断当前 committed frontend 是否漏算了一个可直接补上的固定等待，本轮
新增了守恒 ledger：

```text
fetch block request -> response -> fetch resume
response wait = hidden wait + exposed wait
```

aggregate 和每核都输出五个原始 cycle counter，并要求上述等式成立。完整 C4/C8
审计全部通过。Stockfish 的 workload-equal 结果是：

| case | gem5-FastSim CPI gap | request→response wait/uop | hidden | exposed | response→resume |
|---|---:|---:|---:|---:|---:|
| C4 | 0.072126 | 0.046513 | 0.000511 | 0.046001 | 0.002264 |
| C8 | 0.060132 | 0.060381 | 0.000404 | 0.059977 | 0.001316 |

C4/C8 的 committed response wait 已有 98.9%/99.3% 暴露在时序关键路径上。
因此把 fetch refill 从 1 改成 2 虽能数值上抬高 Stockfish，却是在同一已暴露
等待上再收费。该候选同时回归 NAb、Graph500、NAMD，不能作为修复。

更强的反证是，在只看 FastSim 欠预测行时，exposed response wait/uop 与 CPI
gap 的 Spearman 相关系数为 C4 **-0.329**、C8 **-0.366**。如果缺失机制只是
“每个 committed fetch block 再多等若干拍”，方向应为正。

实验性 committed-PC L1I 对 pooled mean 有小幅改善，但只覆盖 Stockfish
C4/C8 gap 的 1.18%/1.59%，并使 Graph500 C8 回归。它只能作为 committed-path
下界/诊断，不能默认开启并宣称与 gem5 I-side 等价。此前的 state-only
wrong-path pilot 也没有通过跨负载 gate。

所以在“输入永远只有 committed functional trace”的约束下，精确 I-side
equivalence 是不可辨识的。若不扩展输入，只能保留保守下界或明确标注为统计
近似；若要求 gem5 baseline 语义对齐，必须增加非 timing-oracle 的 fetch-side
contract，例如 producer 输出的 fetch/refetch/wrong-path request stream，或至少
可完整解码的代码映像、地址空间映射和动态控制流恢复所需状态。

完整 ledger 证据：

```text
tmp/committed-ledger-repair-20260817/frontend-ledger/c4/summary.json
tmp/committed-ledger-repair-20260817/frontend-ledger/c8/summary.json
```

## 5. 微架构参数是否已经“完全对齐”

没有，当前代码不会作出这个声明。新增的
`tools/audit_fs_profile_identity.py` 同时比较 gem5 `config.ini` 与 FastSim report
中的 effective configuration，并把参数相等与状态机语义等价分开报告。

当前审计结果：

| gate | 结果 |
|---|---:|
| 直接/派生表示的字段 | 129 |
| 数值匹配 | 121 |
| 数值不匹配 | 8 |
| direct parameter gate | FAIL |
| full semantic equivalence | NO |

8 个差异都是目标 DDR4 command 约束：

```text
activation_limit=4
tRAS=96, tRTP=23, tRRD=11, tRRD_L=15
tXAW=64, tCCD_L=16, tCS=5
```

FastSim 代码有对应的 default-off 候选字段，但当前 command calendar 作用在
重建的 controller arrival/order 上，并不等价于 gem5 的完整 DDR controller；
它还缺完整 read/write bus turnaround、refresh 和部分 command/state 规则。
仅把八个数填进去会造成“配置看起来相同、状态机仍不同”的伪对齐。

为避免只凭语义判断，本轮隔离启用了 four-ACT 子集并复跑完整 user matrix：

| split | 当前 mean APE | four-ACT mean APE | 变化 |
|---|---:|---:|---:|
| C4 | 8.508% | 8.529% | **+0.021 pp** |
| C8 | 8.008% | 8.016% | **+0.008 pp** |

TeaLeaf C4/C8 分别回归 0.120/0.098 pp，LBM C4 回归 0.090 pp；个别 case
在增加物理约束后 CPI 反而降低，直接说明当前 reconstructed arrival/order
不是 source-equivalent controller 输入。因此这些字段不能为了让参数清单变绿
而默认开启。

除了这 8 个直接差异，审计还显式列出不能用参数相等解决的语义差异：完整
I-side/ITLB、per-level Ruby page walk、wrong-path O3 occupancy、Ruby transient
protocol/network、完整 DDR command protocol、StoreSet/replay 和精确 SQ lifetime。

审计报告：

```text
tmp/committed-ledger-repair-20260817/profile-identity/summary.md
tmp/committed-ledger-repair-20260817/profile-identity/summary.json
```

## 6. 候选模型的默认策略

| 组件 | 当前决策 | 证据 |
|---|---|---|
| DTLB timing walker/12 | FS 默认开启 | Neutron C4/C8 主误差消除；arch/timing 双域守恒 |
| L2/LLC TreePLRU | 默认开启 | gem5 replacement state machine 直接映射；正式矩阵通过 |
| scope-specific page-fault/cache-state | FS profile 默认开启 | 新 profile 与已接受正式结果逐项 CPI 相同 |
| frontend response ledger | 默认统计，CPI-neutral | C4/C8 aggregate/per-core 守恒通过 |
| committed-PC L1I | 保持 experimental/off | 请求流不完整；Stockfish 修复量不足；有跨负载回归 |
| state-only wrong path | 保持 experimental/off | committed 输入不可辨识，pilot 未过 gate |
| partial DRAM command calendar | 保持 experimental/off | controller arrival 未闭合；P90/tail 未解决；four-ACT 回归 |
| finite committed rename free list | 不默认开启 | 全部 formal core 的 stall 为 0，CPI bit-identical |
| refill latency=2 | 拒绝 | 与 response ledger 重复收费，且跨负载回归 |

更完整的候选结果见
`docs/fs-cpi-candidate-model-audit-2026-08-17.md`。

## 7. 剩余误差的组件归因与下一步

现有证据不支持一个统一全局 scalar。剩余误差应按组件推进：

1. **Stockfish：缺失的 speculative frontend/O3 语义。** committed fetch
   response 已基本全部暴露，L1I 容量模型修复量不足，rename free-list 又是零
   stall。仅凭现有输入不能构造 source-equivalent wrong path；在输入合同不扩展
   时，应把这部分列为不可辨识边界，不能用 refill 常数掩盖。
2. **TeaLeaf/LBM：controller arrival/read-write service。** 它们对 DRAM
   command 候选敏感，但错误方向的 four-ACT/FR-FCFS 结果说明应该先实现并守恒
   `request created -> controller enqueue -> select -> command/bus -> response`
   ledger，再补完整 DDR4 状态机，最后逐项开启参数。
3. **NAMD/SPH/TeaLeaf combined scope：kernel profile/timing。** NAMD C8 user
   APE 只有 3.75%，user+kernel 却为 17.98%，无法由 user-path cache 开关修复；
   需要按 syscall/page-fault/IRQ 的 event-to-resume 和 response-to-retire ledger
   单独验证。
4. **NAb 和部分 OoO residual。** gem5 raw ROB/rename stall 在 C8 欠预测行上
   有相关性，但这些统计不是合法输入。下一步只能用 committed trace 可生成的
   occupancy/response ledger 找因果机制，不能把 gem5 counter 拟合成在线惩罚。
5. **Graph500/DTLB residual。** 固定 12-cycle service 仍是 effective model；
   长期应使用 per-level Ruby walk request/response sidecar 或可重建地址，替代
   固定 service，并保留 architectural/timing 双状态域。

因此下一项可在现有 committed trace 合同内继续做、且最有希望通过 baseline
对齐 gate 的工作，是完整 controller arrival/DDR ledger；精确 I-side 修复需要
先改变输入合同。

## 8. `/tmp` 清理与验证

已将根 `/tmp` 下确认属于 FastSim/本轮 FS 对齐的 505 个历史条目（约 14 GiB）
移动到：

```text
tmp/legacy-root-tmp-20260817/
```

原匹配路径复查为 0。`docs/codebase-workflow.md` 也已从根 `/tmp` 改为项目
`tmp/codebase-sync` 和 `tmp/git-recovery`；测试临时文件使用
`tmp/fastsim-tests/<pid>/`。Python `NamedTemporaryFile` 使用点均显式绑定输出
目录，TCSim validation 的 `TemporaryDirectory` 绑定项目 scratch 目录。

本轮验证命令：

```bash
cmake --build build -- -j16
./build/fastsim_tests
```

正式 20-case 双 scope 复跑、profile identity 审计、frontend ledger 审计和
four-ACT 隔离实验的产物都位于
`tmp/committed-ledger-repair-20260817/`。
