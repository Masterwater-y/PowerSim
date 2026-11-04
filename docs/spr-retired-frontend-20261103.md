# SPR 退休流前端（2026-11-03）

本轮在 Codebase `fastsim-spr` 上实现可选的退休流前端。输入仅含退休功能记录、
精确指令字节、解码分类和记录边界；FastSim 自行决定 MITE/DSB/MS 来源与时序。
不采集或构造推测路径，也不把 gem5 的命中、服务来源、投递时间或停顿时间作为输入。

当前对齐进度及后续优先级见
[FastSim 与 gem5 SPR 对齐进度及后续工作](spr-alignment-progress-and-next-steps-20261103.md)。

## 分支与复现

`fastsim-spr` 已创建并推送，根提交为
`a0d934524450300f3a64e19dd06f64c4c1b9ae70`，无父提交；后续清理提交
`48394e77a25eae10cf5eaced2155ad8b50f26a5a` 将继承的生成可执行文件移出版本跟踪，
保留本地文件。后续提交、更新及推送均使用此分支；不修改历史 `FastSim` 分支。
隔离的 gem5 和框架仍保留各自 Git 历史、remote 和构建目录。父项目的
`patches/spr-{gem5,framework}-source-20261103.patch` 保存各自基线之外的源码差异，
包括原始导入时已有的差异，不能只用 gitlink SHA 表示完整源码身份。
在新建的、干净的子仓库 checkout 上，可分别用 `git -C gem5-spr apply
../patches/spr-gem5-source-20261103.patch` 和 `git -C spr-framework apply
../patches/spr-framework-source-20261103.patch` 恢复覆盖层。现有目录已应用，不能
重复应用；先用 `git apply --reverse --check` 检查。更新 upstream 前保存覆盖层，
按 [导入记录](spr-gem5-c4-validation-20261103.md) 审查后合并，避免强制覆盖本地改动。

```bash
git switch fastsim-spr
scripts/build_gem5_spr.sh
cmake --build build -- -j16
LD_LIBRARY_PATH=/opt/gcc-11/lib64 ./build/fastsim_tests
LD_LIBRARY_PATH=/opt/gcc-11/lib64 ./build/fastsim_spr_frontend_tests
LD_LIBRARY_PATH=/opt/gcc-11/lib64 ./build/fastsim_retired_frontend_facts_tests
LD_LIBRARY_PATH=/opt/gcc-11/lib64 ./build/fastsim_spr_frontend_integration_tests
```

## 功能输入合同

FST v7 增加 feature bit 11；每个热流对应 `.fst.ffe.gz`。每个描述符绑定连续的原始
record 范围、PC、地址空间、精确字节及语义身份，标明功能 UOP 数量、共享分配、
微码、伪指令、完整性、保留的宏／分配组起点、无条件跳转和 DSB 资格。
文件头尾绑定核号、完整热流记录数与原始记录哈希。读取器在暴露输入前完整检查
记录范围、PC/地址空间、哈希、gzip CRC、尾部及未知标志；窗口包装器保留原始 ordinal。

字节证据来自 x86 decoder，随 PCState 保存；DSB 模板与融合重放保留同一证据。
仅可证明为硬件入口的 ROM 伪指令允许零字节。被省略的 MicroDebug 前缀必须由
功能分类证明，才能把首条保留 UOP 标为起点。真实缺失功能组头时不会猜测 slot。
未完成的退休宏可保留已观察前缀，但不能填入 DSB；来源不明或中途开始的宏被拒绝。

旧 trace 没有该 companion，仍可用于原模式，不能用于新前端；新模式报缺失信息。
功能审计入口为 `tools/audit_fst_retired_frontend.py --fst PATH`，通过与 FastSim
相同的 C++ reader 预检完整流。功能来源审计通过不代表时序精度通过。

## 微架构接线及参数来源

选择 `configs/gem5-spr-retired.cfg`；实际采集入口为
`spr-framework/configs/gem5/spr-fixed-work-retired-frontend.json`。
框架从本次 `config.ini` 的活动 CPU/component 对象读取参数，检查各核一致性，
记录 `mapping.json`，不按 CPI 误差调整参数。

