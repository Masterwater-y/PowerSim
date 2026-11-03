# Buffer TSO 队头 demand 候选实现与小集验证（2026-09-23）

> 版本说明：本文保存首次组合实现及其失败结果。随后按用户要求撤销新增资源调度
> 和整 epoch 回退，当前实现见 [仅补队头 demand](buffer-tso-head-demand-only-20260923.md)。
> 下文数值仍对应原冻结二进制，不代表缩小范围后的版本。

本轮按 [根因分析](lbm-buffer-tso-error-mechanism-20260921.md) 实施现有
`core.buffer_tso_prepare_lifecycle` 开关内的队头 demand，未增加配置旋钮或
调整延迟参数。**候选保持默认关闭；机制测试通过不等于精度门禁通过。**

## 实现和边界

不能把每条 store 都实现为“完整 prepare 完成，再收一次 L1 服务”。现在分三种情况：

| 到达 canWB / FIFO 头时的状态 | 服务与 SQ 释放 |
| --- | --- |
| canWB 时已是头部，前一条 demand 已完成 | 原有 hierarchy replay 就是这条 store 的 mandatory 请求；不额外发 prepare / L1 请求 |
| 原为 follower，成为头部时 prepare 仍在途 | mandatory demand 接管在途服务，SQ 等该服务响应；不再串接第二次 L1 服务 |
| 原为 follower，成为头部时 prepare 已完成 | 以 `write=true`、非 permission-only 再调用真实 `replay_memory_event`；SQ 使用其实际响应 |

第一种情况的原请求在试算阶段保持 clean，最终提交其 dirty/modified 状态；
现有 L1 驻留、独占权限和跨核保护证明这期间没有可观察该差异的访问。
第二种情况对应 gem5 `MESI_Three_Level-L0cache.sm` 的
`SP_IE + Store -> IM`；第一种对应 `writebackStores()` 先发头部 demand，
`issueStorePrepareRequests()` 再扫描未 committed 的 follower。

prepare 保留既有共享层级/MSHR 服务，绕过 mandatory Sequencer outstanding。
mandatory 请求按时序 proposal 中的准入时间排序分配 Sequencer；正式计算后
再用**实际请求区间**认证容量并重建跨 epoch 释放堆。不能把尚未发出的未来
store 回调直接写入按程序顺序消费的释放堆，否则会错误阻塞能先发出的 load。
容量认证失败即整 epoch 串行回退，不提交早于真实响应的槽位释放。

时序 trial 使用 cache/directory 事务，回滚私有缓存、共享状态和计数；被选中的
真实 demand 最终只提交一次。trial 之间重新准备 private-read service 状态。
开启 lifecycle 的反馈求解使用串行事务和额外 proposal，宿主成本尚未通过吞吐门禁。

真实 L1 demand 会更新 replacement 状态，因此新增同组顺序认证：若它不能证明
严格晚于本 epoch 其他不同 line 的同组 canonical 访问，包括同周期 tie，整 epoch
回退。原有跨核、page-fill、atomic、barrier、split 和权限丢失保护保留。
已认证的本地 demand 不推进不相关 shared transient 的过期时钟，防止将未来
store 时间泄漏到下一 epoch 的 canonical replay。

这仍是现有两阶段 canonical 模型内的候选，**不是完整的跨 Q 事件引擎**。
prepare 的 canWB 响应下界仍沿用先前 lifecycle 实现，未重构全部 optional
端口/TBE 的逐时刻调度。命中服务来自现有层级 replay / 配置；此次没有更改
`l1d.hit_latency`，也没有以 CPI 为依据重新标定 store edge。
现有统一 L1 时延仍有近似边界：配置注释把 2 周期定义为 load 的端到端边，
而 gem5 普通 store 在 Ruby callback 内直接 `completeStore`，不经过 load
register writeback。因而本报告不把复用的 2 周期称为独立认证过的精确 store
协议时延；进一步细化须先建立同次采集 `config.ini` 的 store 专属映射。

新增逐核和总计字段：`buffer_tso_prepares`、`buffer_tso_head_demands`、
`buffer_tso_merged_demands`。它们只统计**成功接受的优化路径**，串行回退请求
仍属于既有普通访存人口；不能用 head 字段替代全体 store 数。退休指令、UOP 和
`memory_accesses` 不因额外协议请求增加。L1 accesses 包括原请求与完成准备后的
额外 demand，不能继续假定其等于退休访存行数。prepare 与 demand 的完整分层
PMU 字典仍未拆分，本轮 native cache 评分只保留为诊断，不声明严格 PMU 改善。

## 验证、来源与复现

- 测试先行反例：64 条同址 store 后接 fence，配置 L1 服务为 9；旧实现仅
  203 周期，违反 64 次 FIFO demand 至少 576 周期的下界。最终测试通过。
