# TSO buffer-114 baseline-40 FST collection

日期：2026-09-18。

状态：**采集已结束，但 formal 仅完成 38/40，不能发布为完整 corpus**。本轮是
独立于现有 serial corpus 的 fresh gem5 FS `buffer-114` 数据集，不复用 serial
ROI checkpoint、FST 或 oracle。第 6 节记录最终状态和两个失败 case 的修正诊断。

## 1. 冻结合同

- 矩阵：`configs/spec2026-tso-buffer114-baseline-40-v1.json`
- 输出根：`tmp/tso-buffer114-baseline40-20260918`
- case 数：10 workloads × C4/C8/C16/C32 = 40
- 正式 FST 文件数：600
- 测量目标：最快核心到达 10M user UOP 时共同关闭
- 测量策略：`first-core-target-common-end-v1`
- trace scope：`user-plus-kernel`
- functional warmup：启用并保留 record-bounded warmup slice
- TSO 模式：`buffer`
- store buffer / SQ：114 entries
- gem5 binary：
  `/data00/yinhaolang/gem5-fs/build/X86_MESI_Three_Level/gem5.opt`
- gem5 SHA-256：
  `c2c6be9dbdc0da94ec217dda0cdf042ebbf92a8a8176f7e2de5798357592837b`

workload 集合沿用现有 baseline-40：

1. `706.stockfish_r`
2. `710.omnetpp_r`
3. `777.zstd_r`
4. `782.lbm_r`
5. `803.sph_exa_s`
6. `811.tealeaf_s`
7. `816.nab_s`
8. `854.graph500_s`
9. `857.namd_s`
10. `881.neutron_s`

每个 workload 都在 4、8、16、32 核下创建独立 `buffer-114` ROI checkpoint。
checkpoint 的 request 和 metadata 必须同时声明：

```text
tso_store=buffer
tso_store_buffer_size=114
tso_store_buffer_active=true
prepare_origin=fresh-kvm-boot
```

## 2. 启动配置

宿主 preflight 结果：

```text
cases=40
formal_fst_files=600
jobs=40
prepare_jobs=4
finalize_jobs=8
affinity_cpus=192
available_memory_gib=1911.6
disk_free_gib=4341.6
disk_budget_gib=1536
disk_reserve_gib=1229
```

采集器 PID 为 `123429`。冻结合同位于：

```text
tmp/tso-buffer114-baseline40-20260918/run-contract.json
tmp/tso-buffer114-baseline40-20260918/preflight.json
tmp/tso-buffer114-baseline40-20260918/matrix.json
```

启动命令：

```bash
/data00/yinhaolang/infer/.venv/bin/python \
  tools/collect_uarch_common_end.py start \
  --matrix configs/spec2026-tso-buffer114-baseline-40-v1.json \
  --output tmp/tso-buffer114-baseline40-20260918 \
  --binary /data00/yinhaolang/gem5-fs/build/X86_MESI_Three_Level/gem5.opt \
  --resources /data00/yinhaolang/gem5-fs/resources \
  --config configs/gem5-fs-native-kernel.cfg \
  --tso-store buffer \
  --tso-store-buffer-size 114 \
  --jobs 40 \
  --prepare-jobs 4 \
  --finalize-jobs 8 \
  --memory-per-job-gib 12 \
  --memory-reserve-gib 64 \
  --disk-budget-gib 1536 \
  --disk-reserve-gib 1229 \
  --prepare-timeout 14400 \
  --sample-timeout 86400 \
  --finalize-timeout 7200 \
  --transient-retries 2
```

## 3. 当前启动验证

启动后首批四个 C4 checkpoint 已完成：

- `706.stockfish_r`
- `710.omnetpp_r`
- `777.zstd_r`
- `782.lbm_r`

四者的 `request.json` 均为
`buffer/114/active/fresh-kvm-boot`，checkpoint `metadata.json` 均为
`o3_tso_store=buffer`、`o3_tso_store_buffer_size=114`，且 `m5.cpt` 和 memory
payload 存在。后续 checkpoint 仍由四个并发 KVM worker 创建。