| 部件 | 本次配置 | FastSim 行为 |
|---|---|---|
| InstructionBytes | 64B buffer，32B service | 复用 L1I 响应日历，再按实际字节跨度供给；跨 64B 请求第二块 |
| MITE | 每批至多 6 宏、64 功能 UOP；8 slot/cycle，延迟 1 | 按功能宏组批，受字节和 IDQ credit 约束 |
| DSB | 64 sets、8 ways、每 entry 8 slots；8 slot/cycle，延迟 1 | 模型自行查找、填充、替换；共享分配只占一个 slot |
| MS | 每批 1 宏、64 功能 UOP；4 slot/cycle，延迟 1 | 长宏有界分片；普通微码宏仍取字节，入口伪指令不取字节 |
| IDQ / Rename | 144 slots / 每周期 6 slots | 投递占用 credit，在模型 Rename 接受时回收；融合 follower 不重复收费 |
| 来源切换 | DSB→MITE 延迟 0 | 从本次 frontend 对象读取 |

新模式替换旧 fetch/decode 带宽及旧队列近似，要求 `fetch_to_decode=0`、
`decode_to_rename=0`，避免与 SPR component 服务延迟重复。已拒绝推测／shadow
路径开关以及尚未联合验证的独立 rename/response 实验模式。
测量重置保留 DSB、字节和占用状态，仅重置计数器；CLI 报告实际配置与计分人口中的
`spr_frontend` 来源 UOP、宏 span、allocation slot 数。宏 span 包含入口及不完整描述符，
不能当作架构宏指令数。

## 验证与边界

默认构建和 `fastsim_tests` 通过；三个独立 C++ 测试通过。定向覆盖冷热来源、
服务宽度、共享 slot、IDQ 压力、长微码分片、状态重置、copy/snapshot、精确字节、
损坏/截断 companion、原始记录身份、窗口转发、跨行重复宏、缺失元数据和禁止开关。
Python 的字节证据、宏起点、DSB 资格、FST feature 检查，以及框架参数映射与源码
快照检查通过。真实 C4 producer→审计→FastSim 贯通结果另记在下面实验记录。

这是已退休路径上的部件模型，以下尚未完成对齐：

- 一次只处理一个宏，不能完整重现 gem5 跨宏原子批处理与所有反压时序；Rename
  接受来自当前 backend admission，尚无后续 response 修正的联合反馈。
- DSB 使用有界 LRU 分支拒绝记录，实际代码身份使用地址空间及字节/语义证据；
  不等同原生物理代码 lease 和全部 decoder-context 失效语义。
- ITLB/STLB、多组 IQ 的独立准入、全部 ROB/fusion 占用、硬件预取等仍有差异。
  不应把静态参数一致、功能审计通过或定向测试通过升级为逐周期／硬件等价。
- 跨宏 ALU/compare/test + Jcc 融合尚未实现。native 在 `frontend_fetch.cc` 中对
  同一批次、同地址空间/权限、连续且同 64B 块的合格操作组装 AllocationGroup；
  当前 FFE 的 SharedAllocation 只描述宏内共享，不提供完整的跨宏融合资格。
  不从 native 实际融合结果注入时序，也不猜测资格。probe 的模型计分 allocation
  slots 为 875，native `UOPS_RETIRED.SLOTS` 为 728，绝对计数差 147；这一人口差
  必须保留，不能宣称 slot 语义已完整对齐，也不把差值全部归因于某一机制。
  进一步逐核核算为模型 219/220/217/219、native 182/183/181/182；对应静态候选
  37/37/36/37 对，合计 147。其中循环 CMP→JA 占 128 对，其他 TEST/CMP→Jcc
  占 19 对。`1003 UOP - 128 宏内节省 - 147 候选 = 728` 与原生总账一致；已排除
  窗口跨宏、partial/pseudo、非退休或 kernel 人口差异，move elimination 原生保留槽。
  这是缺失跨宏融合的强证据；没有逐对的源 batch/group 证据，仍不能认证每个候选
  在原生中实际同批融合。
