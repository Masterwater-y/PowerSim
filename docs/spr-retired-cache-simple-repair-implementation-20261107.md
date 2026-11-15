# SPR 退休 load miss 简化修复实施记录

按 [最终修复设计](superpowers/specs/2026-11-07-spr-retired-cache-simple-repair-design.md)
实施。当前为代码实现和回归验证阶段；尚未通过小 FS、中性、同源回放及四负载
验收，不能宣布修复完成或四负载零 unknown。

## 实现

- `gem5-spr/src/mem/spr_retired_load_miss.hh` 提供唯一共享规则源；
  FastSim 通过 `include/fastsim/spr_retired_load_miss.hpp` 引用。
  `Result` 有编译期 `sizeof <= 16` 检查，三位 miss 和估计标志在完成时转换，
  宏只做常数大小 OR；可信正成员不被缺失兄弟片段覆盖。
- Ruby 实际查询和非驻留合并点写入当前结果。Request/TBE 活对象共享下层
  服务结果，多消费者分别保留自己的上层事实；预取关联只查活 generation。
  主事件不依赖旧尝试数组或退休时的预取父链搜索。
- L1 demand/prefetch follower 在实际 admission 状态处分开标记；
  缺少合并点观察的共享响应保持 unknown，不凭最终 hit 回调补造驻留证据。
- L1 非驻留 follower 按 `{1,0,0}` 解释，L2 follower 保留实际共享 LLC
  服务，LLC follower 显式估计。stale 独立保留。权限请求、转发、设备绕过、
  软件预取及多 UOP/拆分的解释同时标记正、负成员估计。
- 完成回调按当前 Request 的最近完成身份幂等；全覆盖转发清除先前废弃
  Ruby 尝试对最终退休事件的贡献，晚到响应只收尾服务生命周期。
- 每个主事件输出成员/非成员/unknown、正负确定/估计拆分。
  新策略为 `spr-retired-load-miss-v2`；保留旧兼容请求诊断。
  `tools/retired_cache_contract.py` 拒绝新参考与旧预测混算，检查逐核
  正负估计守恒及 workload_task 的完整人口和零 unknown。
- `perf-retirement-core*.json` 的各 scope coverage 增加
  `spr_service_paths`，单位为“含该服务路径的退休 load 宏”。
  多个路径可重叠，不能把路径计数相加当独立 load 人口。
  不完整完成诊断保留请求 ID、地址、缺失层级和具体各层状态。

未改变 FastSim 的并行两阶段框架或模型时序；未引入 gem5 时序/服务事实作为
功能输入。这些结论描述代码边界，实际功能与时序中性仍需下面的独立 FS 对比。

## 小 FS 和中性验证入口

新增实际四线程小负载
`workloads/spec_roi_integration/spr_retired_cache_smoke.c`：
mode 0 为分散读取、重复读取和软件预取；mode 1 为转发/跨行候选；
mode 2 为共享读写/远端或 stale 候选。模式名不能证明实际路径已覆盖。
二进制位于 `tmp/spr-retired-cache-v2-20261107-build/spr-retired-cache-smoke`。

`fs_capture.py` 增加 `--save-staged-checkpoint`、
`--restore-staged-checkpoint` 和 `--checkpoint-only`，沿用 rate launcher
的 staged marker 顺序。checkpoint 绑定 workload、launcher、输入、内核、
磁盘、调度身份、目标/窗口、硬件参数、boot script 和 Python helper 内容；
载荷逐文件哈希验证。准备状态的身份不要求两个观察器二进制相同，
以便比较旧/new 观察器，但这条兼容边仍需实际恢复验证。

`tools/run_spr_retired_cache_smoke.py` 生成并执行受既有 storage guard 保护的
小窗口命令，默认 50K target、500K warmup、C4、SPR DDR5/32GiB。
同一 mode 使用同一带回执的 staged checkpoint 分别运行旧、新二进制，
再运行 `prepare_spr_capture.py` 和独立 retired-cache 审计。

`tools/compare_spr_retired_cache_neutrality.py` 已改为核验新策略，并要求
相同准备状态回执、输入内容、目标、逐核人口、窗口/task 集合、原有请求诊断、
完整 native summaries 和执行计数。缺少受支持的同一起点不能算通过。

## 沙箱失败回执与恢复验证

真实 FS 保存准备状态的尝试
`c8801392585bd057aad076bd373e79bb` 被会话沙箱拒绝，返回码 1：

```text
TRAE Sandbox Error: hit restricted
  Not allow operate files: /dev/kvm
  Hint: You can configure sandbox rules via Settings -> Conversation -> Custom Sandbox Configuration.
```

