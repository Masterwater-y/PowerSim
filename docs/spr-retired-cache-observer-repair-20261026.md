# SPR 退休 cache miss 观察器改造：实施与验证记录

日期：2026-10-26。验收目标是 SPR `MEM_LOAD_RETIRED.L1_MISS/L2_MISS/L3_MISS`（D1/08、D1/10、D1/20）。三个主事件要求完整功能人口、参考 unknown=0；不再把 known 子集差值当成完整 PMU 精度。

## 当前实施状态

上一轮只完成了事件合同和真机探针，生产观察器仍未补齐。这一轮已经修改实际 gem5 producer、FastSim consumer 和独立审计，重新编译并启动真实八核采集。单测、完整源覆盖和硬件等价是三个不同的验收层次，不能互相替代。

第一版诊断采集定位出剩余路径后，第二版 FLAC 和第三版 Zstd 已使用匹配的新 checkpoint 完成重采。两个八核负载的 gem5 / FastSim 三个主事件 unknown 都为零，逐核人口及独立身份通过，联合维护校验2/2通过。可以报告本次两源的模拟器间误差；更广负载/ISA覆盖和完整SPR硬件等价仍未认证。

证据目录：`tmp/spr-retired-zero-unknown-20261026/implementation/`。先前已接受的基准、旧二进制、原生观察/失败日志与 checkpoint 身份保留。本轮被新源替代的24个诊断FST热文件已按SHA256删除清单清理，原生观察、边界和日志保留。第一版 gem5 SHA256：`20aa293131092faa102443bc5fc5f01eb4bd1c2c7cbdfa51f21159884c3ea54a`；第二版见 `second-producer/producer-identity.json`。

## 实际修改

1. **完整 store forwarding**：根据 LSQ 已证明覆盖整个地址/长度的 terminal reason，独立事件知道这次 load 没有退休 cache miss。保留 forwarding 标签及历史诊断，不把早先发出后丢弃的请求当作最终数据来源。
2. **同核 Sequencer follower**：使用自己的 native 服务事实；没有独立层级访问的 follower 不复制 parent 的三个 miss。一个宏指令有直接访问和 follower 两部分时，仍保留直接片段证明的 miss。
3. **层级守恒**：检查直接上层 miss 数和下层访问数是否一致。不能因为下层局部计数自洽，就把缺失的一片解释为已知 non-miss。
4. **LLC typed 服务**：保留 native `RemoteSupply` 和 `MergedMiss` 的不同类型。MT 的远端 owner/HitM 按共享 L3 snoop hit 映射；IS/ISS 中无驻留数据的合并访问按发起核的 LLC miss 映射。没有修改 Ruby 请求原语或原生 tag miss 总数。这仍需目标硬件微核扩展验证，零 unknown 不代表这项硬件映射已全面认证。
5. **软件预取**：SPR 真机补测与官方字典确认，支持的 ISA hint 在退休 load 口径中计入 load / L1 hit，三个退休 miss 为零；它的异步填充仍可有真实请求 miss。native ledger、独立人口扫描和 FastSim 接受前缀按同一新政策处理。FST feature128 绑定新政策；无该位的历史 trace 保留旧 demand-only 人口。
6. **外部硬件入口微码**：仅在精确已知 x86 ROM 外部入口、且没有 decoded macroop 时，证明它是硬件入口而不是普通 x86 memory instruction。保留所有执行工作、依赖、cache 状态影响和结构性宏/UOP计数；其内部 IDT/stack 访存不计退休 memory instruction。任意 ROM continuation 仍不能据此排除。单独输出 `excluded_hardware_entry_macros`。
7. **原生事实定位信息**：新增有上限的 `native_service_diagnostics`，按 unknown mask 分组计数和采样，记录实际服务原语。另保留不通过人口门禁的 raw observation，明确标为 diagnostic；审计和评分不从 raw 回退取真值。
8. **主评分门禁**：`--retired-cache-misses` 拒绝原生资格 unknown、人口/范围缺口；不能删除字段、替换为请求 miss 或缩小 scope 通过。

## 纯功能 `.fsem` 接口

FST v7 feature bit128 声明必须有 `<trace>.fsem`，而且绑定新退休人口政策。56-byte header：magic `FSTRETV1`、version1、core、完成标志、record/row 数、全 hot stream FNV64、row size16。稀疏 row 包含 ordinal、hot-record FNV32、hardware-entry origin。空稀疏表也是完整声明，缺文件不能解释为空表。

该接口只传递指令来源，不包含 native hit/miss、请求时间或 oracle 计数。QEMU / DynamoRIO 后端可以声明用户指令流、支持的 hint 与硬件入口来源；无需依赖 gem5 的 cache 观察结果。