- 之前 fixed-work 的 `effective-target.json`、cache 窗口 origin 与 `.fsem` 等缺口
  不因本轮 `.ffe.gz` 增加而消失；历史 Zstd 结果和 context 尾部覆盖缺口见
  [前轮记录](spr-gem5-c4-validation-20261103.md)。历史 CPI 不是本轮新模型的精度结果。

## 实际 C4 贯通记录

`spr-framework/output/spr-retired-frontend-probe-c4-20261103` 的 `probe-fork`
完成采集、业务结果核验、完整来源审计和 FastSim 回放。四 worker 各执行 32 次循环；
这用于验证新 producer→consumer 接线，不用来评估正常业务精度。

- gem5 SHA256：`c04a45bc41d6ca3f0f441503a5a18c4ab8c7df84000619a331c034c48816a7fb`；
  FastSim：`b7daf5dae6fae5b485b61919a71afb8639e6c7edadbced1fdda510b36d175564`。
- 新 boot 161.63 秒；采集 55.27 秒，退出 0、四核 result buffer 导出及 drain 完成。
- 四份热流共 1,161,067 条记录；FFE 描述符 1,155,999，allocation slots 1,158,744；
  partial/pseudo 均为 0。依赖、系统调用、虚拟页、动态翻译和 FFE 完整源审计通过。
- FastSim 消费全部 prefix/score/suffix；四核 score 宏／功能 UOP 人口全部与
  native snapshot 一致。计分 UOP 1,003，allocation slots 875；其中 MITE 135、
  DSB 868、MS 0，均为模型计算，非注入原生服务结果。
- 执行尾部仅 1/4 核覆盖最后一个模型 score，`all_execution_covers_last_score=false`；
  不给 team elapsed 或精度验收结论。严格 target/cache-PMU 缺口继续保留。
- 首次发布的 C++ 审计子进程漏传配置中的运行库路径，因 `GLIBCXX_3.4.26`
  启动失败。已统一复用 `runner.process_env`，保留原失败 run/audits，复用同一已完成
  capture 重新审计和回放；`audit-runtime-recovery.json` 记录 9.26 秒恢复及前后相同
  FST SHA256，没有替换采集人口或修改 trace。

同配置 `zstd-A` 首次 C4 重采目录为
`spr-framework/output/spr-retired-frontend-zstd-c4-20261103`。该尝试在
`observeFstFrontend` 调用反汇编格式化 SIMD 操作数时触发 gem5 标量尺寸断言，
退出 -6；60,055,552 字节未完成热流保留，但不发布为有效输入。
`failed-source-receipt.json` 保存大小和哈希。指纹已改用精确代码身份、静态语义
字段及有序源／目标寄存器类和索引，完全移除该路径的反汇编调用；SIMD 定向测试
先复现 abort，再验证修复及身份区分。修复后的 gem5 已重编译完成；重新采集目录为
`spr-framework/output/spr-retired-frontend-zstd-c4-20261103-retry1`，从新 boot 开始，
保持四 worker 各压缩 128 KiB 的冻结工作量。该重采、来源审计及回放均已完成，
但下述行为与证据缺口仍使严格验收不通过。

```bash
/root/.pyenv/versions/3.11.14/bin/python3 spr-framework/tools/calibrate.py collect \
  --target gem5 --config spr-framework/configs/gem5/spr-fixed-work-retired-frontend.json \
  --from spr-framework/build/fixed-work/spr-c4-20261103-v2 \
  --output spr-framework/output/spr-retired-frontend-zstd-c4-20261103-retry1 \
  --case zstd-A --adaptation aligned_buffer --collect-timeout-s 2400
```

Zstd 本轮身份与功能结果：

- gem5 SHA256：`01a293e6019edbde9bbd8d9323d3e6a161b699296666f84144cbf50e75f69750`；
  FastSim：`b7daf5dae6fae5b485b61919a71afb8639e6c7edadbced1fdda510b36d175564`。
  源码提交、dirty diff、配置 include 链和实际参数来源保存在 run/mapping 记录中。
