# SPR 功能 FST 与 OoO baseline 实施计划

目标：一次 OoO FS 运行输出仅含可由 Atomic/QEMU 功能执行获取或由功能事实和静态目标策略确定性推导的信息的 FST，同时独立输出 OoO CPI/PMU 基准。禁止修改 SPR 微架构行为；不得将基准信息送回 FastSim。

1. 在 `gem5-spr/src/cpu/o3/probe/tao_trace.cc` 的 FST syscall 输出边界清除 pre/post timestamp 值及有效位，保留其他 syscall 架构事实和原生计时统计。先建回归测试，再实施并检查二进制产物；旧文件由独立工具复制清理。
2. 将 SA/SD 配对由 SQ 指针改为同一动态退休宏与固定 decoder 策略的功能配对。为不完整、异常、宏切换、核/线程与重复 PC 建立明确边界和拒绝行为。先建立独立单测和已有 FLAC 全流对照，再接入 writer；不得先改 schema 或输出一个尚未证明的 group。
3. 定义只含功能数据的退休及分片快照，统一 OoO 和未来 Atomic producer 的请求/翻译字段，按同轮退休身份绑定；明确 Unknown、特殊访问、跨页及异常。逐类测试，并保持模型真实资源独立裁决。
4. 将 OoO 的 per-core 起止退休身份、CPI 和 Ruby/SLICC PMU 参考输出到独立 baseline，并绑定功能流身份，严格核验起止人口与统计覆盖；不能假定共同开始/结束等同逐核窗口。
5. 编译 gem5-spr 并以小型 Atomic FS/ OoO 同功能人口样例验证字段差分、FST 不含时间及资源观测、采集开关对 OoO 行为中性。完整 FLAC C4 仅在输入和窗口合格后进行；记录 CPI 绝对/相对误差及吞吐，不以单测代替端到端。

当前子仓库 `yinhaolang` 源 checkpoint 1ef38b9055 的新功能 bundle 尚无完整 native build/执行资格。旧 FST 清理、旧 OoO 指标和组件测试不能代替步骤 5。每阶段保留失败与限制，未经证明不宣称整体完成。

## 进行中：2026-11-05

- 步骤 1 的源码改动已在 FST syscall row 写出前清除 native 时间值和有效位，保留内部观测及其余功能字段；有独立 helper 测试及输出调用位置检查。
- 步骤 2 的源码改动已使用按顺序、context/ASID/PC 匹配且由静态 decoder 确认的双成员宏配对，未知形状不推断，fault abandonment 清理候选。独立 helper 的边界测试通过；将该 helper 应用于原 FLAC 四核完整流，与原 SQ group 的 981,312 对逐项一致。此测试重用 OoO 产出的宏边界、角色，不证明 Atomic producer 和异常全覆盖。
- 静态策略指纹包含新 helper 源码。两项改动只触及 probe 和 trace 描述，不更改 CPU 执行结构。gem5 object 编译与实际采集、中性证明仍待完成；步骤 3–5 尚未完成。
- FastSim 默认构建和 `fastsim_tests` 通过；静态策略 39 个测试通过。gem5 构建首次环境检查选错 Python 库，修正后首次增量构建在 300 秒上限停止，继续使用相同输出目录增量构建。为了诊断源码语法，曾在临时目录复制旧生成参数头并仅添加当前源码已有的参数字段；基于该临时头的 `tao_trace.cc` 语法检查通过。这不是正式 SCons 构建或可运行二进制验证。
- 后续正式 SCons 的 `tao_trace.o` 目标编译通过，日志 `tmp/atomic-input-validation-20261105/gem5-probe-build-v3.log`；新 `gem5.opt` 的完整增量构建及采集仍待验证。原有可执行文件已备份在 `tmp/atomic-input-validation-20261105/gem5-before-functional-isolation.opt`。离线配对核查代码保留为 `tools/audit_spr_functional_store_pair.cpp`；旧 FFE 缺少 OS 线程标识，该核查不能证明跨线程配对正确性。
- 首次完整构建发现既有 checkpoint 的 `fetch.cc` 使用 `X86ISA::PCState` 却没有包含其定义，编译失败。已补 `arch/x86/pcstate.hh` include，不改取指行为；增量构建重试中。首次失败日志为 `tmp/atomic-input-validation-20261105/gem5-full-build.log`。
- `tools/audit_fst_syscall_metadata.py --require-no-timing` 拒绝有效时间位和未标记但非零的时间值；单测覆盖二者。对已清理 FLAC 四核通过，对原含时间的 FST 拒绝。这只检验 syscall 时间，不对其他功能字段做全量合格声明。
- 完整构建继续增量编译成功，进入最终链接后达到 300 秒上限退出（`gem5-full-build-v3.log`），已恢复旧二进制并校验备份和恢复文件的 SHA-256 均为 `ec5059fe79c48955d28755b260ff93239c446e50bb5bba6690b0e0217da3d5dd`。`scons -n` 确认只需标签生成和链接，现进行单独的有上限链接重试；尚未宣布新二进制合格。
- 用户明确纠正：gem5 编译和链接使用 128 核，不对链接设人为时间上限。前述带上限链接已停止、旧二进制 SHA 已核对恢复；现运行 `scons --linker=gold build/X86/gem5.opt -j128`，无 timeout，实测 gold 命令带 `--threads --thread-count=128` 且进程具有 128 线程。线程配置不代表每一阶段均能满载 128 核。
- 无超时的 gold/128 构建成功，新二进制 SHA-256 `30f64ee77dfd0be87590e28632fef1601b09e05547533685823d40f1ea1cca61`。四核 FLAC 短窗口采集正在运行：10k 预热、100k 首核 user UOP target，参数记录在 `tmp/atomic-input-validation-20261105/smoke-capture-command.json`。这是采集格式冒烟，不作为 CPI/PMU 精度或吞吐结论；完整功能输入合同和原生窗口对齐仍未完成。
- 短采集已正常退出：四核 FST 共 993,107 records，52 条 syscall 元数据无时间有效位且数值为零；采集目录下分别存在四核 CPI 和 native summary 文件。新采集产出的 FST 通过同宏配对一致性读取，四核共 17,676 对、无不匹配和不完整；这是自一致性检查，不是独立 SQ 真值或 Atomic 差分。共同截止由 core3 触发，core0 的测量 user records 仅 10,995，说明短窗口各核覆盖不均，不能把它当正式精度样本。统计和窗口身份尚未验证。启动准备约 381 秒，measurement-open 到 common-end 约 10 秒；不把这当稳定吞吐。