采集器先完成 40 个 fresh checkpoint，再运行 40 个 10K smoke；只有全部 smoke
通过后才进入 40 个 10M formal capture。每个 case 的 finalize 阶段会执行：

- FST v7 header、record count、dependency companion 检查；
- functional warmup 与 common-end 边界检查；
- `tools/validate_fs_oracle_identity.py` final-config 身份检查；
- native Ruby lifecycle、层次事件和 drain 守恒检查；
- zero unknown-kernel gate。

任一 case 失败都会使该 phase 保持 incomplete，不会发布部分集合为完整 corpus。

## 4. 监控与恢复

查看总体状态：

```bash
/data00/yinhaolang/infer/.venv/bin/python \
  tools/collect_uarch_common_end.py status \
  --output tmp/tso-buffer114-baseline40-20260918
```

查看主日志：

```bash
tail -f tmp/tso-buffer114-baseline40-20260918/collector.log
```

统计各阶段完成数：

```bash
find tmp/tso-buffer114-baseline40-20260918/preparation \
  -name prepared.json | wc -l
find tmp/tso-buffer114-baseline40-20260918/smoke/source \
  -name collection-status.json -exec grep -l '"state": "complete"' {} + | wc -l
find tmp/tso-buffer114-baseline40-20260918/formal/source \
  -name collection-status.json -exec grep -l '"state": "complete"' {} + | wc -l
```

若宿主重启或 collector 中止，使用第 2 节同一 `start` 命令恢复。完成的 checkpoint
和 case 会先经过 snapshot 与身份复核后复用；不完整或损坏的 checkpoint/result
会被隔离后重建，不会静默覆盖。

主动停止：

```bash
/data00/yinhaolang/infer/.venv/bin/python \
  tools/collect_uarch_common_end.py stop \
  --output tmp/tso-buffer114-baseline40-20260918
```

## 5. 发布条件

只有以下条件全部满足，才能把本轮称为完成的 buffer-114 baseline-40 FST corpus：

1. `collector-state.json:state == "complete"`；
2. preparation、smoke、formal 三个 `collection-finished.json` 都为 complete；
3. formal 恰好 40 case、600 个 core FST，无额外 case；
4. 40/40 final-config identity 均为 `buffer/114/active/SQ=114/needsTSO=true`；
5. 40/40 common-end、functional warmup、native Ruby 和 zero unknown-kernel gate
   全部通过；
6. 每个 case 保留 request、command、config、effective target、trace、oracle、
   native summary、audit 和 artifact snapshot。

114 是 Golden Cove/SPR-like 的研究初值，不是 Intel 对所有 Sapphire Rapids
SKU/stepping 的官方逐周期实现保证。

## 6. 2026-09-20 终态与失败复盘

### 6.1 实际 formal 身份与终态

第 1、2 节保留的是本轮最初启动时的配置。最终 formal-only 恢复运行位于：

```text
tmp/tso-buffer114-common-end-baseline40-20260918
```

其 `run-contract.json` 固定的实际 binary 是：

```text
/data00/yinhaolang/FastSim/tmp/se-fs-paired-c4-20260820/gem5-se-isolated/build/X86_MESI_Three_Level/gem5.opt
SHA-256: 74d101122e4775f3cbf0c489af403ccbddb201a9a738ec13f3125edfa51d1e7a
build mtime: 2026-09-18 15:55:45 +0800
```

该合同仍为 `buffer/114/active` 和
`first-core-target-common-end-v1`，formal sample timeout 为 86400 秒。最终状态为：

- preparation：40/40 complete；
- formal：38/40 complete，2 个 case 未完成；
- `uarch-baseline-c08-857.namd_s`：native-response drain fail-closed；
- `uarch-baseline-c32-777.zstd_r`：86400 秒 sample timeout；
- 顶层 `collector-state.json` 最终记录 `[Errno 32] Broken pipe`。

C32 Zstd 的约 402 GiB 失败目录已在诊断后删除，因此当前 formal/source 只剩
39 个目录，即 38 complete 加 1 failed；不能用当前目录数反推当时只启动了
39 个 case。`inventory.json` 的更新时间早于全部任务结束，也不能替代逐 case
状态和 collector 输出作为终态来源。

