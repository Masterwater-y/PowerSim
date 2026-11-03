# Marian speed 八核 32GiB、10M 功能来源采集

2026-10-28 存储状态：用户已授权回收本文早期Marian C8来源的FST及专属旁表，
旧配置、统计和报告保留，来源已标记`retired-not-replayable`，不能再原样回放。
四负载新矩阵中的Marian C8正式来源另行保留，见[当前采集矩阵](spec-speed-32g-matrix-20261027.md)。
删除凭据见[回收收据](../tmp/fst-retirement-20261028/receipt.json)。

用户要求采集一组 C8 Marian 10M。本次使用当前项目默认关闭的大内存入口，显式
开启 32GiB DDR4；不复用旧二进制生成的 checkpoint，不缩小原模型或输入。

## 配置与来源

| 项目 | 本次配置 |
|---|---|
| 负载 | `872.marian_s` 标记版；官方第一条 refspeed 子命令 |
| 输入 | 原 `model.alphas.npz`、`vocab.spm`、`TildeMODEL-spec-long.en` |
| 并发 | 8 个物理参与核、1 个应用进程、8 个原生计算 worker；保留原辅助线程 |
| 工作负载参数 | mini-batch32、workspace64MiB、beam8；不使用此前容量研究的缩减参数 |
| 内存 | `--x86-large-memory --mem-size 32GiB --mem-channels 8`；每段8通道，总16个控制器/directory |
| 内存数据路径 | 低、高两段直接 DRAM/KVM 映射，关闭部分覆盖的 Ruby functional backing store |
| TSO | buffer，114 entries |
| ROI | 首个 decoder 计算入口，全体 worker ready 后 KVM→O3 |
| warmup | core0 请求500K instruction-stop counts，600秒墙钟保护；实际宏/UOP人口另记，不声称收敛 |
| 测量 | 最快核达到10,000,000 measured user UOP后，全核唯一共同终点；保留user+kernel和warmup前史 |
| 空间/运行保护 | 正式案FST10GB，墙钟4200秒；全局FST200GB、199GB提前停止、空闲至少20GB |

生产二进制 SHA256：
`f7d06e8bd7a6a61c94b482fcdf931b83e8e4f6072ab9f2bfe4272e639f2d2506`。
采集使用独立冻结的同字节二进制、runner、checkpoint helper、调度器 profile 和
共享头；`M5_OVERRIDE_PY_SOURCE=false`。输入与工具指纹、实际命令及日志均在
`tmp/spec-marian-c8-32g-10m-20261027/`。

## 硬件身份工具的必要补齐

维护工具 `tools/generate_fs_effective_target.py` 原先要求所有 DRAM 接口使用同一
连续区间，不能对32GiB来源生成有效身份。本次增加仅针对已支持 X86 布局的双段
识别：低段 `[0,3GiB)`、高段 `[4GiB,33GiB)`，总RAM按各段求和，排除I/O hole；
逐段检查相同通道数量及稠密的 `intlvMatch`，保留 `physical_ranges` 和
`channels_per_range`。原单区间行为保留，没有另写参考选择器或精度评分脚本。

先复现3项失败，再通过新增及原有共7项拓扑测试；另有7项TSO身份测试通过。
实际新checkpoint配置已通过维护身份入口。该改动只生成身份元数据，不修改模型
预测、不重编译或替换运行中的采集二进制。

## 执行与资格边界

新 Marian staged checkpoint 已正常保存，耗时421.61秒；输入、硬件和二进制身份
核对通过。**10M功能trace已完成：预期1案，功能人口/身份/原生守恒门禁通过1案，
退出码0，无保护器触发。** 这不包含下文尚未通过的完整syscall参数字段资格。
最快的core7恰好达到10,000,000 measured user UOP，全核共享终点
`63627505451685`；八核测量user UOP合计 **72,960,546**。8个FST共
**5,853,950,784 bytes（5.854GB）**，完整文件SHA256写入本轮`completion.json`。

| 核 | 实际测量user UOP |
|---:|---:|
| 0 | 8,978,135 |
| 1 | 8,864,326 |
| 2 | 8,988,791 |
| 3 | 8,972,919 |
| 4 | 8,948,016 |
| 5 | 8,926,949 |
| 6 | 9,281,410 |
| 7（触发核） | 10,000,000 |

没有独立裁剪其他核或补齐到每核10M。O3 warmup实测181.72秒，core0实际warmup
宏指令1,353,873；请求500K是gem5 instruction-stop单位，不替代实际人口。恢复、
warmup、stat和退出收尾合计1298.50秒；包含保留的1000µs量子退出延迟，不是吞吐
基准或SPECspeed分数。

来源后处理调用维护的共同终点、硬件身份、thread-state、task-PMU及native-response
守恒检查，并核对全部原生线程名册、统计前身份、测量期owner及syscall人口。
本案共同终点、独立硬件身份、owner守恒、task-native人口、完整进程名册及
native-response摘要守恒检查均通过。最终10个OS线程中，8个计算worker身份均在
stat前确认；主线程和另一个辅助线程在测量窗口均为0退休UOP、0 owner ticks，
没有新的clone/fork，unknown owner cycles为0，因此8个worker覆盖本窗口完整
应用进程人口。`workload-task`测量宏指令分母为39,127,055。
native cache来源只使用Ruby/SLICC摘要，不使用kernel-events的path-class字段。

后处理首次遇到一个适配器错误：FST v7合法的稀疏syscall尾表使文件长度大于纯
记录区长度。已改用维护的`audit_fst_syscall_metadata.audit_file`严格验证头、记录
及尾表边界，未改动任何源字节，随后预定的人口/身份/原生守恒审计通过；原失败
脚本和日志保留。

补充的严格 `--require-entry-coverage` 检查**未通过**：100次syscall均缺少参数字段，
这些调用全部位于warmup，测量段syscall为0。标记与元数据表的100行人口及文件布局
完整，但不能称为完整syscall entry或语义回放来源。逐核覆盖、严格检查失败和诊断
结果分别保留在 `syscall-field-qualification.json`、本轮
`syscall-metadata-audit.log` 及同案 `syscall-metadata-audit.json`；不把未知参数填零
或以只检查已知字段的诊断成功替代严格字段门禁。

采集阶段未运行FastSim回放或精度评分；后续已完成显式分段映射回放，见
[FastSim误差报告](marian-c8-32g-10m-fastsim-results-20261027.md)。不发布SPECspeed分数。
16个控制器由两段各8通道
组成，不能直接当作一个连续16路交织的目标。身份/来源守恒通过不等于SPR退休load
全量分类、零unknown、硬件perf等价或CPI/PMU精度认证。
初始PTE快照仍来自launcher CR3；日志保留应用non-target CR3及PTE unknown，不能
把本次完整功能人口/owner资格通过解释为初始页表问题已修复。

## 证据入口

- `campaign-request.json`、`frozen-identity.json`、`audit-tool-identity.json`；
- `checkpoint-creation-command.json`、`checkpoint/fastsim-checkpoint.json`；
- `source-command.json`、`source/c08-872.marian_s/capture-status.json`；
- `source/c08-872.marian_s/capture-receipt.json`、`common-end-audit.json`；
- 同案的 `identity-validation.json`、`process-scope-audit.json`、`native-response-audit.json`；
- 同案的 `syscall-field-qualification.json`：预热段100次调用参数未知、统计段0次；
- `source/c08-872.marian_s/tao_trace/manifest.txt`、`source-ready.json`、本轮 `completion.json`。