接入链路：gem5 writer → BinaryTraceSource → 切片/预热包装 → chunk 中被接受的退休前缀 → FastSim event ledger。升级工具保留资格元数据。Python 独立扫描、C++ / Python 输入内容身份和 maintained validator fingerprint 都绑定 `.fsem`。

Python 审计校验全 hot stream，而不仅是 sparse row：小样本直接 FNV；大 FST 使用 `build/fastsim_fst_stream_hash`。缺 helper 拒绝，不跳过。helper 在默认 CMake 构建中生成。

结构性指令人口仍保留功能微码工作，用于已有两阶段执行/边界核对；这里没有通过改 CPI 分母修复误差，也没有认证其等同于真机 `INST_RETIRED` 的完整宏指令人口。因此不能直接把结构性分母包装为已经认证的 SPR MPKI 分母。

## 第一版真实采集的剩余问题

八核、八内存通道、buffer114、KVM → 新 checkpoint → detailed warmup → stat、first-core-target common end；目标为 2M user records。下表是完整 workload-task 范围中的诊断人口，不是完整精度结论。

| 负载 | 退休 load 人口 | L1 unknown | L2 unknown | L3 unknown | 人口门禁 |
|---|---:|---:|---:|---:|---|
| 777.zstd_r rate ×8 | 2,259,598 | 72 | 72 | 119 | core5 有8次宏边界不连续，整组不通过 |
| 817.flac_s speed 8 workers | 1,589,360 | 4 | 4 | 7 | 八核人口检查均通过；事件覆盖仍不完整 |

FLAC 五个 worker 已有三个 miss 全部零 unknown，两个 worker 各有1/2个 L3 unknown；主核还有4个 ROM 入口。Zstd 的剩余项包括 ROM、native terminal 和 LLC 服务，以及 core5 边界。不能用 worker 子集替代整组。

第二版的定位目标就是这些剩余类别；必须看真实新采集结果，不能由单测推断全部解决。

## 回归与独立审核

- 完整 `cmake --build build -- -j16` 和 `./build/fastsim_tests` 已通过一次含功能资格的构建；review fix 后再次运行并通过，日志 `tests-review-fixes.log`。
- native 资格、审计、历史软件预取、输入身份、门禁等81项定向回归通过，日志 `native-and-policy-regression.log`。
- 新功能 wire / runtime 用例覆盖内部硬件入口成本保留、普通 load 不被排除、必需文件、core/order/row/hash 绑定；新旧预取政策、wrapper/升级和大流 helper 回归见 `functional-retirement-review-tests.log`，以最终日志为准。
- 独立审核发现两个 Important：Python 未校验全流哈希；旧 trace 软件预取被无声改人口。均先复现失败，再修复；不以文档说明代替代码门禁。

## 尚未完成

本轮两源的重采、人口/身份/资格和同源回放已完成。尚待其他负载及更多ISA/复杂分片路径覆盖、SPR更完整的硬件语义认证，以及Zstd LLC等实际模型残差分析；旧FST不能靠改报告升级为新观察器已验证数据。

## 第二版 FLAC：完整人口与同源 consumer 复验

第二版真实 FLAC speed C8：八核人口门禁全部通过；三个主事件的 gem5 / FastSim unknown 均为零，模型 point-estimate fallback 为零。独立身份核验通过。维护入口结果见 `second-producer/validation-flac-complete-service/`；`passed` 证明源/人口/合同核对通过，仍不是硬件等价或误差阈值认证。

| 指标 | gem5 | FastSim | 相对误差 | CPI绝对误差 cycles/宏指令 |
|---|---:|---:|---:|---:|
| workload-task active CPI | 0.44565836 | 0.49556230 | +11.1978% | 0.04990394 |
| elapsed 时间 | 137.092103 μs | 152.443404 μs | +11.1978% | — |
| L1退休load miss | 14,757 | 15,166 | +2.7716% | — |
| L2退休load miss | 23 | 33 | +43.4783% | — |
| LLC退休load miss | 10 | 11 | +10.0000% | — |

只有一个样本，CPI MAE=0.04990394 cycles/宏指令。L2/LLC绝对事件很少，分别只多10/1次；这轮适合验证覆盖和口径，不能据此给出稳定的多负载平均精度。该源与旧10M采集窗口不同，CPI误差不能当成同源5%→11%的模型回归。

同一份 FST / 配置的 consumer A/B：补齐 full forwarding / Sequencer follower 和 typed shared service 后，旧 qualified unknown 112,147/112,147/112,156 变为0/0/0；LLC点估计20→11，参考10。远端供给按共享L3 snoop hit处理，不能当DRAM/LLC miss。CPI、elapsed、trace人口和原有请求PMU逐对象完全相同，见 `second-producer/flac-observer-timing-neutrality.json`。这里是统计口径修复，不是通过改 target 时序拟合结果。

