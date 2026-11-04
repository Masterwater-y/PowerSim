# gem5-spr 采集迁移与独立分支

## 仓库与修改边界

`gem5-spr/` 是独立 Git 仓库，当前开发分支为 **`yinhaolang`**；外层 FastSim 仍在 `fastsim-spr`。迁移前的完整 SPR 本地状态已保存为 `aa8ff90980ed4ad9531e2ee247361fe5ba7007e1`，原 `calibration-deploy` 分支保留 `02119d1`。没有向别人的 gem5 上游推送。

旧采集的大部分代码本来就存在于 SPR：依赖、内存请求、退休、动态翻译的共享格式一致，commit/LSQ/Ruby/SLICC 观察调用也已保留。因此没有套用旧版整份架构补丁。迁移修改集中在 TaoTrace 采集开关和 `configs/fastsim/` 新入口；7,363 个既有非 probe 的 `src/` 文件按迁移前 SHA256 检查保持一致。

独立仓库可以继续取上游更新；先检查工作区并提交本地改动，再显式合入基线分支，解决冲突时保留 SPR 与采集双方语义：

```sh
git -C gem5-spr status
git -C gem5-spr checkout yinhaolang
git -C gem5-spr fetch origin
git -C gem5-spr merge origin/calibration-deploy
```

这里的 fetch/merge 是后续操作示例，不代表本次执行过合并。`origin` 仍是原 `shown-net/gem5`，没有把 FastSim 的 Codebase remote 配成 gem5 remote。

采集实现已提交到子仓库 `27ef513696a7445ccca9a9db5c912eebf5c23a28`，子仓库工作区干净。父仓库保存对原 `02119d1` 的完整可移植补丁 `patches/spr-gem5-source-20261103.patch`，反向应用检查通过；父仓库 gitlink 仍保留可从原 remote 获取的基线，避免引用未发布子提交。在其他机器恢复时，应在该基线创建本地 `yinhaolang` 分支并应用补丁，再本地提交，而不是期待原 remote 已有此分支。

## 实际改动

- 新入口 `gem5-spr/configs/fastsim/fs_capture.py` 沿用旧 FS 负载 checkpoint → O3 预热 → 串口 WORKBEGIN → 最快核 user-UOP 目标共同截止协议。详细阶段为 O3 核 0–3，无额外 housekeeping 核。KVM 切换后回到单事件队列。
- 保留 SPR CPU 默认结构、512 ROB、128/72/48 分区 IQ、共享 FU、MITE/DSB/MS/IDQ、SPR store 实现，以及当前 2.6GHz、96MiB LLC、16 通道 DDR5 配置。入口不调用旧 `configure_o3`。
- 增加默认关闭的 `functional_retirement`：独立输出绑定完整 FST 热记录的 `.fsem` v2，不依赖线程状态开关；硬件入口与有证据的精确缺页前缀放弃分别标记。未知来源拒绝，任务归属和 native cache 资格门禁继续独立。
- 保留 `.deps/.mreq/.xlat.gz/.ffe.gz/.fsem/.vmap/.asmap/.imap`。这些是退休功能流的 companion；native 周期、cache 来源及 fusion 结果不成为模型输入。
- 修复入口对双 WORKBEGIN 通知的处理：通过只读 `functionalMeasurementStarted()` 查询实际采集边界，四核都开始后才清零一次。原始 workid 可以为零，不再依靠退出码猜测来源；之后到达的重复通知不再清零。未修改 guest marker、terminal 或 pseudo instruction 实现。
- 补齐 SPR 已有 `DsbValidation` 原生终结计数的摘要输出。维护验证器使用该计数独立重算请求守恒，旧缺项数据继续拒绝；没有回填旧实验，也没有修改 DSB 行为。
- 有效目标工具显式记录 native SPR store 与分区 IQ/FU 来源，不把 IQ 总数 248 声称为统一队列，也不把 SPR store 标成旧 serial/buffer 实现。

## 四核线程状态探针

实际数据位于 `tmp/spr-collection-migration-20261103/c4-thread-state-final/`。它是 FS 机制验证探针，不是 SPEC 或真机精度样本。内核为已审计的 5.4.49，SHA256 `da3d5e7dbf696b977f635d1123e06b324af873183d90111101ced005dc64e174`；静态探针 SHA256 `1891209a04ae271033b5d7809406604ac82773366c561b690ae9f21f70954040`。参数为 `1000 2000`，四核，最快核目标 100,000 user UOP，实际触发核因同周期退休到 100,002。

| 核 | 预热记录 | 计分记录 | 完整输入记录 |
|---|---:|---:|---:|
| 0 | 553,920 | 114,531 | 668,451 |
| 1 | 542,811 | 33,066 | 575,877 |
| 2 | 57,656 | 20,835 | 78,491 |
| 3 | 96,088 | 17,728 | 113,816 |

四核共同结束 tick 为 `17365923985410`，未裁剪较慢核，也未补造记录。最终日志只有一次 `measurement-open`。原生 committed-response drain 完成。