证据回执：
`tmp/spr-retired-cache-v2-20261107-build/fs-sandbox-block.json`。
启动未进入 workload 测量，没有可用于审计的新源或 checkpoint。
当时需要会话放行 `/dev/kvm`；没有用 SE hello 或旧四负载结果代替这一门禁。

用户要求继续实施后，当前标准 Shell 已能直接打开 `/dev/kvm`，取得
`KVM_GET_API_VERSION=12`，并成功执行 `KVM_CREATE_VM`。创建 VM 的实测
命令回执为 `059aef106f284dea1002d707622f45fd`，退出码 0。
本轮未修改沙箱规则，也未使用替代入口绕过限制。
**直接探测成功不能代表 gem5 命令获得同样权限**。

已启动三个小负载的 staged checkpoint 保存：
`checkpoint-mode0-retry1-run`、`checkpoint-mode1-run`、
`checkpoint-mode2-run`，位于上述工作回执目录，原失败目录保留。
mode 0 重试命令为 `c0ae6611e24514702a9b76dd26b85dbc`；
mode 1/2 分别为 `9c62562f399841424024204838e06595`、
`23b4691c80cf776cd7979d52a2567d07`。
mode 0 重试仍被沙箱拒绝 `/dev/kvm`，gem5 日志同时给出
`KVM: Failed to open /dev/kvm`。最终 `capture-receipt.json` 为
returncode 1、`guard_reason=producer-fatal`、`measurement_open=false`，
运行约 68.26 秒、FST 0 bytes，未产出可用 checkpoint。
mode 1 同样收到设备拒绝；mode 2 已停止，避免重复无效准备。
实际 FS 沙箱阻塞仍未解除，unknown 验证未开始。
启动前 storage guard 记录现有 FST 为 355,960,298,202 bytes；
各次运行 FST 预算 1,000,000,000 bytes，仍执行既定全局保护。