- `cmake --build build -- -j16` 成功；`./build/fastsim_tests` 输出
  `all FastSim tests passed`。仅有工具链既有的串行 LTO 提示。
- 新增覆盖：immediate head 无 prepare、在途接管、SQ 1/2/8/114、有限
  Sequencer 的混合 load/store、private-read services 组合、跨 epoch、warmup
  计数重置、atomic/serialize/split/跨核回退、同组 LRU 顺序触发整 epoch 回退。
- 关闭态 8/8：`scope_metrics`（去掉宿主 throughput）、完整 `threads` 和
  所有原有逐核字段与修改前精确一致，见 `off-equivalence.json`。
- 核心修改在 `src/simulator.cpp`，字段和 JSON 导出位于
  `include/fastsim/types.hpp` / `src/main.cpp`，测试位于 `tests/test_main.cpp`。

实验根为 `tmp/buffer-tso-head-demand-20260923/`。旧二进制冻结于
`before/build/fastsim`，SHA256 为
`d5f76813a9111d7abb3e3fc614eb586827ad03c64e7e455173e25aed8fc6b45e`；
最终二进制为 `fastsim-final`，SHA256 为
`dbcfd1aa0b53ba0895a657b5177bf331b46e7c716deb919e19f91320980691c8`。
源码指纹见 `final-fingerprints.json`，相对此次工作前快照的差异见
`implementation.diff`，不会混入工作区原有的其他修改。

`source/` 固定 8 个已有 common-end native-FS 案例：LBM C4/C8/C16/C32，
Stockfish、TeaLeaf、Graph500、NAMD C4，均来自
`tmp/tso-buffer114-common-end-baseline40-20260918/formal/source/`。
这 8 案例用于机制选择和控制，属于 diagnostic pilot，不是 workload-held-out
或全 39 案例验收；未重采 gem5、未截短 trace，也未输入 oracle 时刻。
共同窗口、macro 分母、native 用户及内核人口由维护 validator 检查。
独立硬件身份审计为 8/8 valid，保存在 `identity.json`；因 source 使用目录
symlink，审计时显式逐个传入 `--result`，未把递归发现零案例视为通过。

旧/新 replay 的 overlay 仅切换既有开关，关闭 store forwarding 和 store-wait
relief，buffer 大小 114。`dual.cfg` 同时启用 residence certificate 和 lifecycle；
`life.cfg` 仅启用 lifecycle；`off.cfg` 两者都关闭。没有更改 cache/DRAM 时延。
各 case 的输入、配置 include chain、二进制指纹及 native 源均保存在维护验证器的
`cases/<case_id>/validation.json`。并发 4 跑 CPI，几组诊断同时运行；这些 wall time
不能用作 quiet-host 吞吐比较。

```bash
python3 tools/run_native_kernel_fastsim_validation.py \
  --source-root tmp/buffer-tso-head-demand-20260923/source \
  --fastsim tmp/buffer-tso-head-demand-20260923/fastsim-final \
  --config tmp/buffer-tso-head-demand-20260923/dual.cfg \
  --output-dir tmp/buffer-tso-head-demand-20260923/repeat-dual \
  --expected-cases 8 --jobs 4
```

所有 CPI 表只汇总维护 validator 的 `cpi` 字段，不从 legacy path-class cache
字段另选真值。最终报告的 user-plus-kernel CPI 用每个模拟器自己的宏指令分母。
MAPE/MAE/百分位按 case 等权；WAPE 和 signed bias 按匹配的宏指令人口加权。
未另跑 user-only scope 或吞吐。原始 native PMU 诊断及其来源、映射、分位数保留
在各轮 `summary.md/json`；`passed` 只表示源与守恒门禁，不代表精度通过。

初版 `after-dual/` 存在未来 head 过早占 Sequencer 的问题，已被舍弃；不能引用
其误差作为最终实现效果。最终结论使用 `final-dual/`、`final-off/` 和
`final-life-namd/`。

## 最终小集结果与停止决定

旧/新 off、旧/新 dual 四组均为 8/8 discovered、passed 和 native PMU scored，failed/pending 均为 0；NAMD lifecycle-only 两组各 1/1。每核退休宏指令、UOP、访存人口和分支数守恒。独立身份门、输入守恒门与下述精度门分别判断。