测量阶段吞吐2.7076 MIPS；含功能预热的端到端吞吐2.8404 MIPS。输出中的 `mips=0.34647` 是“测量指令数 / 含预热总墙钟”，不能当成测量阶段吞吐。此处只报告该次实测，不宣称优化加速。

## 第三版 Zstd：权限和精确缺页前缀

第二版 Zstd剩余37/38/38个未知，原生样本表明主要是权限升级，而不是数据不在cache。第三版增加分层permission-upgrade事实：本层驻留数据可证明hit，下层ownership流量继续作为请求诊断。混合分片同时有L2 ownership与真实data miss时，缺少LLC逐片归属不能设置L3正位来覆盖unknown；当前保守拒绝这类未证明的成员，不能伪造zero。

Zstd core5的11个不连续点来自MOVUPS分成两个store micro-op，第一片已提交、第二片触发精确缺页。KernelEntryEvent传递接受点的context/PC，只有匹配open prefix且随后进入精确external-ROM/no-macroop入口时，记录真实fault abandonment。保留执行成本与未完成前缀尾账，避免该未退休load/store被并入入口或重试指令。

`.fsem` version2支持 origin bit1（value2）表示精确fault后的前缀中止；必须同时有hardware-entry bit0。native producer、FST读取/包装/升级、FastSim接受前缀和Python独立boundary证明已接入；version1保持兼容。无证明的不连续仍失败。第三版已用匹配checkpoint完成Zstd重采和维护回放。八核人口检查全部通过，core5的11个精确缺页中止前缀被正确计入，异常边界计数为0；gem5与FastSim三主事件unknown均为0。

最终回归：84项定向Python测试、9项功能wire/runtime测试、完整FastSim测试、退休及cache集成测试均通过。独立审核发现并修复了mixed ownership/data正位掩盖未知的问题，ledger级见证RED→GREEN。保留一个非生产默认API诊断细节：直接Ledger调用省略miss_estimate时会保留fallback标志，生产路径显式传入完整参数，实测主事件fallback=0。

## 最终两源结果

维护入口通过 `--source-root` + `--stats-dir` 对已生成输出重新核验并汇总，未重新选择参考：`final-two-case/validation/`，2/2 passed。此次重评分不算新的吞吐测量。

| 负载 / 核数 | gem5 active CPI | FastSim active CPI | CPI相对误差 | CPI绝对误差 cycles/宏指令 |
|---|---:|---:|---:|---:|
| Zstd rate ×8 / C8 | 1.35483688 | 1.30517986 | −3.6652% | 0.04965701 |
| FLAC speed 8 workers / C8 | 0.44565836 | 0.49556230 | +11.1978% | 0.04990394 |

两例 CPI MAPE **7.4315%**，CPI MAE **0.04978047 cycles/宏指令**。没有剔除异常样本。

| 负载 | 主事件 | gem5计数 | FastSim计数 | 相对误差 | gem5 / model unknown |
|---|---|---:|---:|---:|---:|
| Zstd C8 | L1 miss | 256,867 | 274,251 | +6.7677% | 0 / 0 |
| Zstd C8 | L2 miss | 54,007 | 58,526 | +8.3674% | 0 / 0 |
| Zstd C8 | LLC miss | 20,867 | 25,799 | +23.6354% | 0 / 0 |
| FLAC C8 | L1 miss | 14,757 | 15,166 | +2.7716% | 0 / 0 |
| FLAC C8 | L2 miss | 23 | 33 | +43.4783% | 0 / 0 |
| FLAC C8 | LLC miss | 10 | 11 | +10.0000% | 0 / 0 |

| 事件 | MAPE | WAPE |
|---|---:|---:|
| L1 miss | 4.7696% | 6.5506% |
| L2 miss | 25.9228% | 8.3824% |
| LLC miss | 16.8177% | 23.6289% |

Zstd仍有78个有界影子表近似标记，但三个事件原始成员资格unknown为0，所有退休load均参与统计，qualified和点估计总数一致。这是模型时钟/容量近似，不是删除无法分类的成员。Zstd LLC参考已有20,867次，+4,932次的残差需要分析模型，不能归结为低计数百分比波动。

Zstd elapsed：gem5494.925578 μs、FastSim476.785737 μs（−3.6652%）；测量阶段2.1868 MIPS，含预热端到端2.1384 MIPS。二者均是本次实测，不表示全矩阵吞吐。

本轮清理24个已替代诊断FST，释放13.463 GB，记录 `superseded-diagnostic-fst-cleanup.json`。最终FST热记录总量109.897 GB，低于200 GB上限；剩余磁盘318.667 GB，记录 `final-storage.json`。保留原生诊断、边界、checkpoint以及最终两份接受源。