- 新 boot 110.10 秒，采集 870.44 秒，回放 119.24 秒，采集与回放退出码均为 0；
  时间各自独立，未合并为端到端吞吐测量。
- 四份热流 3,378,431,584 字节、52,787,963 条记录；FFE 40,828,529 个宏 span，
  16 个明确的不完整前缀、1,128 个已知硬件入口伪宏。完整范围、核号、PC/ASID、
  热流哈希、边界及 gzip CRC 校验通过，没有通过裁剪或猜测补齐缺失记录。
- 四 worker 输出分别经 host Zstd 解压及逐字节核验，均 `verified=true`。
  依赖、系统调用、虚拟页、FFE、四核动态翻译审计全部通过；16,036,907 条内存
  记录的翻译已知，unknown=0。该 unknown 只描述翻译，不描述 cache PMU 资格。
- 计分 26,140,218 条宏指令、33,535,530 条功能 UOP，四核分别与 native snapshot
  一致。所有 prefix/score/suffix 记录已消费。模型计分 MITE/DSB/MS UOP 分别为
  1,944,245 / 30,065,306 / 1,525,979，三者相加等于计分 UOP。
- 本轮模型前端 allocation slots 为 30,258,940，native 退休分配组为 28,004,317，
  差 2,254,623。尚未补齐跨宏融合等 slot 行为，不能称 slot 人口或微架构已完全对齐。

以下为 **物理核窗口 CPI 诊断**，不是严格精度验收。绝对误差单位均为 cycles/宏指令：

| replay core | gem5 CPI | FastSim CPI | 相对误差 | CPI 绝对误差 |
|---|---:|---:|---:|---:|
| 0 | 0.451345 | 0.473662 | +4.944% | 0.022316 |
| 1 | 0.452560 | 0.472093 | +4.316% | 0.019533 |
| 2 | 0.449382 | 0.476006 | +5.925% | 0.026624 |
| 3 | 0.450673 | 0.471123 | +4.538% | 0.020450 |

四核等权 CPI MAE 为 **0.02223066**，MAPE 为 **4.93065%**。按宏指令权重合并，
即 `sum(物理核窗口 cycles)/sum(宏指令)`，gem5 CPI=0.45098568、FastSim CPI=0.47322597，
相对误差 +4.93148%，绝对误差及此单案例 CPI MAE=0.02224029。
该合并量不是 team elapsed；四核也不是四个独立负载案例，不外推多负载精度。

严格缺口已用本次输出重新核验：

- 只有 core2 的执行尾部覆盖最后模型 score，1/4；其余三核为 false，
  `all_execution_covers_last_score=false`。采集端是统一结束的，这个标志比较的是
  FastSim 重算后的完成时刻，不能仅据此认定 gem5 漏采或存在实际干扰缺失。
  320,375 个计分周期处在至少一核输入耗尽之后，占四核计分周期的 2.58989%；
  该比例不是 CPI 偏差或误差上限，实际影响尚需保持计分人口一致的尾部长度对照。
- `validate_fs_oracle_identity.py --result .../cases/zstd-A/gem5` 返回 `valid=false`：
  缺 `effective-target.json`。未以手工填表或 legacy 许可替代身份合同。
- 维护 native validator 对该 fixed-work 布局发现 0/1、passed=0、pending=1；
  不能把空集合当通过。未复用 path-class cache 字段；固定窗口的 cache origin、
  `.fsem` 等退休 load PMU 资格缺口仍在，`native_cache_accuracy=null`。
- 本轮与历史 Zstd 的原生窗口人口不同；不把两轮 CPI 差异直接称为模型精度提升。

可审阅的精简数据见 [Zstd summary](spr-retired-frontend-20261103/zstd-summary.json) 和
[probe summary](spr-retired-frontend-20261103/probe-summary.json)。原始 trace、镜像、
构建和大批运行输出保留在工作区，不推入 Codebase 源码分支。