必须撤回此前“C8 NAMD 使用的是未修复旧 binary”的判断。formal 的 run
contract、case `command.json` 和 binary 哈希共同证明，它使用的是 9 月 18 日
新构建且被冻结的 binary。两个 case 的失败也都不是顶层 `Broken pipe`
造成的：NAMD 已先由 gem5 明确退出，Zstd 已先由 supervisor 超时终止；
`Broken pipe` 是之后 collector 向已断开的外部 stdout 管道写汇总状态时产生的
独立错误。

### 6.2 C8 NAMD：此前修复未覆盖的新 lifecycle 出口

NAMD 到达 10M user-record common end 后，core 4 仍有两条已提交 software
prefetch，seq 分别为 `718635` 和 `718636`。两条记录的关键状态完全一致：

```text
found=1 committed=1 squashed=0
request=1 pred=1 mem_pred=1 translation=1/1
executed=1 completed=1
admissions=0 responses=0 hierarchy_requests=0
terminal_no_ruby=0
```

drain 因此轮询到 32768 次后按 fail-closed 规则拒绝发布：

```text
TaoTrace: committed native-response drain timed out after 32768 polls;
refusing to publish an incomplete baseline
```

此前几次修复针对的是不同 lifecycle 出口，包括 predicate-off、
memory-predicate-off 和根本没有 request 的 no-request 路径。这两条 NAMD
prefetch 已经存在 request，predicate 也均为真，所以正确绕过了旧 terminal
条件；但它们执行完成后没有进入 Ruby，也没有被标记为 no-Ruby terminal。
因此这不是“同一个修复反复失效”，而是严格 drain/conservation gate 在更完整
矩阵中暴露了另一个此前未覆盖的状态：

```text
request allocated -> executed/completed -> skipped before Ruby admission
```

修复必须放在真正决定 software prefetch 不再送入 Ruby 的生产点，并使用已有的
`PrefetchSkipped` terminal reason 闭合 lifecycle。不能在 drain 阶段把所有
`admissions=0` 的记录宽松吞掉，否则会掩盖真实漏发请求并破坏 native
conservation 的 fail-closed 语义。

2026-09-20 已按该约束完成修复。最终改动不是 drain 兜底，而是闭合两个相邻的
生产路径：

1. RubyPort 构造 software-prefetch request clone 时，保留原 Request identity，
   并与原 Request 共享同一个 native-response extension；
2. Sequencer 只在“同一 cache line 已有多个 outstanding request、因此本次
   software prefetch 被确定丢弃且绝不会进入 Ruby”这一决策点返回
   `RequestStatus_PrefetchSkipped`，记录 `prefetch_skipped` no-Ruby terminal，
   且不虚构 Ruby admission。

外部 gem5 改动已保存为
`patches/p2-external-native-prefetch-lifecycle.patch`，定向源码不变量测试为
`tests/test_native_prefetch_lifecycle.py`。修复 binary 为：

```text
/data00/yinhaolang/FastSim/tmp/se-fs-paired-c4-20260820/gem5-se-isolated/build/X86_MESI_Three_Level/gem5.opt
SHA-256: 468205d2b8f0cbd3ae3b23951e16ab4c48296621ab31f8187a4af14a635e398f
build mtime: 2026-09-20 11:55:57 +0800
```

原 formal binary 已另行冻结保存，其 SHA-256 仍为
`74d101122e4775f3cbf0c489af403ccbddb201a9a738ec13f3125edfa51d1e7a`；
本次验证没有覆盖或伪装原 formal 产物。

同一原 checkpoint 上的 10K gate 与原 10M target 精确复验均成功。10M 结果位于
`tmp/native-prefetch-fix-namd-c08-exact10m-20260920`，关键边界与原失败运行
一致：

```text
WORKBEGIN tick:       24317467192665
common-end tick:      24318450716490
trigger core:         1
core 1 user records:  10000000
core 4 user records:  9925538
target drain polls:   1
```

维护的 `audit_p1_native_response_sideband.py` 以零 hierarchy-gap 容差审计后：

