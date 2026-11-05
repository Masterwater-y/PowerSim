# FLAC C4 10M：全功能伴随采集记录

采集路径：`tmp/spr-functional-10m-c4-20261105/source/c04-817.flac_s/`；请求和监督结果在其上级的 `capture-request.json`、`capture-result.json`。独立 gem5-spr `yinhaolang` 源提交 `fb8ef0f162`；可执行文件 SHA-256 `30f64ee77dfd0be87590e28632fef1601b09e05547533685823d40f1ea1cca61`。采集命令沿用前次 FLAC C4 负载及输入，启用 data translation origins、instruction translations、core facts、MMU controls 和 architectural boundaries；四核 `config.ini` 均确认 true。

工作线程身份准备就绪后，以 core 0 运行 500,000 条 gem5 指令停止计数为预热预算（此计数口径并非每核 500,000 条，也不等同 UOP 数）；预热期间的记录保留。进入测量后首个核达到 10,000,000 条用户 UOP 时共同截止。预热边界报告 1 个尚未回到用户态的 page-fault 事件，需要独立验证其绑定。

| 核 | 预热记录数（用户+内核 UOP） | 测量记录数（用户+内核 UOP） | 测量用户 UOP |
|---|---:|---:|---:|
| 0 | 841,026 | 5,706,822 | 5,321,670 |
| 1 | 710,135 | 10,004,580 | 10,000,000 |
| 2 | 693,335 | 9,315,040 | 9,231,097 |
| 3 | 1,206,592 | 9,880,423 | 9,877,115 |

四核总记录 38,357,953；core 1 触发共同截止。gem5 和监督进程退出码 0，存储 guard 未触发，native response drain 完成；端到端墙钟约 1,839 秒，`measurement-open` 到 `common-end` 约 1,290 秒。这些仅是单次运行记录，不是模型吞吐测试。

## 输入与参考质量

四核 `.core.gz`、`.ixlat.gz`、`.mmuctl.gz`、`.archb.gz`、原有 FST 及其他伴随文件均已产生。68 条 syscall 元数据通过 `tools/audit_fst_syscall_metadata.py --require-no-timing`：时间有效位和时间值均为零。OoO 的 `cpi-core*.json`、`native-summary-core*.json` 等独立输出存在；尚未完成目标身份、计时口径、Ruby/SLICC cache 真值或逐核计分窗口资格验证，因此本记录不报告模型 CPI/PMU 或误差。

**新功能束未通过全流读取，不能作为合格的 FastSim 完整输入。** `build/fastsim_audit_retired_frontend` 汇总失败为 `MMU controls: bundle terminal frontend span`；逐核检查 core 0、1、3 同样失败，core 2 的文件结构读取通过。原文件未修改，日志和 core 2 详细结果分别保留在上级的 `functional-audit.log`、`audit-core*.log` 和 `audit-core2.json`。

进一步只读解码发现，三个失败核各有一个 status=2 的部分事件：core 0 event 3、core 1 event 2、core 3 event 3，reason mask 均为 `0x300`（EntryDiscontinuity 与 MissingExit）。事件的 entry 只覆盖其 56-UOP 前端宏的前 29 条；起点、前端静态语义与代码哈希和对应 FFE 宏一致，但终点短 27 条。读取器当前要求含成员的 terminal 范围与前端宏范围完全一致。尚未证明问题属于 writer 或 reader，也未判定是否能安全接受部分事件；禁止为了通过而删除尾部、补造成员或盲目放宽校验。

core 2 的结构读取结果仍非完整输入：architectural bundle `ready_files=0`，24 条 MMU 控制命令未绑定，其原因计数均为 `UnboundPageFault`；core-facts 有 1,791 条适用记录为 Unknown；data translation 3 条仅有叶映射、无完整路径，instruction translation 无 Unknown（34 条 pseudo 为 N/A）。这些数字只属于 core 2 全流，包括预热和测量；其余核因读取失败尚无等价完整审计。即使解决读取错误，未绑定命令和 Unknown 仍需分别修复或明确保持不可评分。

新功能束是 OoO 观察生成的功能数据；Atomic/QEMU 独立 producer 等价、启用观察前后 native 中性及当前 FastSim 新路径完整执行均未得到本次采集证明。共同截止人口也不能直接替代每核预热退休起点的独立 OoO 计时参考。