已核对 [TRAE 官方沙箱文档](https://docs.trae.cn/ide_sandbox)：
在 **设置 → 对话流 → 沙箱自定义配置 → 打开配置** 中，把
`/dev/kvm` 加入现有 `filesystem.readWrite` 列表；保存生效。
若同一路径同时位于 `readOnly`，只读优先。
当时未提供专门的会话沙箱策略工具，尚未修改配置。

随后用户明确要求“调整权限使其允许”。已通过文件编辑工具创建官方约定的
`/root/.trae-cn/sandbox.json`，只加入
`filesystem.readWrite=["/dev/kvm"]`，未放开整个 `/dev`、
关闭沙箱或修改设备的系统权限。已读回确认内容。
真实 mode 0 FS 重试命令为 `a80d825a27f425745b074a0f116d2c40`，
输出为 `checkpoint-mode0-retry2-run`，预定 checkpoint 为
`checkpoint-mode0-retry2`。配置是否被当前远程会话采纳，仍须以此次
实际 gem5 启动和最终回执验证，不能仅凭配置文件存在宣称恢复。

随后用无需网络资源的最小 gem5 `System + KvmVM` 配置验证。
命令 `a57c2ee57fc5b0ab930337b0f98d0d05` 仍被 TRAE 沙箱明确拒绝
`/dev/kvm`，gem5 在 `src/cpu/kvm/vm.cc:83` 报
`KVM: Failed to open /dev/kvm`。日志为
`kvm-permission-probe-retry1.log`。因此写入远端官方路径并未使当前
执行策略生效，不能宣布权限修复。等待资源准备的完整 FS retry2 已停止。
需要通过 TRAE 客户端的沙箱自定义配置入口使权限被当前会话采纳；
当前工具没有客户端配置刷新接口。小 FS 和四负载验收仍未通过。

后续顺序仍为：实际小 FS 特殊路径覆盖和逐核零 unknown → 同起点中性 →
gem5/FastSim 同源回放 → Marian/Graph500/XZ/FLAC C4 最终重采集。
最终四负载保留 10M target、500K warmup 和既定源/身份/native 守恒门禁；
尚未启动。

### 当前会话的小 FS 重启

用户再次要求开始小负载 unknown 验证后，当前会话实际 gem5 KVM
实例化已通过，命令 `62d377e5cd3ca9cbb12dfbbf89d11800` 退出码 0，
输出 `GEM5_KVM_INSTANTIATION_PASSED`。本轮没有修改沙箱或设备权限。
这替代了此前对当前会话设备可用性的阻塞判断，但不代表负载验证通过。

三个模式已通过维护的 `run_spr_retired_cache_smoke.py` 启动准备状态保存，
使用 C4、SPR DDR5/32GiB、500K warmup 和 50K target。输出分别为
`checkpoint-mode{0,1,2}-session2-run`，位于既有工作回执目录；
命令 ID 分别为 `ba9fe4c0b2aaa02e06f167584f9a72df`、
`be3d408f230bbf1810f003068d85ed77`、
`56ccb768c1699ab7017f5e5e22b63295`。准备状态保存后将使用新观察器恢复采集，
然后核验身份、独立退休侧计数、逐核 unknown 和实际服务路径。

当前观察器 SHA256 为
`f55049051fb9c55fc7e31e3154c9cd8d910ce403f72ee5ec16ea2cb67796207c`；
小负载 SHA256 为
`952ff581d76599b2c764567f066f64b560e92a98eb6b364466c543d95688dded`。
尚无本轮实际退休计数结果；小 FS 验收仍未完成。

这三次准备随后确认在资源锁等待：实际 gem5 线程的 `wchan` 均为
`hrtimer_nanosleep`，本地磁盘的 gem5 下载锁文件自旧失败尝试遗留。
通过给三次 storage guard 发送 SIGTERM 正常收尾，回执均为
`guard_reason=signal-SIGTERM`、`measurement_open=false`、FST 0 bytes，
运行约 130 秒；没有进入负载，也没有将它们记为 unknown 验证失败。

维护的小负载入口已增加 `--kernel`、`--disk` 和
`--disk-root-partition` 参数转发；本地参数及原资源默认行为的检查通过。
重启明确选择 `/root/.cache/gem5/x86-linux-kernel-5.4.49-1.0.0` 和
`/root/.cache/gem5/x86-ubuntu-18.04-img-1.0.0`，没有修改下载锁或本地资源。
两者 SHA256 分别为
`da3d5e7dbf696b977f635d1123e06b324af873183d90111101ced005dc64e174`、
`00aed79a5b91fbd97885944c3f70511e19756d4f6ae9e13e15322ab9c8759905`，
与原失败尝试的资源身份一致。

命令 `96fa38c969a39142c7d451f7238a6743` 已启动三个模式的完整短流程：
保存 `checkpoint-mode{0,1,2}-session2-local`，恢复到
`smoke-mode{0,1,2}-session2-local`，通过 `prepare_spr_capture.py`
核验独立硬件身份，再调用 `audit_retired_cache` 和维护的 native response
auditor。每一步都有独立命令、日志、退出码及状态回执。
总回执为 `smoke-session2-local-completion.json`；实际服务路径单独报告，
不会根据用例名称推断覆盖，零 unknown 不自动代表特殊路径全部覆盖。

**本轮最终结果：真实 FS 仍被当前会话沙箱阻塞。** 上述完整短流程命令
返回 `TRAE Sandbox Error: hit restricted`，明确指出
`Not allow operate files: /dev/kvm`。三个模式均在 checkpoint 阶段失败，
gem5 在 tick 0 报 `src/cpu/kvm/vm.cc:83: fatal: KVM: Failed to open /dev/kvm`。
每次回执均为 returncode 1、`guard_reason=producer-fatal`、
`measurement_open=false`、FST 0 bytes，约 16.45 秒结束。
没有保存可用准备状态，也没有执行恢复采集或退休侧审计。
总回执 `passed=false` 代表环境启动失败，不能解释为观察器产生了 unknown。

进程检查确认三个 gem5 均已退出。现有 `/root/.trae-cn/sandbox.json` 仍含
`filesystem.readWrite=["/dev/kvm"]`、`readOnly=[]`，但真实 FS 命令的拒绝
说明该文件未使当前执行策略放行设备。仍需通过 TRAE 客户端的沙箱配置入口
使 `/dev/kvm` 放行对当前会话生效；本轮没有工具可刷新客户端执行策略。
小 FS 零 unknown、特殊路径、中性、同源回放和四负载验收均未通过。
本轮简短结果见 [小 FS 启动验证回执](spr-retired-cache-small-fs-validation-20261107.md)。

## 回归与构建

工作回执目录为 `tmp/spr-retired-cache-v2-20261107-build/`。
早期完整 gem5 build 和包含主体生命周期/路径诊断的 `build-final.log`
均已通过；L1 预取 subtype 的 `build-l1-prefetch.log` 已通过。
停止修改 C++ 源码后的最终复建 `build-stable.log` 已通过，
命令 `1e51251f17646562cc3e62a5c4b54835` 退出码为 0，
日志以 `scons: done building targets.` 结束。
FastSim 最终构建 `fastsim-build-final.log` 已通过，
直接 `fastsim_tests` 已通过，日志为 `fastsim-tests-final.log`；
`fastsim_perf_retirement_tests` 已通过。

已通过的专项回归：共享 SPR 转换/估计规则、活多消费者/预取/stale、
缺失回调诊断、重复回调、废弃转发请求、软件预取主事件、
逐核正负估计守恒、checkpoint marker/内容身份和中性门禁。
这些是单元或入口回归，不代表实际 FS 门禁通过。

未进行真机 perf 采集；`hardware_equivalence=unverified`。
未发布本轮 CPI、PMU 精度或吞吐测量。未提交或推送。

### Codex 会话：小 FS 验证进行中

本会话真实 mode 0 KVM 启动、staged checkpoint 保存及恢复采集已完成。
checkpoint 回执 `checkpoint-mode0-codex-run/capture-receipt.json` 和采集回执
`smoke-mode0-codex-retry/capture-receipt.json` 的返回码均为 0，storage guard
未触发；采集到达 common-end。首次恢复因保存时使用相对 workload 路径、
恢复时使用绝对路径而未通过 checkpoint 参数身份比较；原失败保留在
`smoke-mode0-codex/`，使用原路径重试成功。

`prepare_spr_capture.py` 在补齐 `/opt/gcc-11/lib64` 运行库路径后成功，
独立 target identity 有效。`retired-cache-audit.json` 使用维护审计器生成，
策略为 `spr-retired-load-miss-v2`，报告无 evidence gaps；这不代表零 unknown。
workload_task 各核 L1/L2/L3 miss 的 unknown 分别为 3665、3721、3599、3626
（三个事件在每个核上数值相同）；退休 load 人口 unknown 分别为 5、4、3、2。
主要 reason 为 `U-INCOMPLETE-LIFECYCLE`，另有 `U-UNSUPPORTED-ISA`。
这些是原始 native 侧文件中的分类，不是审计脚本用代理生成的参考。
core 0 样本的旧服务诊断含 L1 hit，但新策略完成时三层状态均未判定；
这是调查线索，不能据此认定根因或直接补造成员关系。

mode 1/2 checkpoint 正在执行，尚无退休审计结果；四负载未启动。当前结果
不满足零 unknown 门禁，不发布完整 PMU 精度或硬件等价结论。

随后 mode 1/2 的 staged checkpoint 均成功，回执的返回码为 0 且 guard 未触发。
mode 1 从 checkpoint 恢复后在详细预热期间报
`RuntimeError: SPR current Request/TBE observer capacity exhausted`，上限为
65,536；测量窗口未打开，采集返回码 1。回执和日志位于
`smoke-mode1-codex/`。该失败需要查明活对象注册、完成和释放的实际生命周期；
不能把提高上限视为修复，也不能产生 mode 1 的 unknown 或路径覆盖结论。

mode 2 恢复采集到达 common-end，回执返回码 0，guard 未触发；准备完成且
独立 target identity 有效。维护的退休审计没有 evidence gaps，但三个 miss
事件每核 unknown 分别为 2447、2495、2504、2220，均标为
`U-INCOMPLETE-LIFECYCLE`；各核退休 load 人口 unknown 均为零。原始侧文件
实际观测到 forwarded、demand/prefetch follower、remote、stale 路径；
partial-forward 和 split/retry 在此窗口未观测到。服务路径计数允许重叠。
mode 0 与 mode 2 的独立 native response 审计均报告结构守恒及 target drain
完成；这与新 SPR 成员分类不完整是不同门禁。各自的
`retired-cache-audit.json`、`native-response-audit.json` 和原始逐核侧文件
均保留在 `smoke-mode0-codex-retry/`、`smoke-mode2-codex/`。

三模式小 FS 验证未通过：mode 0/2 有真实 unknown，mode 1 在预热失败。
不继续同起点中性、同源 FastSim 评分或四负载验收；先排查上述两类观察器问题。

### 小 FS unknown 原因定位

后续通过原始逐核样本、生成的 SLICC 代码和生产组件隔离复现，
确认驻留 L1 hit 的观察写入发生在 `noteHierarchyCompletion` 之后，
当前结果此时已转换并移出表；还确认 Request 扩展到退休事实的投影漏拷贝
`sprMiss`。同一 Packet 重发后旧 attempt 未结束造成活对象表累积的机制
也已复现，但 mode 1 实际残留的完整构成尚未确认。
隔离复现命令 `1a9caee34afcbfe414fb1a92cfdad67d` 四项检查通过。
本次只定位原因，未修改模拟器源码或重采集。
详细证据和边界见 [unknown 原因定位](spr-retired-cache-unknown-diagnosis-20261108.md)。