- `all_structurally_conserved=true`；
- `all_target_drains_complete=true`；
- `all_collection_eligible=true`；
- 8 核均有 `pending_without_response_uops=0`、
  `response_without_native_fact_uops=0` 和
  `native_hierarchy_incomplete_uops=0`；
- core 4 的 `committed_memory_uops=2248104` 与原失败运行相同，
  `native_no_ruby_uops=28141`，terminal reason 为
  `store_forward=17634`、`local_access=10504`、`prefetch_skipped=1`；
- 全 case 共观察到 93 个真实 `prefetch_skipped` terminal，均纳入严格
  lifecycle 守恒，没有放松 zero-admission、common-end 或 zero-unknown gate。

因此 C8 NAMD 的代码缺口已修复并通过原 10M 边界复验；但在 C32 Zstd 恢复门禁
和定向复验完成前，原 40-case formal collection 的发布状态仍保持 38/40，
不能把单 case ad-hoc 验证写成全矩阵已完成。

### 6.3 C32 Zstd：异常发生在 measurement 前

C32 Zstd 不是因为“10M target 太大”而在 measurement 中跑不完。删除前证据
显示，它从 fresh checkpoint tick `21237102245395` 恢复后，24 小时内始终没有
打印 `WORKBEGIN`，也没有生成 functional-boundary JSON。约 402 GiB 空间主要
由 32 个 pre-WORKBEGIN raw FST 占用，每个约 12.7--13.8 GiB。也就是说：

```text
checkpoint restore
  -> source-defined functional warmup
  -> [24 h timeout here]
  -> WORKBEGIN not reached
  -> 10M common-end measurement never started
```

历史 serial/common-end C32 Zstd 是成功对照：

- 2026-09-11 约 17:06 启动，19:45:47 完成，总计约 2 小时 39 分；
- 使用的 checkpoint tick 路径不同；
- 在 tick `21014645494491` 到达 `WORKBEGIN`，随后进入正式 ROI。

较早的同日 buffer-114 C32 Zstd smoke 也支持“target 不是原因”：即使 target
只有 10K，它仍花约 84 分钟才到 `WORKBEGIN`，并在此前产生约 35 GiB raw
trace。当前 formal binary 下，Zstd C4/C8/C16 均完成，其他 C32 workload 也能
在数小时内完成，所以异常集中在 **Zstd C32 的 pre-WORKBEGIN 路径**，不能
概括成 Zstd 一直很慢或 O3 的普遍吞吐问题。

现存 formal 产物的文件时间戳还能把 Zstd 的正常耗时分成两段。这里
`pre-WORKBEGIN` 以 command/request 创建时间到 measurement PTE snapshot
时间近似，`ROI` 以该 snapshot 到最终 `trace.json` 时间近似：

| 配置 | pre-WORKBEGIN | WORKBEGIN 后 10M | 总计 |
| --- | ---: | ---: | ---: |
| C4 Zstd | 159 秒（2.7 分） | 442 秒（7.4 分） | 601 秒（10.0 分） |
| C8 Zstd | 696 秒（11.6 分） | 883 秒（14.7 分） | 1579 秒（26.3 分） |
| C16 Zstd | 3962 秒（66.0 分） | 2390 秒（39.8 分） | 6352 秒（105.9 分） |
| 历史成功 C32 Zstd | 未保留可直接分段的时间戳 | 未保留可直接分段的时间戳 | 约 9540 秒（159 分） |

另一个同配置的旧 C32 checkpoint 在 no-record probe 中用 3742 秒（62.4 分）
到达 `WORKBEGIN`。这些结果与异常 checkpoint 的 86400 秒仍无 `WORKBEGIN`
形成数量级分叉，不能用正常的 core-count scaling 解释。

2026-09-20 的固定条件 checkpoint A/B 已把分叉进一步缩小。两次 probe 均使用
修复后的 gem5 binary `468205...`、同一 O3/cache/DRAM geometry、同一
`buffer-114` 参数、同一 workload binary/argv/OMP 配置，并且都只运行到
`WORKBEGIN` 后一条指令且不生成 FST：

