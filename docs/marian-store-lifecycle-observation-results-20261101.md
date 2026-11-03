# Marian store 生命周期观测结果（2026-11-01）

## 结果

本轮只增加观测，用现有共同终点FST重放Marian P1。没有改动目标时序、cache分类、
延迟参数或gem5，没有重采、commit或吞吐收益声明。新预测与冻结P1的全部既有目标
JSON字段一致；只排除明确列出的宿主wall/throughput/线程等待字段。

测量期捕获1,832,068条regular非local store UOP，服务总和52,006,276 cycles，
逐核人口与服务总和逐位等于原有response residual计数。另保留3,910,005条warmup
记录。二进制记录约877MiB，观察开关默认关闭。

本轮把差异进一步定位到**store miss的剩余服务时间**，但还未完成gem5与FastSim
同端点、同成员的因果闭合，不能宣称千万级CPI残差已修复。

## 已落实的观测

`diagnostics.store_lifecycle_path` 输出160-byte记录：核、UOP序列、dispatch、issue、
retire、eligible、前序release、正式send、SQ release、bounded prepare与permission、
role、canonical private level、fragment数、line、task/kernel/能力标记、source sequence、
ordinal及warmup/measurement phase。

记录只从最终提交的timing事务输出；临时重算和回滚不写文件。内部向量不并入长期
统计累计。普通服务、prepare角色和既有时序的计算不变。

当前bounded路径没有保留独立协议callback；记录明确标为不可用，没有把SQ release
减一拍伪装成callback。模型级正式send也未被自动认证为原生Sequencer准入。

现成的 `bounded_store_demand_observation` 已有角色和时延分布，本轮复用它并补齐
逐核/逐条身份。关闭旧prepare lifecycle实验不意味着当前P1没有准备重叠。

## 逐核排空观测

本表为测量期regular-store成员，时间单位均为333-tick核周期。
记录保留绝对时间；裁剪窗口从报告中的warmup barrier **5,896,082cycles** 开始，
各核结束为该起点加各核报告周期，不使用错误的零起点。

| core | store数 | send→release总和 | 窗口内service区间并集 | 未被这些区间覆盖的核周期 | 子集最大同时驻留 |
|---|---:|---:|---:|---:|---:|
| 0 | 442,058 | 13,599,362 | 13,596,392 | 456,323 | 114 |
| 1 | 452,148 | 13,679,849 | 13,679,592 | 522,479 | 114 |
| 2 | 483,502 | 12,909,080 | 12,909,080 | 1,035,825 | 114 |
| 3 | 454,360 | 11,817,985 | 11,817,985 | 1,828,913 | 114 |
| 合计 | 1,832,068 | 52,006,276 | 52,003,049 | 3,843,540 | — |

最大驻留按模型dispatch→release区间计算。这里不是完整硬件SQ人口；不含local、
NoAccess等成员，且服务并集仅包含phase1成员，不含warmup发起后跨入测量的服务。
“未覆盖”不能称为流水线空闲、SQ空闲或已证明的因果停顿。

完整时长与裁剪并集相差3,227cycles，混合边界裁剪与可能的区间重叠，不能单称重叠。
非workload-task的1,013条观测服务共1,528cycles；这些已观察到的FS端小项不是
千万级差距的主体，但不能据此证明原生成员已逐条匹配。

## 差异集中在哪里

| 各自分类下的服务分组 | FastSim人数 | FS服务总和 | gem5人数 | gem5服务总和（由打印均值估算） |
|---|---:|---:|---:|---:|
| FS canonical L1 / native ST hit | 1,619,186 | 1,627,623 | 1,619,294 | 约1,620,212 |
| FS 非L1 / native ST miss | 212,882 | 50,378,653 | 212,354 | 约39,874,662 |

hit侧差约7,411cycles，miss侧差约10,503,991cycles，占服务总差约99.93%。
这是按各自标签做的**数值分摊**，不是同成员已配对：原生hit/miss依赖
Sequencer的 `isExternalHit`，FS列为canonical private分类，多fragment只保留首片层级。

模型private-L2 miss/shared路径共212,569条，50,378,109cycles；其中OriginalDemand
为211,449条，50,328,687cycles，均值238.018cycles。这里的shared路径不等于
已证明每条均访问DRAM。

模型已有1,015,865次Publish、705次Takeover、12次Reacquire；另有389条非单片
bounded资格成员，角色未推断。绝大多数Publish为L1路径；shared路径的Publish为606，
Takeover为514。总prepare角色数不能证明关键miss获得了与gem5相同的提前量。
下一步应核对这批OriginalDemand miss为何没有更早准备，以及其完整服务recipe是否
与原生一致；不能按prepare总数去调等待。

## 对“10.51M闭合”的修正

模型完整服务52,006,276；native ST samples=1,831,648、打印均值22.654393，
两者相减约10,511,402.37cycles。原生总和来自六位小数均值，并非直接输出的精确sum，
舍入范围约±0.916cycles，对量级判断没有影响。

但比较口径必须分别保留：

- 官方FS active cycles=56,801,558；模型各核报告周期之和=55,846,589，二者差954,969。
- gem5 numCycles各核和=46,633,744，因此模型各核和多9,212,845cycles。
- 10.51M服务差对官方active差“接近3.3%”不能替代逐核因果分解；对上述核和差，
  服务差反而多约1.30Mcycles。核和只是诊断，不能替换官方active CPI评分。
- 原生ST直方图按完成请求入账，FS按接受的store UOP保留完整服务，窗口边缘人口
  不同；原生当前文件只有全局RequestType.ST直方图，不能虚构逐核native直方图。

因此报告保留 `causal_closure=false`。当前已经排除“FS完全没有prepare”的笼统解释，
并把优先级缩小到关键store miss的服务路径与准备提前量；尚不能在DRAM到达轴和
prepare不足之间作出根因判定，也不设计时序修复。

## 验证与产物

代码：`src/simulator.cpp`、`src/config.cpp`、`include/fastsim/config.hpp`、
`include/fastsim/types.hpp`，分析工具 `tools/analyze_store_lifecycle.py`。

- 完整 `cmake --build build -- -j16` 成功。
- `LD_LIBRARY_PATH=/opt/gcc-11/lib64 ./build/fastsim_tests` 通过。
- bounded integration入口通过，新增检查二进制格式、人口/时长守恒、观测周期中立性。
- Python观测分析6项通过，含非零测量起点、零native store、区间裁剪、callback缺失。
- 独立审阅发现并修复了窗口起点和零人口问题。模型/source/core/reference时钟验证后
  才允许裁剪，不支持的异频组合直接拒绝。

证据目录 `tmp/store-lifecycle-20261101/`：`freeze.json`、`fastsim-observer`、
`marian.cfg`、`stores.bin`、`marian.json`、`analysis-complete.json`、各测试日志及
`completion.json`。早期analysis.json/analysis-final.json为开发收据；最终以
analysis-complete.json为准。

下一步保持只读诊断：对OriginalDemand miss按prepare扫描资格、同line前驱、阻挡原因
和共享服务recipe继续分组。若现有native数据不能证明相同成员的prepare/demand关系，
再考虑有限窗口同次FST与流水线/请求时刻采集；当前没有启动这项采集。
