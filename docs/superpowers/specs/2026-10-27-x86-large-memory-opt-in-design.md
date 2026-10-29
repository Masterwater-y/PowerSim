# X86 FS 大内存显式开启与旧配置隔离

用户已确认默认关闭、独立组件的方案并要求实施及记录文档。

默认命令继续实例化现有 `X86Board` 和 `ChanneledMemory`，不改这两个公共类或
`HighBandwidthMemory`。新增 `--x86-large-memory`；仅当显式开启且容量超过
3GiB 时，runner 选择新增 `X86LargeMemoryBoard` 和 `X86MultiRangeDDR4Memory`。
开启但容量不超过 3GiB 仍选原组件。未开启却请求超过 3GiB 时，在仿真前明确报错。

大内存组件只支持当前 DDR4_2400_8x8，按 `[0,3GiB)` 和
`[4GiB,4GiB+(容量-3GiB))` 构造 RAM，保留 I/O hole，追加高段 E820 usable 条目；
每段独立配置 N 个交织控制器和 Ruby directory。单区间赋值保留基类控制器对象，
多区间检查总容量、非重叠、通道交织对齐后再重建控制器。HBM 不能传入新 Board。
大内存路径拒绝 `--ruby-functional-backing-store`，不默默覆盖用户选项。

两种候选方式：公共类中按开关分支，或开关选择独立派生组件。采用后者，避免公共
`ChanneledMemory` 的重建逻辑破坏 HBM 和调用者预先设置的控制器参数。

新增类在 `gem5-collection/overlay/src/python/` 维护，并同步到本地 `gem5-fs`；
通过独立 SConscript 注册补丁加入嵌入 Python 构建。旧两文件实验补丁保留作历史证据，
不应用到生产公共类。runner 的请求和 checkpoint 身份记录所选大内存模式；二进制、
runner 哈希检查继续有效，不承诺新二进制恢复旧收据。

验收：默认值及拒绝条件的 Python 回归；真实 gem5 中默认 3GiB、开启但 3GiB、
开启 32GiB 的接线和 E820；HBM 原控制器与 pseudo-channel 接线；旧容量上限；
32GiB KVM boot；项目默认 C++ build/test。当前 producer 的完整 Marian 短窗口实测
保留为历史证据，新功能不自动取得正式来源、CPI/PMU 或硬件认证。