- 旧 checkpoint 由系统 gem5 binary `c2c6be...` 创建，tick 为
  `22288144235311`；恢复后 3742 秒到达 `WORKBEGIN` tick
  `22289855265900`，随后正常退出；
- 异常 checkpoint 由本轮 isolated gem5 binary `74d101...` 创建，tick 为
  `21237102245395`；原 formal 恢复后 86400 秒仍未到达 `WORKBEGIN`，同一
  checkpoint 的 no-record probe 也重复停留在相同的 pre-WORKBEGIN 路径。

两份 `prepared.json`、checkpoint `request.json` 和 metadata 的 workload、
拓扑、磁盘、TSO、模板及 boot-profile 身份相同；可见的合同身份差异是创建
checkpoint 的 gem5 binary，checkpoint tick、guest PTE root、调度现场和
memory/COW payload 也不同。单份成功快照与单份失败快照只能证明恢复结果依赖于
checkpoint 内部状态，不能证明创建 binary 本身是因果变量。因此可以确认：

```text
Zstd 输入和 10M target 未改变
  -> 恢复端使用同一修复 binary
  -> checkpoint A 可恢复
  -> checkpoint B 不到 WORKBEGIN
```

Zstd 的 source marker 进一步限定了故障窗口。程序先 fork 32 个 worker；每个
worker 到首个 block 后增加共享 `ready`，worker 0 等待 `ready == 32` 后触发
checkpoint，其他 worker 等待共享 `start`。恢复后 worker 0 应设置 `start`，
所有 worker 各完成 64 KiB warmup、增加共享 `warmed`，最后由 worker 0 在
`warmed == 32` 时发出 `WORKBEGIN`。两份 checkpoint 创建日志都已到达前一个
全员 barrier，异常恢复却没有到达后一个全员 barrier。因此当前最精确的根因
边界是：

```text
32-worker first-block barrier 已完成
  -> 在活跃多进程同步点保存并恢复 checkpoint
  -> leader 恢复后释放 start，或至少一个 worker 的 64 KiB warmup/汇合失去进展
  -> warmed 无法达到 32
  -> WORKBEGIN 永远不发出
```

异常运行持续产生大量 pre-WORKBEGIN 指令记录，所以不能称为整个 O3/LSQ
完全死锁。现有日志没有记录 `start`、逐 worker warmup 完成数或周期性 committed
instruction 进度，尚不能在上述三个子步骤中唯一定位。可以确认的根因类别是：
checkpoint 保存了一个正在执行跨 32 进程共享同步协议的瞬间，恢复正确性依赖
leader 和全部 worker 的现场共同前进；失败快照破坏了这一路径的可恢复进度。
这解释了同一输入、同一恢复 binary 下 checkpoint A 能到 `WORKBEGIN`、B 却在
24 小时内仍不到达的分叉，但现有证据不能进一步断言具体是 leader、某个 worker
还是 barrier 汇合状态首先失去进展。

### 6.4 已实现的 checkpoint 恢复门禁

2026-09-20 已增加新的正式合同
`configs/spec2026-tso-buffer114-baseline-40-v2.json`。原 v1 合同继续锁定
9 月 18 日的历史 binary 和既有采集语义，不会被静默改写；v2 则同时锁定：

- 修复后 gem5 binary SHA-256
  `468205d2b8f0cbd3ae3b23951e16ab4c48296621ab31f8187a4af14a635e398f`；
- `buffer/114/active` 和 `first-core-target-common-end-v1`；
- 显式 runtime `libstdc++.so.6` SHA-256
  `9ac7e2ec283e4a4a9a1ffdf0a17d688ca90e7f9d020cb8f372af7fa5194618f0`；
- schema 为 `fastsim-checkpoint-workbegin-gate-v1` 的恢复门禁。

v2 的每个 fresh preparation checkpoint 必须在进入 smoke/formal 前，由同一
修复 binary、同一 O3 配置、同一 cache/DRAM geometry 和同一 TSO 参数恢复一次。
probe 使用：

```text
--wait-for-roi-workbegin
--tao-no-records
--max-insts-per-core 1
```