完整 1,436,635 条输入通过 frontend companion 审计；依赖、静态指令表、功能退休 companion 的热流绑定和动态翻译审计通过。四核动态翻译分别覆盖 164,615 / 159,454 / 16,646 / 23,857 条内存记录，unknown 均为零。末端存在 1 个部分宏，作为共同截止截断保留。

FastSim 使用本次实际 `config.ini` 生成的 184 项参数映射及退休前端配置完成回放，线程状态计划显式启用。48 个计划事件全部释放，4 个 task，unknown owner 周期为零；`RecordedScheduler` 确认启用。此处的 task-oncpu 资格不等于真机 perf 等价。映射审计仍明确列出 IQ 分区、store 合并与信用、端口仲裁等模型近似。

该 pthread 探针不产生 OpenMP parallel/barrier 事件，功能同步编译器正确拒绝“没有事件”的空计划，没有把它当作同步验证通过。

## 证据边界

本次证明采集协议、功能输入与消费路径的兼容性，不宣称“完美输入”或完整微架构、硬件 PMU、CPI 精度、吞吐验收。迁移无法消除以下已有边界：

- 新框架原用的 5.15 内核缺少已审计 scheduler profile；本次使用已审计 5.4.49，不能把任务归属结论直接外推到 5.15。
- thread-state profile 保留 build 审计限制；部分 wake 原因未知，不能合成因果唤醒边。
- `.fsem` 对精确放弃的证明限于已接受同步缺页；其他异常不能冒充同等证据。
- 固定工作量入口原有的独立逐核 score/execution 与 native cache 资格问题，不因新增旧协议入口而自动解决。
- native cache/退休 PMU 仍须使用独立 conserved Ruby/SLICC 来源和维护验证器。此文没有用 path-class 计数评分，也没有发布 CPI/PMU 误差。

上面这批线程状态数据的功能输入审计通过，但独立 native 摘要合并仍拒绝：原始摘要遗漏了 SPR 的 DSB validation terminal，导致可序列化的原语无法重新守恒。即使原始摘要自报布尔值为真，验证器仍拒绝。旧数据保持原样；后续修复后的新采集必须重新通过门禁，不能从缺口反推并补写旧计数。

构建、定向测试和审查证据保留在 `tmp/spr-collection-migration-20261103/`；全量 Python 回归还发现缺少 `fastsim_py` 和两项旧 TCSim checkpoint wrapper fixture。使用 GCC11 的进程内运行库重跑后，原先由运行库引起的相关回归已通过；未修改全局环境。

## 最终复验：同步与线程状态同时启用

最终数据在 `tmp/spr-collection-migration-20261103/c4-combined-final/`，使用修复后的 gem5 二进制和当前 instrumented libgomp。仍为四核 FS 机制探针，实际负载是既有 `tests/workloads/openmp_sync_probe.c`，参数 `4 1000 2000 1`；运行时在创建线程时预先绑定 `OMP_PROC_BIND=close / OMP_PLACES=cores`。不使用历史探针二进制。

| 核 | 预热记录 | 计分记录 | 完整输入记录 | 原生 DSB validation terminal |
|---|---:|---:|---:|---:|
| 0 | 177,758 | 391,121 | 568,879 | 12,631 |
| 1 | 162,578 | 352,815 | 515,393 | 11,779 |
| 2 | 0 | 403,771 | 403,771 | 10,462 |
| 3 | 0 | 467,745 | 467,745 | 10,948 |

- 四核共同结束 tick：`4806158750470`，原生 response drain 完成；真正起点只清零一次。
- 完整 1,955,788 条记录通过维护的原生 `BinaryTraceSource` 前端/功能流审计，0 个部分宏，82 个 pseudo spans。四核 FST 及所有 companion 保持原名和原始字节。
- 功能同步：216 个事件、9 个 region、17 个 barrier。共同截止留下 1 个未完成 region；仅在共同截止验证后允许 truncated region，不合成离开事件、不放宽 DAG 因果检查。
- 线程状态：428 个计划事件、4 个 task，任务归属完整，unknown switch 为零。72 个 wake 原因仍未知，保留诊断；71 个已证明 block/wake admission 不等于证明全部 OS 唤醒因果。
- FastSim 同时启用两份计划，共 644 个事件全部释放，计分段 642 个事件全部消费；unknown owner 周期为零，暴露出 28 次真实模型依赖等待。退休前端消费计分段 1,615,452 条 UOP。这里的“真实”指模型基于功能计划的等待，不是注入原生等待周期。
- 维护的 `merge_kernel_events_oracle_v3.py` 接受新输出，DSB terminal 守恒缺项已在新采集中闭合。旧缺项数据仍拒绝；这不是 native cache/退休 PMU 完整精度通过。

两核预热为零是实际计数：它们在计分开始前没有 O3 退休记录。采集能力支持完整预热前缀，不能据此声称每核都有非零业务预热。业务精度样本应使用明确的逐线程 warmup 工作和相应边界再采集。