| 案例 | gem5 CPI | 旧 dual CPI | 旧相对误差 | 旧 CPI 绝对误差 | 新 dual CPI | 新相对误差 | 新 CPI 绝对误差 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| c04-782.lbm_r | 0.817326 | 0.773531 | -5.36% | 0.043795 | 2.253361 | +175.70% | 1.436035 |
| c08-782.lbm_r | 0.822077 | 0.758934 | -7.68% | 0.063144 | 2.239046 | +172.36% | 1.416969 |
| c16-782.lbm_r | 0.951678 | 1.045025 | +9.81% | 0.093347 | 2.703193 | +184.04% | 1.751515 |
| c32-782.lbm_r | 1.502989 | 1.755288 | +16.79% | 0.252298 | 3.420970 | +127.61% | 1.917981 |
| c04-706.stockfish_r | 0.668626 | 0.660548 | -1.21% | 0.008078 | 0.666677 | -0.29% | 0.001949 |
| c04-811.tealeaf_s | 1.004183 | 0.862259 | -14.13% | 0.141924 | 0.856906 | -14.67% | 0.147277 |
| c04-854.graph500_s | 1.305529 | 1.163890 | -10.85% | 0.141639 | 1.538424 | +17.84% | 0.232895 |
| c04-857.namd_s | 0.479341 | 0.383074 | -20.08% | 0.096267 | 0.474274 | -1.06% | 0.005068 |

CPI 绝对误差及 MAE 的单位均为 cycles/宏指令；relative 为带符号误差。

| 范围 / 版本 | MAPE | CPI MAE | P50 APE | P90 APE | P99 APE | 最大 APE | WAPE | signed bias |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 全部 8 案例 / 旧 dual | 10.74% | 0.105061 | 10.33% | 17.78% | 19.85% | 20.08% | 13.72% | +8.87% |
| 全部 8 案例 / 新 dual | 86.70% | 0.863711 | 72.73% | 178.20% | 183.46% | 184.04% | 126.91% | +125.64% |
| LBM 4 案例 / 旧 dual | 9.91% | 0.113146 | 8.74% | 14.69% | 16.58% | 16.79% | 14.03% | +12.19% |
| LBM 4 案例 / 新 dual | 164.93% | 1.630625 | 174.03% | 181.54% | 183.79% | 184.04% | 145.42% | +145.42% |

| 案例 | optional prepare | 优化路径 head demand | 在途接管 demand | epoch 回退 |
| --- | ---: | ---: | ---: | ---: |
| c04-782.lbm_r | 1649548 | 1656624 | 67324 | 4576 |
| c08-782.lbm_r | 2894552 | 2910197 | 118442 | 6964 |
| c16-782.lbm_r | 4924461 | 4949730 | 172987 | 10361 |
| c32-782.lbm_r | 8430785 | 8475780 | 284775 | 15351 |
| c04-706.stockfish_r | 52900 | 1017296 | 1599 | 1418 |
| c04-811.tealeaf_s | 141585 | 238817 | 5024 | 2985 |
| c04-854.graph500_s | 72299 | 165168 | 2693 | 3702 |
| c04-857.namd_s | 9992 | 19853 | 177 | 3114 |

NAMD 单开 lifecycle 的独立对照（residence certificate 关闭）：

| 版本 | gem5 CPI | FastSim CPI | 相对误差 | CPI 绝对误差 |
| --- | ---: | ---: | ---: | ---: |
| 旧 lifecycle | 0.479341 | 0.414151 | -13.60% | 0.065191 |
| 新 lifecycle | 0.479341 | 0.428394 | -10.63% | 0.050947 |

**结论：未达到本次预期的“NAMD 收敛且 LBM C16/C32 不恶化”。** NAMD 和 Stockfish
改善，但 LBM 四个核数及 Graph500 明显回退，TeaLeaf 亦略退。全部 8 案例 CPI
MAE 从 0.105061 增至 0.863711，MAPE 从 10.74% 增至 86.70%。因此该候选只保留
默认关闭的实现、反例和证据，不改默认，不扩全 39 案例，也不宣布修好了生产精度。

LBM C4 的 L1D/private-L2/LLC miss 分别仍为 931,042 / 566,498 / 565,195，
与旧 dual 相同；新增认证回退 4,576 个 epoch，且 mandatory 准入与 SQ 生命周期
已改变。不能把新增周期解释为 miss 数增长，也不能把累计 Sequencer/SQ 等待求和
当作退休关键路径收益或回退比例。本轮未取得逐事件原生配对，尚不能把最终 CPI
退化唯一归因到某一个新 guard 或协议边。

下一步若重开，应先在 LBM C16/C32 的首个分叉 epoch 配对 canWB、optional
admission、mandatory admission、native callback 与 SQ release，分清容量认证、
replacement 顺序认证和既有 canonical 准入下界的贡献，并补齐 store 专属的时延
映射。不得关闭容量/顺序保护来追平 CPI，不得扫描 publish 常数或按负载选择开启。
完整 optional 资源逐时刻模型、全矩阵及独立 quiet-host 吞吐门禁均未完成。