它不产生 FST records；只有日志同时出现且顺序完成以下两个 source marker 才能
写出可复用的 complete receipt：

```text
[fs-ckpt] WORKBEGIN tick=...
[fs-ckpt] MAX_INSTS reached at tick=... phase=roi target=1
```

每个 probe 的 wall-time 上限为 7200 秒，整个输出目录上限为 256 MiB。supervisor
在运行中检查输出大小，超时、超限、外部停止或异常时终止整个 gem5 进程组；
正式 10M capture 不会启动。receipt 冻结 checkpoint 文件快照和 metadata、
workload request、gem5 binary/config SHA、硬件 geometry、TSO 参数和 probe
预算；恢复使用时重新检查这些身份以及日志 SHA、输出大小和零 FST 条件。中断后
没有 receipt 的目录会保留为 `checkpoint-recovery.interrupted-*`，不能被误认
为成功。若恢复门禁本身超时、超限或缺少 marker，collector 还会把对应
checkpoint 隔离为 `checkpoint.failed-*`，把 `prepared.json` 改名为
`prepared.recovery-failed-*`，并写入
`checkpoint-recovery-rejection-*.json`。因此下次 resume 必须重新创建
checkpoint，不会再对同一坏 checkpoint 重复等待两小时。

7200 秒是全矩阵通用上限，不是按 Zstd 正常值设置。现存 38 个成功 formal case
中，最慢的 pre-WORKBEGIN 是 C32 Neutron 的约 6715 秒（111.9 分），因此全局
7200 秒只多约 485 秒，不能直接缩短。对 Zstd 单独看，成功 no-record probe 为
3742 秒，旧 smoke 约 5040 秒到 `WORKBEGIN`；90 分钟可能是可行的 workload
专用上限，但在没有多份 fresh C32 checkpoint 重复性数据前不应写入正式合同。
当前 7200 秒相对于原 86400 秒 timeout 已把最坏等待缩短 12 倍，并由 256 MiB
上限阻止再次产生数百 GiB pre-WORKBEGIN trace。

该实现位于 `tools/collect_uarch_common_end.py`；SPEC 2026 静态合同检查已扩展到
v2，位于 `tools/collect_spec2026_uarch_fs.py`。相关 collector 测试 32/32
通过，包括 marker fail-closed、receipt 复用、FST 拒绝、运行中磁盘上限、
失败 checkpoint 隔离、进程组 timeout、binary/runtime SHA 锁定和 v1/v2
隔离。默认 `cmake --build build -- -j16` 和 `./build/fastsim_tests` 也已通过。

顶层 `Broken pipe` 也已修复。collector 仍先原子写入 per-case inventory 和
`collector-state.json`，进度文本改为 best-effort 输出；外部 stdout 管道断开
不会再中止任务收集或覆盖真实 case 错误。formal-only driver 同时移除了写死的
`136`，所有 expected/processed/complete 数量均从当前矩阵推导，因此 baseline-40
会正确记录 40，而不是沿用 C4/C8 generalization 矩阵的数量。

当前 sandbox 无法读写 `/dev/kvm`，所以这里不能重新执行 offline preparation
来创建另一份 fresh checkpoint。真实 gate 验证改为对已有 checkpoint 做固定
binary、固定 buffer-114 的恢复 A/B；这可以验证门禁本身并隔离 checkpoint
状态差异，但不能单独命名更低层根因。

### 6.5 Zstd pre-fork checkpoint 修复

2026-09-20 已把 Zstd checkpoint 从 32-worker 活跃同步点移动到唯一父进程中：

```text
create shared wave control
  -> allocate child PID table
  -> checkpoint once in the sole parent
  -> restore returns
  -> fork and pin 32 workers
  -> each worker completes the same 64 KiB compression warmup
  -> all-worker WORKBEGIN barrier
```

具体修改为：

- `zstdcli.c` 在任何 `fork()` 发生前调用
  `gem5_roi_checkpoint_once("zstd-before-worker-fork", 777)`；
- `benchfn.c` 删除每个 worker 的 `gem5_roi_wave_checkpoint()`，但保留原
  64 KiB `p.benchFn(...)` 和
  `gem5_roi_wave_begin("zstd-64k-warmup-complete", 777)`；
