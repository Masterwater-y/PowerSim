# SQLite checkpoint 前移初始化成本 / XZ 恢复崩溃修复

## 问题与原因

SQLite 原 checkpoint 的位置是 `rate-staged-before-fork`：OS、输入与程序已经准备好，但八个数据库副本还没有创建。恢复后仍要逐个 fork、exec 和初始化数据库。旧 launcher 又在切换 O3 后读取 `/proc` 并向 UART 输出完整普查，因此这些管理工作被计入 detailed warmup。上次正式采集的预热为 826.02s，总耗时 4080.47s；该次 producer 退出 0，但任务 10 跨核迁移缺少可认证顺序，source gate 失败，不能算有效完整采集。

XZ 正式采集在恢复约 126s 后发生宿主 gem5 SIGSEGV，尚未切换 O3。实际栈为 `pthread_kill → BaseKvmCPU::wakeup → X86 APIC timer`。短、正式命令在此之前的路径相同，只改变测量目标和输出目录。

checkpoint 中最近 APIC 定时事件距恢复点 874,690,766 ticks，小于 KVM 同步量子 1,000,000,000 ticks。APIC 位于 event queue 0，八个 vCPU 位于 queue 1–8；APIC 可以在对应 worker 执行 `restartEqThread()` 前触发 `wakeup()`。旧 `kick()` 无条件使用尚未初始化的 `pthread_t`，因此短采集偶然通过不代表恢复可靠。

## SQLite 新流程

新增显式选项 `--checkpoint-phase compute-ready --rate-serial-init`，旧 staged 默认流程保留。

1. KVM 完成 OS boot、输入部署和程序启动。
2. 逐个初始化八个 SQLite 副本；先就绪的副本阻塞在原计算入口 pipe，避免同时初始化的峰值内存超出 3GiB。
3. 所有副本就绪后，仍在 KVM 下读取完整 `/proc` 普查。compute-ready 模式要求每个副本只有主线程。`fflush` 后执行 `tcdrain` 排空 TTY 输出，避免把尚未传输的完整普查带入恢复后的 O3。
4. 在所有计算门均未释放时保存 checkpoint。phase、serial-init 策略、launcher/runner/producer、硬件、输入、kernel 和 disk 身份全部绑定。
5. 保存后继续或恢复后，都执行一个**新的** resume checkpoint 标记，再切换 O3。恢复流程不等待已经消费过的保存标记重新出现。
6. 在 O3 下检查相同阻塞子进程仍存在，输出保存在 RAM 中的任务名册和普查快照；释放共同计算门，然后执行原定 500K 指令预热与 stat。

恢复后的名册是受控阻塞期间保存的同一组任务，不是新读取的 RSS；RSS 是 checkpoint 时的快照。完整 `/proc` 读取仅在 KVM 阶段执行。身份标记、源文件、共同终点、参与者和 syscall/owner 资格仍走原审核，不以已保存名册替代实际 trace 身份证据。

`capture-phases.jsonl` 记录 boot/restore 开始、保存点、保存完成、切换开始/完成、measurement-open 和 common-end 的宿主墙钟与 tick，便于区分首次建 checkpoint 的初始化成本和后续重复运行成本。

## XZ producer 修复

`BaseKvmCPU` 显式初始化线程句柄，增加 `std::atomic<bool> vcpuThreadReady`。在线程自己的 `restartEqThread()` 完成 signal handler、counter 和 timer 初始化后，用 release 发布就绪；`kick()` 用 acquire 检查。startup、drainResume、notifyFork 和 switchOut 使旧状态失效。

未就绪时仅省略没有接收对象的 signal，保留完整 `wakeup()` 和 guest context 激活。APIC 在调用 wakeup 之前已经保存 pending IRQ，X86 KVM 首次进入会检查并注入中断，因此不通过丢弃中断来掩盖崩溃。

## 验证与边界

- 新增 checkpoint marker 状态机和真实 C launcher 的串行初始化测试：先红后绿，2 项通过。
- KVM 回归直接执行真实 `kick/wakeup` 函数体：未就绪不发 signal、仍激活 guest；就绪后发送；失效后不使用旧线程，2 项通过。
- 原 rate capture 6 项、输入归档 4 项、native speed 4 项、XZ ROI 2 项、warmup 3 项和空间 guard 6 项通过。
- `cmake --build build -- -j16` 与使用 GCC 11 运行库的 `fastsim_tests` 通过。
- gem5 使用当前可用 192 核的约 80%，`-j153` 编译通过。旧 producer 已封存，新 binary、runner、launcher 和输入另建冻结实验。

实验目录：`tmp/spec-c8-ready-kvm-repair-20261022/`。SQLite 新建 compute-ready checkpoint；XZ 新建 staged checkpoint，先重复两次 2,000,000,000-tick 恢复，再执行短采集。短采集审核通过才启动各自 10M 正式采集。

SQLite detailed warmup 墙钟上限 900s，XZ 1800s；均保留 500K instruction-stop。FST 总量上限 200GB、全局停止线 199GB、可用磁盘保留 20GB；每次采集通过现有原子空间预算与 producer guard。旧三项合格完整 FST 和故障证据不覆盖。

上述测试和编译证明实现机制；真实 checkpoint 恢复、短/正式采集及 source gates 的结果以实验状态和 receipts 为准。本次不产生新的 CPI/PMU 精度结论。

## 实际执行记录

