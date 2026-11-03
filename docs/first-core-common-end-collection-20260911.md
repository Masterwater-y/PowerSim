# 最快核 10M：共同终点采集

日期：2026-09-11。用户将停止规则改为“最快核达到 10M 就停止”，替代同日此前的
“最慢核达标”提案。规范见 [项目规约 §3.0](project-goal-and-semantic-contract.md)。

## 实施

- 目标单位是测量段用户 UOP，沿用原 syscall marker 计数口径，不含功能预热或内核 UOP。
- `noteFunctionalRecordEmitted()` 在首核达标的宏指令边界同步关闭全部参与核心的
  FST、CPL 周期和退休/分支 PMU；原来各核独立冻结、等全部达标的逻辑已替换。
- native/frontend registry 同时关闭。按 committed 请求身份归属的在飞服务仍结清；
  drain 不延长 CPI 或新增计分指令，不能只检查触发核的 pending 请求。
- 非触发核可以在宏指令中途或 syscall 入口遇到共同事件。保留全部已采 UOP，
  宏指令分母只统计真实完成的宏指令；不补记录、不伪造 last-UOP、不对齐到另一个窗口。
- 边界记录 `first-core-target-common-end-v1`、触发核、共同 tick、参与核数、停止原因、
  每核实际人口；tick 只用于离线核对，不作为 FastSim 的时序输入。
- 新整理工具保存实际 record/macro 边界，慢核不足 10M 合法。旧每核 `>= 10M`
  校验与 syscall-entry EOF 拒绝条件不能用于新的共同事件。

交付代码：

- [采集补丁](../patches/gem5-taotrace-first-core-common-end.patch)
- [共同边界校验](../tools/common_end_capture.py)
- [FST 与 oracle 整理](../tools/collect_common_end_fst.py)
- [显式清单删除与并行重采集](../tools/recollect_common_end_fst.py)
- [完成后的元数据审计与新参考索引](../tools/audit_common_end_corpus.py)

本次构建已有隔离 gem5 树，未覆盖 sibling `gem5-fs` 或 TCSim 源码。采集二进制
冻结在 `tmp/first-core-common-end-20260911/gem5.opt`，SHA-256 为
`473ef0cb4792208f9a3ff60633142e4291da6816a693dcecc9aa50837b9c1471`。
FastSim 继续使用当前 `interval_weave + time_epoch` 两阶段路径，没有增加 causal_read、
设备组件或时序补偿。

## 必要验证

旧完整 LBM C4 采集被新门禁拒绝：它没有共同关闭证明。4 个边界单元测试覆盖
不等速实际人口、CPI 分母错配、独立结束 tick、缺失 policy/关闭事件或未达标触发核。

一次 LBM C4 短采集将目标设为 10,000 用户 UOP，实际结果：

| 核 | 测量用户 UOP | 共同 CPI/FST 结束 tick |
|---|---:|---:|
| 0 | 2,860 | 22,074,058,644,534 |
| 1 | 9,672 | 22,074,058,644,534 |
| 2 | 4,828 | 22,074,058,644,534 |
| 3（触发核） | 10,000 | 22,074,058,644,534 |

合计 27,360 用户 UOP；四核 FST/oracle 人口一致、硬件身份与 PMU oracle 门禁通过。
当前两阶段 FastSim 完整读取这份短采集并通过维护中的 native validator。该试验是
采集机制和输入兼容性验证，不是 10M 精度或吞吐量结果。正式采集目标仍为 10,000,000。
gem5 构建、补丁反向 dry-run、默认 FastSim 构建及 `fastsim_tests` 均通过。

## 完整重采集

替换上一轮 `tmp/formal40-latest-complete-deps-20260910/` 的 10 个负载 × C4/C8/C16/C32。
按显式清单删除 600 份旧 FST 及配套附件，共 2,936 个文件、725,374,940,620 bytes
（675.56 GiB）。保留 checkpoint、磁盘镜像、旧 oracle 和历史报告。

新任务根目录为 `tmp/first-core-common-end-20260911/`，40 路并行，每 case 一次。
不会自动调用旧的全核达标校验，不自动重试，也不将旧窗口的 CPI 填入新参考。
任务持续写入每 case 的 `status.json`，采集后执行共同边界、完整依赖附件头、
硬件身份及 PMU oracle 门禁，写入 `collection-results.json`；全任务结束后生成
`collection-finished.json`。后台元数据审计随后更新 `inventory.json` 为新窗口自己的
宏 CPI 参考和 manifest，并输出 `corpus-audit.json`，只有全部 40 个 case 通过才标为
完整。各核完整 FST SHA-256 和 companion SHA-256 保存在 trace 元数据。

已验证 40 个任务全部启动。第一批通过的正式 10M case 为 C4 Stockfish、NAMD、LBM、
TeaLeaf；LBM 四核实际用户 UOP 为 2,972,680 / 9,519,434 / 10,000,000 / 9,715,335，
所有核的指标共同结束。正式重采集完成状态以实时文件为准，不能把启动数
当作完成数或新的 CPI 精度结论。gem5 `stats.txt` 是退出时的原始诊断转储，可能包含
请求 drain；正式 CPI/PMU 使用已在共同事件关闭并按事件字典结清的 oracle，不能用
原始转储替代它。