- `patches/tcsim-zstd-prefork-checkpoint.patch` 保存跨仓 overlay，
  `tests/test_zstd_prefork_checkpoint.py` 锁定调用顺序和 warmup/WORKBEGIN
  不变量。

SPEC 原生 GCC 11.5 配置重建和 runsetup 均成功。最终宿主 binary SHA-256 为
`76efb6c3126b105026700567023c47473232194d3ee272b7478b8116a8824919`。
基于原 `spec2026.ext4`、仅替换 Zstd binary 的独立镜像为
`/data00/yinhaolang/TCSim/data/spec2026_diskimg/spec2026-zstd-prefork-v1.ext4`，
SHA-256 为
`d2053efd398881db97325591ef5f8a8e2ad6b3d2d0db2e20a9322917d078b8a7`；
从镜像重新导出的 binary 与宿主 binary SHA 完全一致，并且只包含新的
`zstd-before-worker-fork` 和既有 `zstd-64k-warmup-complete` marker。

新合同 `configs/spec2026-tso-buffer114-baseline-40-v3.json` 保持原 40 case、
10M common-end、buffer-114 和 7200 秒 no-record recovery gate，只将 Zstd
路由到上述独立镜像，并同时锁定镜像和 workload binary SHA。静态合同预检通过，
marker 合同测试 3/3、collector 测试 33/33 通过。使用正式参数
`-b3 --verbose --compress-only -i1 -B16777216` 的本机 C32 功能检查中，
32/32 worker 均完成并正常退出。

这些结果证明源码、构建产物、镜像内容和 host-side 32-worker 执行路径一致，
但不等价于 gem5 checkpoint 恢复验证。当前环境没有 `/dev/kvm`，因此必须在
KVM 可用节点创建 **fresh v3 Zstd checkpoint** 并通过恢复门禁后，才能把
C32 Zstd 标记为动态修复；任何旧 Zstd checkpoint 都不含该修复，不能复用。

checkpoint 前移后，O3 恢复路径还会新增 fork、绑核和 worker 初始化工作，
因此目前更不能把 7200 秒缩短。7200 秒仍由全矩阵已成功的 C32 Neutron
6715 秒上界决定；待至少三份 fresh C32 Zstd checkpoint 都通过并获得稳定分布
后，才有依据讨论 Zstd 专用的更短 gate。

### 6.6 剩余最小闭环

1. C8 NAMD 的定向 lifecycle regression、10K gate 和原 10M target 复验已经
   完成；后续正式补采只替换该 case，不重跑完整 40-case 矩阵。
2. C32 Zstd 的固定恢复 A/B 和 pre-fork 源码修复已完成。下一步是在可用 KVM
   环境中使用 v3 合同与新镜像创建 checkpoint；恢复门禁失败时会自动隔离，
   不能进入 formal。
3. 若 pre-fork checkpoint 仍不能在 7200 秒内恢复到 `WORKBEGIN`，增加周期性
   guest/CPU 进度证据再做最小变更二分；不能直接写成 LSQ deadlock。
4. 只有新 checkpoint 通过恢复门禁后，才补采 C32 Zstd formal。

在两个 case 都通过原有严格门槛之前，本轮只能称为 38/40 的诊断性采集，不能
发布或用于 buffer-114 全矩阵精度结论。

### 6.7 2026-09-20 清理记录

确认旧 smoke 树中的 40 个 source case 和 8 个隔离副本全部为 `failed`，顶层
状态为 `incomplete` 后，已删除
`tmp/tso-buffer114-baseline40-20260918/smoke`。同时删除中断的 Zstd recovery
probe、NAMD 早期失败 gate 和 NAMD 10K 临时 gate，共释放约 259.4 GiB
（278.6 GB），文件系统可用空间由约 3.4 TiB 增至 3.7 TiB。

完成 pre-fork 修复并建立 v3 artifact 身份后，又删除异常 C32 Zstd
checkpoint `cf298f0acfa7ff71b899` 及其 stale preparation 副本，释放
334,324,623 bytes（约 319 MiB）。旧成功 checkpoint
`56d8e8ad56b30e9f5a32` 与 75 MiB no-record A/B 对照仍保留，供追溯原故障；
它们不满足 v3 workload/disk SHA，collector 不会复用。