首轮 SQLite 新建 checkpoint 在 `staged-before-fork` 后运行了原配置允许的 100,000,000,000,000 ticks（100s 模拟时间），退出原因是 `simulate() limit reached`。宿主总耗时 337.74s，未到 compute-ready，未打开测量、没有保存 checkpoint。producer 退出 0 后，实验仍因缺少 checkpoint receipt 拒绝继续。这不是 900s 预热超时。

SQLite 后续使用独立目录 `tmp/spec-c8-ready-kvm-repair-sqlite-init500s-20261022/`：先对相同 launcher/输入做真实主机八副本入场检查，仅新建 checkpoint 时把模拟时间预算放宽到 500s；恢复采集保持原 100s 模拟预算、500K 指令预热和 900s detailed warmup 墙钟限制。输入、八通道和 CPU/TSO 参数不变。该时间预算不属于已保存 RAM/硬件身份，新 checkpoint 仍由实际 launcher、runner、producer 和输入全量哈希绑定。

XZ 首轮已保存新 staged checkpoint，进入重复有界恢复测试。审计 helper 已根据代码复核改用冻结工具和冻结配置，修订记录在 `freeze-audit-amendment.json`，未修改 producer/runner/checkpoint 身份。

XZ 连续两次真实恢复已按 tick 上限正常退出，均 `returncode=0`、无 guard，宿主耗时分别 229.91s 和 228.32s。各运行 2,000,000,000 ticks；第一个待处理 APIC 事件距恢复点 362,307,995 ticks，小于 1,000,000,000-tick 同步量子，两次都跨过这一窗口。此结果证明这两次恢复没有崩溃，不代替完整短采集资格。机器可读证据见实验目录 `xz-restored-apic-window.json` 和 `restore-probe-*/c08-801.xz_s/`，之后已启动完整 recovery-smoke。

SQLite 新 launcher 的真实八副本主机入场验证通过：114.61s、峰值 RSS 2,262,851,584 bytes、就绪 RSS 2,112,704,512 bytes，8 个任务均到达门禁，无 swap。该主机检查仅证明入场和内存规模，不是 gem5 checkpoint 恢复或程序最终结果正确性认证。

500s 建点预算的 SQLite 已到达 compute-ready 并成功保存：从第一个 staged marker 到就绪约 149.17s，保存压缩内存镜像约 431.20s。该次保存的 UART 仍只有普查开头的 `[`；`Terminal` 使用 unitbuf 每字符落盘，因此不是宿主日志缓冲，而是 guest TTY 发送队列尚未排空。仅移动 `/proc` 和 `fflush` 仍会把输出成本带入 O3。

因此补充 `isatty + tcdrain`（EINTR 重试、其余错误拒绝继续），并增加真实 launcher 的排空顺序回归：包装 isatty/tcdrain，先观察失败，再验证 drain 位于保存标记之前。旧诊断恢复通过 guard 的 SIGTERM 停止并保留 receipt 和 checkpoint；最终版本另建 `tmp/spec-c8-ready-kvm-repair-sqlite-drain-20261022/`，新 launcher 重新哈希绑定，保持 producer/runner/输入和 900s/500K 预热不变。该目录的 receipt 与 source-ready 才作为后续正式资格依据。

排空针对保存前的完整 stdout 普查。随后 handoff 自身还有一小段 marker 输出，恢复后仍输出缓存名册和执行身份确认，因此此变更没有消除全部 O3 管理指令或串口成本。真实保存验证需确认 UART 中已完整出现 `pre-checkpoint-census end`，不能只看包装函数的单元测试。

最终 drain 版本已在真实 gem5 下保存成功：UART 完整包含八个 PID/TID 的普查、`pre-checkpoint-census end` 和 compute-ready 文本。8 个副本均只有主线程、VmSwap=0，保存时没有 resume/compute-gate-release，也没有 O3 switch。保存边界与全部 checkpoint 文件哈希审核通过，见最终目录 `checkpoint-placement-audit.json`（`checkpoint_saved=true`、`payload_hashes_validated=true`）。已经启动从此 checkpoint 的 recovery-smoke；完整恢复来源资格和正式重采结果仍等待自动审核。

## 后续进度检查：两项短采集均超时停止

本次现场检查发现两项 worker 已退出，无 gem5 采集进程，均未生成 recovery-smoke 的 `source-ready.json`，也未启动 10M 正式 source。上文“运行中”是此前阶段记录，不代表当前状态。

| 负载 | 已确认进展 | 最终停止原因 | 短采集 FST |
|---|---|---|---:|
| SQLite | 新 compute-ready checkpoint 成功恢复；独立 resume marker 后切换 O3 | detailed warmup 902.32s，触及 900s 墙钟上限；未进入 measurement | 2,058,805,248 bytes |
| XZ | 两次有界恢复通过；本次 O3 warmup 1424.06s 后打开 measurement，未重现 SIGSEGV | 总运行 2401.68s，触及 2400s 总时限；退出码 -15；未完成来源审核 | 3,224,387,584 bytes |

XZ 日志有 native-response drain complete，但没有 runner common-end/simulation complete 或成功 receipt，不能据此宣称完整短采集成功。SQLite checkpoint 位置和 payload 审核成功也不代表预热时限问题已经解决。

检查时全局 FST 为 93,851,215,416 bytes，低于 200GB 上限；磁盘可用约 371.50GB。当前障碍是预热及完整运行耗时，不是存储上限。本轮没有新增合格正式 FST，也没有新增 FastSim CPI/PMU 精度结果。