最终 gem5 SHA256 为 `d28fdfd739293572c6658c7612cca318542956559daf8e11f75fa619c26510fe`；FastSim SHA256 为 `b7daf5dae6fae5b485b61919a71afb8639e6c7edadbced1fdda510b36d175564`；当前探针 SHA256 为 `efa63d0e931238b96ebf8795b998daa7cc16e341dc1869761e0e6d7b5dbc654c`；运行库 SHA256 为 `aaed00854f9739ee00667a85ae0196bdc6146ab05b7c0fccdbd309563c7e7565`。

历史 `sync-probe-fs-pinned` 的第一次采集因迁核被静态绑定图拒绝；加运行时绑核后，历史二进制仍发生 team 复用时的 region 归属错误。反汇编确认其逐事件重读共享 team；当前运行库已在安全入口保留 TLS 身份，重新链接后完成上述复验。不能通过拷贝旧二进制或只匹配 marker 符号来替代运行库身份验证。

## 完整输入门禁与最终限制

新增 `tools/prepare_spr_capture.py` 整理已完成的本地采集。它不搬移或删除原始流，要求全部声明参与核、共同终点、热记录／companion 绑定、精确 warmup/measurement 宏计数和实际硬件核数一致，再生成 canonical 身份、manifest、同步与线程计划。既有不同内容的派生文件拒绝覆盖。未证明的空同步计划不启用；缺 native 摘要与功能输入资格分开报告。暂不支持 `--extra-cores` 的未采集备用核。

本机示例需 Python 3.11 和进程内 GCC11 运行库，不改全局环境：

```sh
LD_LIBRARY_PATH=/opt/gcc-11/lib64 \
/root/.pyenv/versions/3.11.14/bin/python3 tools/prepare_spr_capture.py \
  --source tmp/spr-collection-migration-20261103/c4-fixed-owners-final
```

完整功能审计器 `build/fastsim_audit_retired_frontend` 必须已构建。20 项定向测试包含缺失／损坏 companion、完整热流篡改、错误宏计数、硬件与来源核数不一致、空同步、合法截断及无效同步图；独立审查发现的问题已修复。

固定线程源 `c4-fixed-owners-final` 最终通过全部输入整理与独立身份检查，重复整理保持幂等，103 个原始文件校验未改变。四核都有非零预热，完整记录 1,368,003 条；66 个编译后的线程计划事件全部消费，unknown owner 周期为零。70 条原始线程状态事件与66个编译事件是不同计数。该 pthread 探针没有 OpenMP 同步事件，状态保持 unavailable。

维护的 native validator 对固定线程源检查 **expected=1 / discovered=1 / passed=1 / failed=0 / pending=0**，来源是本次原始 Ruby/SLICC 摘要，未使用 path-class cache 评分。结构守恒、native drain、用户／内核宏与 UOP 人口及 task PMU 人口检查均通过。此项通过只代表来源与守恒：

- 严格 CPI 为 **unavailable**，原因为 `gem5:proxy-scheduler-boundary`，CPI 绝对误差与 MAE 均为 null；不能从 owner unknown=0 推导 perf 周期等价。
- 评分命名空间是 `native_tag_miss_diagnostics`，不是三个真机 `MEM_LOAD_RETIRED.*` 事件的完整验收。没有认证硬件 PMU。
- 本次使用维护入口 `--source-root` 和 `--stats-dir` 对刚保存的回放输出复核，没有重测吞吐。复核器仍标注 `not-certified-by-rescoring`；独立二进制哈希在本记录和精简证据中保存。

**同步探针 `c4-combined-final` 不能宣称完整输入 ready。** 核1、2经历多个 CR3，旧静态 `.imap` 没有 ASID 字段，生产器在 `noteFstAddressSpace()` 清理并在 `writeFstInstructionMap()` 主动省略这些表。这是保守行为；完整 FFE、动态翻译和同步事件通过，不会自动补足静态指令几何／操作数资格。严格整理器正确拒绝该源，未合成静态表。后续应设计 ASID 感知静态指令表并同步扩展消费者，或使用有证据的固定地址空间采集；不能仅删除门禁。

另一个可选元数据限制是 syscall ABI 参数：当前沿用 number-only 默认；原生 syscall UOP 已完整采集，参数 valid 位需通过既有 `syscall_arg_counts` 配置才会声明。不能把原始微操作采集误报为所有可选 ABI 语义字段都已证明。

本次已经完成采集入口与功能证据迁移，保留 SPR 结构；没有承诺所有负载均已获得“完美输入”。后续重点是多地址空间静态表、已审计的5.15 scheduler profile、原 Zstd 业务同步／固定计分布局及更长尾部复采，再开展同人口 CPI 与退休 load PMU 验收。精简的来源、哈希、事件与门禁记录见 [summary.json](spr-collection-migration-20261103/summary.json)。