保留 NAMD 原 10M 边界修复证据
`tmp/native-prefetch-fix-namd-c08-exact10m-20260920`、Zstd 成功 checkpoint A
及其 no-record 恢复证据
`tmp/zstd-c32-checkpoint-recovery-old-control-20260920`，以及 38 个完整
formal case。失败 checkpoint B 的身份和运行结论已写入本文，payload 不再保留。

### 6.8 非 Zstd 数据逐 case 严格审计

2026-09-20 对 9 个非 Zstd workload 的 C4/C8/C16/C32 数据进行了逐 case
复核。审计不是读取目录名或复用 `collection-audit.json:valid`，而是重新调用
`tools/collect_uarch_common_end.py:validate_case()`，并另行对每个
`oracle/kernel_events.json` 运行
`tools/validate_kernel_events_oracle.py` 的默认 zero-unknown 严格门禁。
审计只读取 JSON、manifest、每个 FST 的 72-byte header 和 dependency
companion header/文件尺寸；没有读取或哈希完整 FST payload。逐 case 摘要保存于：

```text
tmp/non-zstd-baseline40-strict-audit-20260920.json
```

结果如下：

| Workload | 预期配置 | 语义门禁通过 | 原 formal complete receipt | 替代证据 |
|---|---:|---:|---:|---:|
| 706.stockfish_r | 4 | 4 | 4 | 0 |
| 710.omnetpp_r | 4 | 4 | 4 | 0 |
| 782.lbm_r | 4 | 4 | 4 | 0 |
| 803.sph_exa_s | 4 | 4 | 4 | 0 |
| 811.tealeaf_s | 4 | 4 | 4 | 0 |
| 816.nab_s | 4 | 4 | 4 | 0 |
| 854.graph500_s | 4 | 4 | 4 | 0 |
| 857.namd_s | 4 | 4 | 3 | 1（修复后 C8） |
| 881.neutron_s | 4 | 4 | 4 | 0 |
| **合计** | **36** | **36** | **35** | **1** |

36/36 数据均满足以下 source-data 门禁：

- `first-core-target-common-end-v1`，全部参与核共享同一 end tick，触发核达到
  10M user UOP target；
- `effective-target.json`、最终 `config.ini` 和
  `tao_trace/uarch_profile.json` 身份一致，36/36 无 mismatch；
- CPI/CPL 双口径及 kernel-event cycle/population 守恒通过；
- aggregate 和逐核 `unknown_kernel` event/cycle 均为 0；
- native Ruby/SLICC hierarchy 语义可比较、结构守恒和 target drain 均通过；
- `native_hierarchy_incomplete_uops=0`、
  `pending_without_response_uops=0`、`hierarchy_gap_ratio=0`。

因此，**排除 Zstd 后，其余 9 个 workload 的 36 个配置在 trace、CPI oracle、
common-end、目标硬件身份、native Ruby 生命周期/守恒和 zero-unknown 层面均可用**。
这包括修复后的 C8 NAMD 10M 证据。

仍需保留两个边界：

1. 原 v1 formal 树中只有 35 个非 Zstd complete receipt；旧 C8 NAMD 目录仍是
   failed。修复后的 C8 NAMD 证据通过全部语义门禁，但尚未生成正式
   `collection-status.json` 并替换进 formal inventory。因此“非 Zstd 数据内容
   全部可用”不等于“formal corpus 已完整封装为 36/36”。
2. 36/36 的 native hierarchy 都是
   `native_hierarchy_semantic_comparable=true`，但当前 sideband auditor 明确给出
   `hardware_pmu_formal=false`、`formal_comparable=false` 和
   `accuracy_metrics_emitted=false`。所以这些 native Ruby/SLICC 数据可用于
   source-valid 诊断和维护 validator 的比较，不能表述为 strict hardware PMU
   formal certification。本次审计也没有重跑 FastSim，因而没有产生新的 CPI/PMU
   精度数字。
